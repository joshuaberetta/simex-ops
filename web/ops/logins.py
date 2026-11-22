"""Login emails: a person's SIMEX Mail login (username, a new password, the QR code and sign-in
link of signin.py, and how to get started) emailed by Resend to their personal email
(Person.personal_email), and when each login was shared (Person.login_shared_at / _via).

Passwords are never stored readable, so every login email issues a new password: the old one,
its printed slips and QR codes stop working. The Message row written before the send keeps the
email without the password and the sign-in link."""

import base64
import io
import uuid

import segno
from django.conf import settings
from django.db import IntegrityError
from django.template.loader import render_to_string
from django.utils import timezone

from . import conf, mailer, render, signin
from .models import Message, Person, audit

FROM_LOCAL = "exercise-control"  # answers to it (no reply-to set) are kept in Replies, ignored
HIDDEN = "(only in the email sent)"


def from_addr():
    return f'"SIMEX Exercise Control" <{FROM_LOCAL}@{settings.MAIL_DOMAIN}>'


def problem(person):
    """Why person's login can't be emailed, or None."""
    addr = (person.personal_email or "").strip()
    if not person.active:
        return f"{person.name} is inactive."
    if person.role != "desk" and not person.team_id:
        return f"{person.name} has no team."
    if not addr:
        return f"{person.name} has no personal email."
    if not render.EMAIL.match(addr) or render.PLACEHOLDER_DOMAIN.search(addr):
        return f"{person.name}: {addr} is not a usable address."
    return None


def qr_png(text):
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=8, border=4)
    return buf.getvalue()


def _context(person, password, link, note):
    return {"p": person, "first_name": person.first_name, "team": person.team, "username": person.user.username,
            "password": password, "link": link, "site": settings.MAIL_URL or settings.PUBLIC_URL,
            "expires": signin.expires(), "note": note.strip(), "personal": conf.flag("personal_mailboxes")}


def subject(person):
    return "Your SIMEX Mail login" + (f" ({person.team.name})" if person.team_id else "")


def send(person, note="", reply_to="", user=""):
    """Issue a new password for person's login (creating the login if need be) and email it,
    with the QR code, to their personal email. Returns an error message, or None when sent."""
    from .views_excon import ensure_account
    err = problem(person)
    if err:
        return err
    addr = person.personal_email.strip()
    password = ensure_account(person, reset=True)
    person.refresh_from_db()
    link = signin.url(person.user) if signin.can_use(person.user) else ""  # none for the persona desk
    real = _context(person, password, link, note)
    kept = {**real, "password": HIDDEN, "link": HIDDEN if link else "", "redacted": True}
    m = Message(kind="login", channel="email", target=f"{person.name} · login", from_addr=from_addr(), to=[addr],
                reply_to=reply_to or None, subject=subject(person), team_id=person.team_id, person=person,
                text=render_to_string("ops/email/login.txt", kept), html=render_to_string("ops/email/login.html", kept))
    n = Message.objects.filter(kind="login", person=person).count() + 1
    m.idem_key = f"login-{person.id}-{n}"
    try:
        m.save()
    except IntegrityError:  # another send for this person at the same moment
        m.idem_key = f"login-{person.id}-{n}-{uuid.uuid4().hex[:6]}"
        m.save()
    payload = {"from": m.from_addr, "to": m.to, "subject": m.subject,
               "text": render_to_string("ops/email/login.txt", real),
               "html": render_to_string("ops/email/login.html", real),
               "headers": {"X-Simex-Login": str(person.id)}}
    if reply_to:
        payload["reply_to"] = reply_to
    if link:  # shown in the HTML by its content id; an attachment too, where images are blocked
        payload["attachments"] = [{"filename": "simex-mail-sign-in.png", "content_id": "signin-qr",
                                   "content": base64.b64encode(qr_png(link)).decode()}]
    try:
        rid = mailer.get().send(payload, m.idem_key)
    except Exception as e:  # mailer.SendError, or anything else: the new password wasn't shared
        Message.objects.filter(pk=m.pk).update(status="failed", error=str(e)[:500])
        audit("login_email_failed", f"{person.name} <{addr}>: {e}", user)
        return f"{person.name}: {e}"
    now = timezone.now()
    Message.objects.filter(pk=m.pk).update(status="sent", sent_at=now, resend_id=rid)
    Person.objects.filter(pk=person.pk).update(login_shared_at=now, login_shared_via=addr)
    audit("login_email", f"{person.name} <{addr}>", user)
    return None


def last_emails(people):
    """{person id: their latest login email (Message)}."""
    out = {}
    for m in Message.objects.filter(kind="login", person__in=people).order_by("id"):
        out[m.person_id] = m
    return out

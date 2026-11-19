"""Participants' answers: in-app messages (matched exactly) and received email (Resend
webhook with Svix signature, content fetch, matching by reply token or a guess)."""

import base64
import hashlib
import hmac
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone as dt_tz
from email.utils import parseaddr

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from . import mailer
from .models import Message, Person, Response, ResponseAttachment, audit

log = logging.getLogger("simex.inbound")
MAX_ATTACHMENT = 25 * 1024 * 1024
TOLERANCE = 300  # seconds, for webhook timestamps


class BadSignature(Exception):
    pass


def _key(secret):
    return base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)


def sign(secret, msg_id, ts, body: bytes):
    """The signature header Resend would send (tests, local simulation)."""
    return "v1," + base64.b64encode(hmac.new(_key(secret), f"{msg_id}.{ts}.".encode() + body,
                                             hashlib.sha256).digest()).decode()


def verify(secret, headers, body: bytes):
    msg_id, ts, sigs = headers.get("svix-id"), headers.get("svix-timestamp"), headers.get("svix-signature")
    if not (secret and msg_id and ts and sigs):
        raise BadSignature("missing signature headers or secret")
    try:
        if abs(time.time() - int(ts)) > TOLERANCE:
            raise BadSignature("timestamp outside tolerance")
    except ValueError as e:
        raise BadSignature("bad timestamp") from e
    expected = sign(secret, msg_id, ts, body).split(",", 1)[1]
    for sig in sigs.split():
        version, _, value = sig.partition(",")
        if version == "v1" and hmac.compare_digest(expected, value):
            return
    raise BadSignature("no matching signature")


def _addr(value):
    name, addr = parseaddr(value or "")
    return name or None, addr.lower()


def _ours(addr):
    return addr.endswith("@" + settings.MAIL_DOMAIN.lower())


def persona_and_token(addresses):
    for a in addresses:
        _, addr = _addr(a)
        if _ours(addr):
            persona, _, token = addr.split("@")[0].partition("+")
            return persona, token or None
    return None, None


SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|tr|rv|sv|wg)\s*(\[\d+\])?\s*:\s*|\[[^\]]*test[^\]]*\]\s*)+", re.I)


def clean_subject(subject):
    return SUBJECT_PREFIX.sub("", subject or "").strip().lower()


def guess(r: Response):
    """No exact link (a fresh message to a persona): the persona's latest delivery to that
    team, preferring the same subject. Marked "guess"."""
    if not r.persona:
        return
    qs = Message.objects.filter(from_addr__icontains=f"<{r.persona}@", status="sent", sent_at__lte=r.received_at,
                                kind__in=["live", "reply"])
    if r.team_id:
        qs = qs.filter(Q(team_id=r.team_id) | Q(team__isnull=True) | Q(inbox_team_id=r.team_id))
    cands = list(qs.order_by("-sent_at")[:20])
    subj = clean_subject(r.subject)
    best = next((m for m in cands if subj and clean_subject(m.subject) == subj), None) or (cands[0] if cands else None)
    if best:
        r.message, r.inject_id, r.match = best, best.inject_id, "guess"
        r.team_id = r.team_id or best.team_id


def match_email(r: Response):
    _, from_addr = _addr(r.from_addr)
    person = Person.objects.filter(email__iexact=from_addr).first() if from_addr else None
    if person:
        r.person, r.team_id = person, person.team_id
    if r.reply_token:
        m = Message.objects.filter(reply_token=r.reply_token).first()
        if m:
            r.message, r.inject_id, r.match = m, m.inject_id, "token"
            r.team_id = r.team_id or m.team_id
            r.kind = "live" if m.kind in ("live", "reply") else "test"
            return
    guess(r)


# --- in-app ----------------------------------------------------------------------------------

def from_mail(mail, personas):
    """A participant's in-app message to one or more personas -> one Response (the first
    persona answers it). A reply links exactly to the message it answers; a fresh message
    is matched by guess."""
    parent = mail.in_reply_to
    persona = personas[0]
    r = Response(channel="inapp", mail=mail, received_at=mail.created_at, from_addr=mail.from_addr,
                 from_name=mail.from_name, to=[f"{p}@{settings.MAIL_DOMAIN}" for p in personas], subject=mail.subject,
                 text=mail.body, persona=persona, team=mail.team, person=mail.author, fetched=True,
                 kind="test" if mail.team.is_test else "live")
    src = parent.message if parent and parent.message_id else None
    earlier = Response.objects.filter(mail=parent).first() if parent is not None else None  # a follow-up
    if src:
        r.message, r.inject_id, r.match = src, src.inject_id, "exact"
    elif earlier and earlier.inject_id:
        r.message, r.inject_id, r.match = earlier.message, earlier.inject_id, "exact"
    elif parent is not None and parent.inject_id:
        r.inject_id, r.match = parent.inject_id, "exact"
    else:
        guess(r)
    r.save()
    audit("inapp_response", f"{mail.team.name} -> {', '.join(personas)}: {mail.subject[:80]}")
    return r


# --- email -------------------------------------------------------------------------------------

def record_event(event):
    """Store a webhook event. Returns the Response id to fetch, or None."""
    kind, data = event.get("type", ""), event.get("data") or {}
    if kind == "email.received":
        rid = data.get("email_id") or data.get("id")
        if not rid or Response.objects.filter(resend_id=rid).exists():
            return None  # retried webhook
        return _new_email(rid, data).id
    if kind.startswith("email.") and data.get("email_id"):
        m = Message.objects.filter(resend_id=data["email_id"]).first()
        if m:
            m.delivery, m.delivery_at = kind.split(".", 1)[1], timezone.now()
            m.save(update_fields=["delivery", "delivery_at"])
            if m.delivery in ("bounced", "complained"):
                audit(f"email_{m.delivery}", f"{', '.join(m.to)}: {m.subject[:80]}")
    return None


def _new_email(rid, data):
    name, addr = _addr(data.get("from"))
    to, cc = list(data.get("to") or []), list(data.get("cc") or [])
    persona, token = persona_and_token(to + cc + list(data.get("received_for") or []))
    created = data.get("created_at")
    try:
        received = datetime.fromisoformat(created.replace("Z", "+00:00")) if created else timezone.now()
        if received.tzinfo is None:
            received = received.replace(tzinfo=dt_tz.utc)
    except ValueError:
        received = timezone.now()
    r = Response(channel="email", resend_id=rid, from_addr=addr, from_name=name, to=to, cc=cc,
                 subject=data.get("subject") or "", persona=persona, reply_token=token, received_at=received)
    if _ours(addr):
        r.status = "ignored"  # our own all-Bcc emails arrive here too
    match_email(r)
    r.save()
    audit("inbound", f"{addr} -> {persona or '?'}: {r.subject[:80]}")
    return r


def fetch(response_id):
    m_ = mailer.get()
    r = Response.objects.filter(pk=response_id).first()
    if not r or r.fetched or r.channel != "email":
        return
    try:
        full = m_.get_received(r.resend_id)
        r.text, r.html, r.headers = full.get("text"), full.get("html"), full.get("headers")
        r.message_id_header = full.get("message_id")
        r.subject = r.subject or full.get("subject") or ""
        for a in (m_.received_attachments(r.resend_id) if full.get("attachments") else []):
            size = int(a.get("size") or 0)
            data = m_.download(a["download_url"], MAX_ATTACHMENT) if size <= MAX_ATTACHMENT else None
            ResponseAttachment.objects.create(response=r, filename=a.get("filename") or "attachment",
                                              content_type=a.get("content_type") or "application/octet-stream",
                                              size=size or len(data or b""), data=data)
        r.fetched, r.error = True, None
    except Exception as e:  # keep the row; Sync retries
        log.exception("fetch of received email failed")
        r.error = f"{type(e).__name__}: {e}"
    r.save()


def fetch_async(response_id):
    threading.Thread(target=fetch, args=(response_id,), daemon=True).start()


def sync(limit=50):
    m_ = mailer.get()
    added = 0
    for item in m_.list_received(limit=limit):
        rid = item.get("id")
        if rid and not Response.objects.filter(resend_id=rid).exists():
            _new_email(rid, item)
            added += 1
    todo = list(Response.objects.filter(channel="email", fetched=False).values_list("id", flat=True))
    for rid in todo:
        fetch(rid)
    return added, len(todo)


QUOTE_START = re.compile(r"^(On .{5,200} wrote:|-----\s*Original Message|From: .+|Le .+ a écrit|El .+ escribió)",
                         re.I | re.M)


def split_quoted(text):
    if not text:
        return "", ""
    m = QUOTE_START.search(text)
    if m and m.start() > 0:
        return text[:m.start()].rstrip(), text[m.start():]
    lines = text.splitlines()
    for n, line in enumerate(lines):
        if line.startswith(">") and all(not x.strip() or x.startswith(">") for x in lines[n:]):
            return "\n".join(lines[:n]).rstrip(), "\n".join(lines[n:])
    return text, ""


def first_sent(kinds):
    """(inject id, team id or None) -> earliest delivery time."""
    from django.db.models import Min
    out = {}
    for row in (Message.objects.filter(status="sent", kind__in=kinds)
                .values("inject_id", "team_id").annotate(at=Min("sent_at"))):
        out[(row["inject_id"], row["team_id"])] = row["at"]
    return out


def response_minutes(r):
    m = r.message
    if m and m.sent_at:
        return max(0, int((r.received_at - m.sent_at) / timedelta(minutes=1)))
    return None

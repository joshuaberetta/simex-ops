"""Received email: Resend webhooks (signature check), fetching the content, and matching
each email to the inject and team it answers."""

import base64
import hashlib
import hmac
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from email.utils import parseaddr

from sqlalchemy import func

from . import config, mailer
from .db import (InboundAttachment, InboundEmail, Message, Person, SessionLocal, audit, utcnow)

log = logging.getLogger("simex.inbound")
MAX_ATTACHMENT = 25 * 1024 * 1024
TOLERANCE = 300  # seconds, for webhook timestamps


class BadSignature(Exception):
    pass


def verify(secret, headers, body: bytes):
    """Svix-style signature check used by Resend webhooks."""
    msg_id, ts, sigs = headers.get("svix-id"), headers.get("svix-timestamp"), headers.get("svix-signature")
    if not (secret and msg_id and ts and sigs):
        raise BadSignature("missing signature headers or secret")
    try:
        if abs(time.time() - int(ts)) > TOLERANCE:
            raise BadSignature("timestamp outside tolerance")
    except ValueError as e:
        raise BadSignature("bad timestamp") from e
    key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    for sig in sigs.split():
        version, _, value = sig.partition(",")
        if version == "v1" and hmac.compare_digest(expected, value):
            return
    raise BadSignature("no matching signature")


def sign(secret, msg_id, ts, body: bytes):
    """For tests and local simulation: the signature header Resend would send."""
    key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    return "v1," + base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()


def _addr(value):
    name, addr = parseaddr(value or "")
    return name or None, addr.lower()


def _ours(addr):
    return addr.endswith("@" + config.MAIL_DOMAIN.lower())


def persona_and_token(addresses):
    """First address at our domain -> (persona, token) from persona+token@domain."""
    for a in addresses:
        _, addr = _addr(a)
        if _ours(addr):
            local = addr.split("@")[0]
            persona, _, token = local.partition("+")
            return persona, token or None
    return None, None


SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|tr|rv|sv|wg)\s*(\[\d+\])?\s*:\s*|\[[^\]]*test[^\]]*\]\s*)+", re.I)


def clean_subject(subject):
    return SUBJECT_PREFIX.sub("", subject or "").strip().lower()


def match(s, em: InboundEmail):
    """Attach an inbound email to its message / inject / team / person."""
    _, from_addr = _addr(em.from_addr)
    person = s.query(Person).filter(func.lower(Person.email) == from_addr).first() if from_addr else None
    if person:
        em.person_id = person.id
        em.team_id = person.team_id
    if em.reply_token:
        m = s.query(Message).filter_by(reply_token=em.reply_token).first()
        if m:
            em.message_id, em.inject_id, em.match = m.id, m.inject_id, "token"
            em.team_id = em.team_id or m.team_id
            em.kind = "live" if m.kind in ("live", "reply") else "test"
            return
    # No token (a fresh email, or a client that dropped the reply-to): the latest email
    # from that persona to that person's team, preferring one with the same subject.
    if em.persona:
        q = s.query(Message).filter(Message.from_addr.ilike(f"%<{em.persona}@%"), Message.status == "sent",
                                    Message.sent_at <= em.received_at, Message.kind.in_(["live", "reply"]))
        if em.team_id:
            q = q.filter((Message.team_id == em.team_id) | (Message.team_id.is_(None)))
        cands = q.order_by(Message.sent_at.desc()).limit(20).all()
        subj = clean_subject(em.subject)
        best = next((m for m in cands if subj and clean_subject(m.subject) == subj), None) or (cands[0] if cands else None)
        if best:
            em.message_id, em.inject_id, em.match = best.id, best.inject_id, "guess"
            em.team_id = em.team_id or best.team_id
            return
    em.match = "none"


def record_event(s, event):
    """Store a webhook event. Returns the InboundEmail id to fetch, or None."""
    kind, data = event.get("type", ""), event.get("data") or {}
    if kind == "email.received":
        rid = data.get("email_id") or data.get("id")
        if not rid:
            return None
        if s.query(InboundEmail).filter_by(resend_id=rid).first():
            return None  # webhook retried
        return _new_inbound(s, rid, data).id
    if kind.startswith("email.") and data.get("email_id"):
        # Delivery events for emails we sent: delivered, bounced, complained, delivery_delayed...
        m = s.query(Message).filter_by(resend_id=data["email_id"]).first()
        if m:
            m.delivery, m.delivery_at = kind.split(".", 1)[1], utcnow()
            if m.delivery in ("bounced", "complained"):
                audit(s, f"email_{m.delivery}", f"{', '.join(m.to)}: {m.subject[:80]}")
        s.commit()
    return None


def _new_inbound(s, rid, data):
    name, addr = _addr(data.get("from"))
    to, cc = list(data.get("to") or []), list(data.get("cc") or [])
    persona, token = persona_and_token(to + cc + list(data.get("received_for") or []))
    created = data.get("created_at")
    try:
        received = datetime.fromisoformat(created.replace("Z", "+00:00")).replace(tzinfo=None) if created else utcnow()
    except ValueError:
        received = utcnow()
    em = InboundEmail(resend_id=rid, from_addr=addr, from_name=name, to=to, cc=cc, subject=data.get("subject") or "",
                      persona=persona, reply_token=token, received_at=received)
    if _ours(addr):
        em.status = "ignored"  # our own all-Bcc emails arrive here too
    s.add(em)
    s.flush()
    match(s, em)
    audit(s, "inbound", f"{addr} -> {persona or '?'}: {em.subject[:80]}")
    s.commit()
    return em


def fetch(inbound_id):
    """Download body, headers and attachments of a received email."""
    m_ = mailer.get()
    with SessionLocal() as s:
        em = s.get(InboundEmail, inbound_id)
        if not em or em.fetched:
            return
        try:
            full = m_.get_received(em.resend_id)
            em.text, em.html = full.get("text"), full.get("html")
            em.headers = full.get("headers")
            em.message_id_header = full.get("message_id")
            if not em.subject:
                em.subject = full.get("subject") or ""
            for a in (m_.received_attachments(em.resend_id) if full.get("attachments") else []):
                size = int(a.get("size") or 0)
                data = m_.download(a["download_url"], MAX_ATTACHMENT) if size <= MAX_ATTACHMENT else None
                em.attachments.append(InboundAttachment(filename=a.get("filename") or "attachment",
                                                        content_type=a.get("content_type") or "application/octet-stream",
                                                        size=size or len(data or b""), data=data))
            em.fetched, em.error = True, None
        except Exception as e:  # keep the row; Sync retries
            log.exception("fetch of received email failed")
            em.error = f"{type(e).__name__}: {e}"
        s.commit()


def fetch_async(inbound_id):
    threading.Thread(target=fetch, args=(inbound_id,), daemon=True).start()


def sync(limit=50):
    """Catch up on received emails (missed webhooks) and retry failed fetches."""
    m_ = mailer.get()
    added = 0
    with SessionLocal() as s:
        for item in m_.list_received(limit=limit):
            rid = item.get("id")
            if rid and not s.query(InboundEmail).filter_by(resend_id=rid).first():
                _new_inbound(s, rid, item)
                added += 1
        todo = [e.id for e in s.query(InboundEmail).filter_by(fetched=False)]
    for iid in todo:
        fetch(iid)
    return added, len(todo)


QUOTE_START = re.compile(r"^(On .{5,200} wrote:|-----\s*Original Message|From: .+|Le .+ a écrit|El .+ escribió)", re.I | re.M)


def split_quoted(text):
    """(new part, quoted part) of a reply's plain text, roughly."""
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


def response_minutes(em, s):
    m = s.get(Message, em.message_id) if em.message_id else None
    if m and m.sent_at:
        return max(0, int((em.received_at - m.sent_at) / timedelta(minutes=1)))
    return None

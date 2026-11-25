"""Participants' answers: in-app messages (matched exactly) and received email (Resend
webhook with Svix signature, content fetch, matching by reply token or a guess)."""

import base64
import hashlib
import hmac
import logging
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone as dt_tz
from email.utils import parseaddr

from django.conf import settings
from django.db import connection
from django.db.models import Q
from django.utils import timezone

from . import conf, logins, mailer, notices, render
from .models import Event, Message, Person, Response, ResponseAttachment, audit

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
    qs = Message.objects.filter(Q(persona=r.persona) | Q(from_addr__icontains=f"<{r.persona}@"), status="sent",
                                sent_at__lte=r.received_at,
                                kind__in=["live", "reply", "followup", "leader", "desk"])
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
    by_addr = Q(email__iexact=from_addr) | Q(personal_email__iexact=from_addr)  # also an answer to a copy sent there
    person = Person.objects.participants().filter(by_addr).first() if from_addr else None
    if person:
        r.person, r.team_id = person, person.team_id
    m = Message.objects.filter(reply_token=r.reply_token).first() if r.reply_token else None
    if m:
        r.message, r.inject_id, r.match = m, m.inject_id, "token"
        r.team_id = r.team_id or m.team_id
        r.kind = "live" if m.kind in ("live", "reply", "followup", "leader", "desk") else "test"
    else:
        guess(r)
    # A team leader answering their copy of an inject: kept, never counted as the team's reply.
    leader = Person.objects.filter(by_addr, role="tl").first() if from_addr and not person else None
    if leader or (m and m.person and m.person.is_leader):
        r.match, r.status = "leader", "ignored"
        r.team_id = r.team_id or (leader.team_id if leader else None)


# --- in-app ----------------------------------------------------------------------------------

def from_mail(mail, personas):
    """A participant's in-app message to one or more personas -> one Response (the first
    persona answers it). A reply links exactly to the message it answers; a fresh message
    is matched by guess."""
    parent = mail.in_reply_to
    persona = personas[0]
    r = Response(channel="inapp", mail=mail, received_at=mail.created_at, from_addr=mail.from_addr,
                 from_name=mail.from_name, to=[render.persona_addr(p) for p in personas], subject=mail.subject,
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
    elif not mail.event_id:  # an invitation (SIMEX Calendar) answers no inject
        guess(r)
    r.save()
    audit("inapp_response", f"{mail.team.name} -> {', '.join(personas)}: {mail.subject[:80]}")
    notices.reply(r)
    from . import desk, npc
    npc.on_response(r)  # first: the desk's copy says when an AI persona answers it (desk._told_in)
    desk.on_response(r, personas)
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
            record_engagement(m, kind.split(".", 1)[1], data, event.get("created_at"))
    return None


DELIVERY_STATES = {"sent", "delivered", "delivery_delayed", "bounced", "complained", "failed", "suppressed"}


def _when(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else timezone.now()
        return dt if dt.tzinfo else dt.replace(tzinfo=dt_tz.utc)
    except (ValueError, AttributeError):
        return timezone.now()


def record_engagement(m, what, data, created_at=None):
    """Delivery state, opens and clicks for one of our emails (Resend webhook)."""
    click = data.get("click") or {}
    at = _when(click.get("timestamp") or created_at or data.get("created_at"))
    Event.objects.create(message=m, type=what, at=at, link=click.get("link"),
                         user_agent=(click.get("userAgent") or click.get("user_agent") or "")[:400] or None)
    if what in DELIVERY_STATES:
        # A late "delivered" must never hide a bounce.
        if not (m.delivery in ("bounced", "complained") and what in ("delivered", "delivery_delayed", "sent")):
            m.delivery, m.delivery_at = what, at
        if what in ("bounced", "complained"):
            audit(f"email_{what}", f"{', '.join(m.to)}: {m.subject[:80]}")
    elif what == "opened":
        m.open_count += 1
        m.opened_at = min(m.opened_at, at) if m.opened_at else at
    elif what == "clicked":
        m.click_count += 1
        m.clicked_at = min(m.clicked_at, at) if m.clicked_at else at
        if not m.opened_at:  # a click proves an open, even with the tracking pixel blocked
            m.opened_at, m.open_count = at, max(m.open_count, 1)
    m.save(update_fields=["delivery", "delivery_at", "open_count", "opened_at", "click_count", "clicked_at"])


def record_read(mails, person, user_agent="", what="read", link=None):
    """In-app: a participant opened a conversation (read) or downloaded a file (download).
    Counts on the Message that delivered each item, first time kept."""
    now = timezone.now()
    for mail in mails:
        m = mail.message
        if not m or m.kind not in ("live", "reply", "followup", "leader", "desk", "test", "oneoff"):
            continue
        Event.objects.create(message=m, type=what, at=now, person=person, link=link, user_agent=(user_agent or "")[:400] or None)
        if what == "read":
            m.open_count += 1
            m.opened_at = m.opened_at or now
            m.save(update_fields=["open_count", "opened_at"])
        else:
            m.click_count += 1
            m.clicked_at = m.clicked_at or now
            if not m.opened_at:
                m.opened_at, m.open_count = now, max(m.open_count, 1)
            m.save(update_fields=["click_count", "clicked_at", "opened_at", "open_count"])


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
    if _ours(addr) or is_auto_reply(r.subject) or persona == logins.FROM_LOCAL:
        # Our own all-Bcc emails arrive here too; so do out-of-office answers and answers to login emails.
        r.status = "ignored"
    match_email(r)
    r.save()
    audit("inbound", f"{addr} -> {persona or '?'}: {r.subject[:80]}")
    notices.reply(r)
    from . import npc
    npc.on_response(r)  # answered once its content is fetched
    return r


AUTO_SUBJECT = re.compile(r"^\s*(automatic reply|auto(matic)?[- ]?reply|out of (the )?office|autoreply|r[ée]ponse automatique|abwesenheit)\b", re.I)


def is_auto_reply(subject, headers=None):
    """An out-of-office or other automatic answer (RFC 3834 Auto-Submitted, the usual X- headers,
    or an Outlook-style subject): kept, but not a team's reply (no AI persona answer, no figures)."""
    if AUTO_SUBJECT.match(subject or ""):
        return True
    items = headers.items() if isinstance(headers, dict) else (
        [(h.get("name"), h.get("value")) for h in headers if isinstance(h, dict)] if isinstance(headers, list) else [])
    h = {str(k or "").lower(): str(v or "").strip().lower() for k, v in items}
    return (h.get("auto-submitted", "no") not in ("", "no") or "x-autoreply" in h or "x-autorespond" in h
            or h.get("precedence") == "auto_reply")


def _ignore_auto(r):
    r.status = "ignored"
    from .models import NpcReply
    NpcReply.objects.filter(response=r, status="queued").update(status="skipped", note="An automatic reply (out of office).")


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
        if r.status != "ignored" and is_auto_reply(r.subject, r.headers):
            _ignore_auto(r)
    except Exception as e:  # keep the row; Sync retries
        log.exception("fetch of received email failed")
        r.error = f"{type(e).__name__}: {e}"
    # Only what the fetch filled in: ExCon may have matched or reviewed it in the meantime.
    r.save(update_fields=["text", "html", "headers", "message_id_header", "subject", "fetched", "error"]
           + (["status"] if r.status == "ignored" else []))
    if r.fetched:
        notices.reply(r)  # now with its opening words
        from . import calendar
        try:  # a guest's iMIP answer to one of our invitations (SIMEX Calendar)
            calendar.email_reply(r)
        except Exception:
            log.exception("calendar reply in received email %s failed", r.pk)
        from . import desk
        desk.on_response(r)


# Webhook fetches go through a few worker threads per process: a burst of replies (every team
# answering the same inject) must not open a database connection each (the cluster has few).
FETCH_WORKERS = 3
_fetch_queue = queue.Queue()
_fetchers = []
_fetchers_lock = threading.Lock()


def _fetch_worker():
    while True:
        response_id = _fetch_queue.get()
        try:
            fetch(response_id)
        except Exception:
            log.exception("fetch of received email %s failed", response_id)
        finally:
            connection.close()


def fetch_async(response_id):
    with _fetchers_lock:
        while len(_fetchers) < FETCH_WORKERS:
            t = threading.Thread(target=_fetch_worker, daemon=True, name=f"fetch-{len(_fetchers)}")
            t.start()
            _fetchers.append(t)
    _fetch_queue.put(response_id)


def catch_up():
    """Scheduler worker, every minute or so: pick up received email whose webhook never came
    (or came while the web was restarting) and retry content fetches that failed, so no reply
    waits on someone pressing Sync. Only with Resend (the log mailer has nothing to list)."""
    if mailer.get().name != "resend":
        return 0, 0
    # Only recent mail: older items are from before (deleted replies must not come back).
    return sync(limit=50, since=timezone.now() - timedelta(hours=2))


def sync(limit=50, since=None):
    """Add received emails we have no Response for (only those received after since, if
    given) and fetch the content of any not fetched yet."""
    m_ = mailer.get()
    added = 0
    floor = conf.get("reset_at")  # Settings → Start afresh: what came before is gone for good
    if floor:
        try:
            floor = datetime.fromisoformat(floor)
            since = max(since, floor) if since else floor
        except ValueError:
            pass
    for item in m_.list_received(limit=limit):
        rid = item.get("id")
        if since and _when(item.get("created_at")) < since:
            continue
        if rid and not Response.objects.filter(resend_id=rid).exists():
            _new_email(rid, item)
            added += 1
    todo = list(Response.objects.filter(channel="email", fetched=False)
                .exclude(received_at__gt=timezone.now() - timedelta(seconds=30))  # the webhook's own fetch
                .values_list("id", flat=True))
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
    """(inject id, team id or None) -> earliest delivery time (to the team: not its leader's copy)."""
    from django.db.models import Min
    out = {}
    for row in (Message.objects.filter(status="sent", kind__in=kinds).exclude(person__role="tl")
                .values("inject_id", "team_id").annotate(at=Min("sent_at"))):
        out[(row["inject_id"], row["team_id"])] = row["at"]
    return out


def response_minutes(r):
    m = r.message
    if m and m.sent_at:
        return max(0, int((r.received_at - m.sent_at) / timedelta(minutes=1)))
    return None


# --- conversations (Replies tab) ---------------------------------------------------------------

def thread_key(r):
    """Which back-and-forth a Response belongs to: the in-app thread, or for email the same
    team (or sender), persona and subject without Re:/Fwd:."""
    if r.channel == "inapp" and r.mail_id and r.mail.thread:
        return f"m:{r.mail.thread}"
    who = f"t{r.team_id}" if r.team_id else (r.from_addr or "").lower()
    return f"e:{who}:{r.persona or ''}:{clean_subject(r.subject)}"


def conversation(r):
    """Everything in r's back-and-forth, oldest first: what the persona sent that started it
    (an inject or an earlier answer), the team's messages, ExCon's answers. Each item is
    (kind, at, obj) with kind "inject" | "team" | "answer"."""
    key = thread_key(r)
    if key.startswith("m:"):
        reps = list(Response.objects.filter(mail__thread=r.mail.thread, team_id=r.team_id)
                    .select_related("mail", "person", "team", "inject", "message"))
    else:
        qs = Response.objects.filter(channel="email", persona=r.persona)
        qs = qs.filter(team_id=r.team_id) if r.team_id else qs.filter(team__isnull=True, from_addr__iexact=r.from_addr)
        reps = [x for x in qs.select_related("mail", "person", "team", "inject", "message") if thread_key(x) == key]
    items = [("team", x.received_at, x) for x in reps]
    answers = Message.objects.filter(kind="reply", response__in=reps)
    items += [("answer", m.sent_at or m.created_at, m) for m in answers]
    answer_ids = {m.id for m in answers}
    starts = {x.message for x in reps if x.message and x.message.kind != "reply" and x.message.id not in answer_ids}
    # The original delivery and its follow-ups, whichever of them the team answered.
    roots = {m.parent or m for m in starts}
    starts |= roots | set(Message.objects.filter(kind="followup", parent__in=roots).exclude(status__in=["pending", "failed"]))
    items += [("inject", m.sent_at or m.created_at, m) for m in starts]
    items.sort(key=lambda it: (it[1], {"inject": 0, "team": 1, "answer": 2}[it[0]]))
    return items

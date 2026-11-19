"""Sending: the background scheduler loop and the live / test / one-off send paths.

Every email is written to the messages table (status pending) before it is handed to
Resend with an idempotency key, so a restart mid-send resumes without duplicates.
"""

import logging
import threading
import time
import uuid
from datetime import timedelta

from sqlalchemy import update

from . import config, mailer, render, settings
from .db import Attachment, Inject, Message, SessionLocal, TestRun, TestRunItem, audit, utcnow

log = logging.getLogger("simex.scheduler")
BCC_CHUNK = 45  # stay under Resend's 50-recipients-per-email limit
state = {"last_tick": None, "last_error": None}
_busy = set()
_busy_lock = threading.Lock()


def _claim_busy(key):
    with _busy_lock:
        if key in _busy:
            return False
        _busy.add(key)
        return True


def _release(key):
    with _busy_lock:
        _busy.discard(key)


def chunk(drafts):
    """Split drafts whose recipient count is over the Resend limit (Bcc lists)."""
    out = []
    for d in drafts:
        if len(d.to) + len(d.cc) + len(d.bcc) <= 50:
            out.append(d)
            continue
        bccs = d.bcc
        for i in range(0, len(bccs), BCC_CHUNK):
            part = render.Draft(**{**d.__dict__, "bcc": bccs[i:i + BCC_CHUNK]})
            out.append(part)
    return out


def _message(inj, d, kind, idem_key, run=None):
    return Message(inject_id=inj.id, run_id=run.id if run else None, kind=kind, target=d.target,
                   idem_key=idem_key, from_addr=d.from_addr, reply_to=d.reply_to, to=d.to, cc=d.cc,
                   bcc=d.bcc, original_to=d.original_to, subject=d.subject, text=d.text, html=d.html,
                   attachment_ids=d.attachment_ids)


def deliver(s, msgs, dry_run=False, respect_pause=True):
    """Send pending messages one by one, committing after each. Stops if sending is paused
    (the rest stay pending and the next tick resumes them)."""
    m_ = mailer.get()
    for m in msgs:
        if m.status != "pending":
            continue
        if respect_pause and settings.flag(s, "paused"):
            break
        if not m.to:
            m.status, m.error = "failed", m.error or "No recipients."
            s.commit()
            continue
        if dry_run:
            m.status, m.sent_at = "dry", utcnow()
            s.commit()
            continue
        atts = s.query(Attachment).filter(Attachment.id.in_(m.attachment_ids or [])).all() if m.attachment_ids else []
        try:
            m.resend_id = m_.send(mailer.payload(m, atts), m.idem_key)
            m.status, m.sent_at, m.error = "sent", utcnow(), None
        except mailer.SendError as e:
            m.status, m.error = "failed", str(e)
        except Exception as e:  # never leave a message half-processed
            log.exception("send failed")
            m.status, m.error = "failed", f"{type(e).__name__}: {e}"
        s.commit()


def _outcome(msgs):
    if any(m.status == "pending" for m in msgs):
        return "sending", None
    ok = [m for m in msgs if m.status in ("sent", "dry")]
    errors = "; ".join(sorted({m.error for m in msgs if m.status == "failed" and m.error}))
    if msgs and len(ok) == len(msgs):
        return "sent", None
    return ("partial" if ok else "failed"), errors or "Nothing to send."


# --- live -------------------------------------------------------------------------------

def process_live(inject_id):
    key = f"live-{inject_id}"
    if not _claim_busy(key):
        return
    try:
        with SessionLocal() as s:
            inj = s.get(Inject, inject_id)
            if not inj or inj.status != "sending":
                return
            prefix = f"live-{inj.id}-a{inj.attempt}-"
            msgs = s.query(Message).filter(Message.idem_key.like(prefix + "%")).order_by(Message.id).all()
            if not msgs:
                drafts, warnings = render.build(s, inj, settings.get_all(s))
                for i, d in enumerate(chunk(drafts)):
                    removed = render.strip_placeholders(d)
                    m = _message(inj, d, "live", f"{prefix}{i}")
                    if removed and not d.to:
                        m.error = "Only placeholder addresses: " + ", ".join(removed)
                    s.add(m)
                    msgs.append(m)
                if not msgs:
                    inj.status, inj.last_error = "failed", "; ".join(warnings) or "No emails to send."
                    audit(s, "live_failed", f"{inj.mel_id}: {inj.last_error}")
                    s.commit()
                    return
                s.commit()
            deliver(s, msgs)
            status, err = _outcome(msgs)
            if status != "sending":
                inj.status, inj.last_error, inj.sent_at = status, err, utcnow()
                audit(s, f"live_{status}", f"{inj.mel_id}: {sum(m.status == 'sent' for m in msgs)}/{len(msgs)} emails"
                      + (f"; {err}" if err else ""))
                s.commit()
    finally:
        _release(key)


def start_live(s, inj, retry_failed=False):
    """Manual Send now / Retry. Returns an error message or None; the send runs in a thread."""
    if settings.flag(s, "paused"):
        return "Sending is paused (Settings)."
    if not settings.flag(s, "live_armed"):
        return "Live sending is not armed (Settings)."
    if retry_failed:
        if inj.status not in ("partial", "failed"):
            return "Nothing to retry."
        prefix = f"live-{inj.id}-a{inj.attempt}-"
        failed = s.query(Message).filter(Message.idem_key.like(prefix + "%"), Message.status == "failed").all()
        if inj.status == "failed" and not failed:
            # Failed before any email was built (e.g. no recipients): start a fresh attempt.
            inj.attempt += 1
        for m in failed:
            m.status, m.error = "pending", None
            m.idem_key = f"{m.idem_key}-r{uuid.uuid4().hex[:6]}"
    elif inj.status in ("sent", "partial", "sending"):
        return f"Already {inj.status}."
    else:
        inj.attempt += 1
    inj.status = "sending"
    audit(s, "live_send_now" if not retry_failed else "live_retry", inj.mel_id)
    s.commit()
    threading.Thread(target=process_live, args=(inj.id,), daemon=True).start()
    return None


# --- tests -------------------------------------------------------------------------------

def process_test_item(item_id):
    key = f"test-{item_id}"
    if not _claim_busy(key):
        return
    try:
        with SessionLocal() as s:
            item = s.get(TestRunItem, item_id)
            if not item or item.status != "sending":
                return
            run, inj = item.run, item.inject
            prefix = f"test-{run.id}-{item.id}-"
            msgs = s.query(Message).filter(Message.idem_key.like(prefix + "%")).order_by(Message.id).all()
            if not msgs:
                drafts, warnings = render.build(s, inj, settings.get_all(s))
                for i, d in enumerate(chunk(drafts)):
                    render.redirect(d, run.redirect, label=f"TEST {run.name}")
                    m = _message(inj, d, "test", f"{prefix}{i}", run=run)
                    s.add(m)
                    msgs.append(m)
                if not msgs:
                    item.status, item.error = "failed", "; ".join(warnings) or "No emails to send."
                    s.commit()
                    _finish_run(s, run)
                    return
                s.commit()
            deliver(s, msgs, dry_run=run.dry_run)
            status, err = _outcome(msgs)
            if status != "sending":
                item.status, item.error, item.sent_at = status, err, utcnow()
                s.commit()
                _finish_run(s, run)
    finally:
        _release(key)


def _finish_run(s, run):
    if run.status == "active" and all(i.status not in ("pending", "sending") for i in run.items):
        run.status = "done"
        audit(s, "test_run_done", run.name)
        s.commit()


def send_oneoff(s, inj, address, all_targets=False):
    """Send test: the inject as it would go out now, to one address. Synchronous."""
    drafts, warnings = render.build(s, inj, settings.get_all(s))
    if not drafts:
        return [], warnings
    drafts = drafts if all_targets else drafts[:1]
    msgs = []
    for d in drafts:
        render.redirect(d, address, label="TEST")
        m = _message(inj, d, "oneoff", f"oneoff-{inj.id}-{uuid.uuid4().hex}")
        s.add(m)
        msgs.append(m)
    audit(s, "send_test", f"{inj.mel_id} -> {address} ({len(msgs)})")
    s.commit()
    deliver(s, msgs, respect_pause=False)
    return msgs, warnings


def create_test_run(s, name, injects, start, redirect, dry_run, factor=None, interval=None):
    """Copy injects into a test run starting at `start` (UTC): gaps between their send times
    divided by `factor`, or a fixed `interval` in seconds between consecutive injects."""
    injects = sorted(injects, key=lambda i: (i.send_at is None, i.send_at or start, i.seq))
    run = TestRun(name=name, redirect=redirect, dry_run=dry_run,
                  description=f"{len(injects)} injects, " + (f"time / {factor:g}" if factor else f"every {interval:g} s"))
    s.add(run)
    s.flush()
    first = next((i.send_at for i in injects if i.send_at), None)
    for n, inj in enumerate(injects):
        if factor and inj.send_at and first:
            due = start + (inj.send_at - first) / factor
        else:
            due = start + timedelta(seconds=(interval or 60) * n)
        s.add(TestRunItem(run_id=run.id, inject_id=inj.id, due_at=due))
    audit(s, "test_run_created", f"{name}: {run.description}, redirect {redirect}" + (", dry run" if dry_run else ""))
    s.commit()
    return run


# --- loop ----------------------------------------------------------------------------------

def tick():
    now = utcnow()
    to_live, to_test = [], []
    with SessionLocal() as s:
        grace = timedelta(minutes=float(settings.get(s, "grace_minutes") or 10))
        overdue = s.query(Inject).filter(Inject.status == "scheduled", Inject.send_at.is_not(None),
                                         Inject.send_at < now - grace).all()
        for inj in overdue:
            inj.status = "overdue"
            audit(s, "overdue", f"{inj.mel_id} (due {inj.send_at:%Y-%m-%d %H:%M} UTC)")
        s.commit()
        if settings.flag(s, "paused"):
            return
        if settings.flag(s, "live_armed"):
            due = s.query(Inject.id).filter(Inject.status == "scheduled", Inject.send_at.is_not(None),
                                            Inject.send_at <= now).order_by(Inject.send_at, Inject.seq).all()
            for (iid,) in due:
                claimed = s.execute(update(Inject).where(Inject.id == iid, Inject.status == "scheduled")
                                    .values(status="sending", attempt=Inject.attempt + 1)).rowcount
                if claimed:
                    audit(s, "live_due", str(iid))
            s.commit()
            to_live = [iid for (iid,) in s.query(Inject.id).filter(Inject.status == "sending").all()]
        due_items = (s.query(TestRunItem.id).join(TestRun)
                     .filter(TestRun.status == "active", TestRunItem.status == "pending", TestRunItem.due_at <= now)
                     .order_by(TestRunItem.due_at).all())
        for (tid,) in due_items:
            s.execute(update(TestRunItem).where(TestRunItem.id == tid, TestRunItem.status == "pending")
                      .values(status="sending"))
        s.commit()
        to_test = [tid for (tid,) in s.query(TestRunItem.id).join(TestRun)
                   .filter(TestRun.status == "active", TestRunItem.status == "sending").all()]
    for iid in to_live:
        process_live(iid)
    for tid in to_test:
        process_test_item(tid)


def loop(stop: threading.Event):
    log.info("scheduler started (tick %ss, mailer %s)", config.TICK_SECONDS, mailer.get().name)
    while not stop.is_set():
        try:
            tick()
            state["last_error"] = None
        except Exception as e:
            log.exception("scheduler tick failed")
            state["last_error"] = f"{type(e).__name__}: {e}"
        state["last_tick"] = utcnow()
        stop.wait(config.TICK_SECONDS)


def start():
    stop = threading.Event()
    t = threading.Thread(target=loop, args=(stop,), daemon=True, name="scheduler")
    t.start()
    return stop

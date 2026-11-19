"""Delivery: the scheduler tick and the live / test / one-off / answer send paths, for both
channels.

Every delivery is written as a Message (status pending) before anything happens. Each
Message is then claimed (pending -> sending, a conditional UPDATE, safe across processes)
and delivered: in-app by creating the Mail in the same transaction, by email by handing it
to Resend with its idempotency key. A crash can therefore never duplicate an in-app item,
and a resumed email send is deduplicated by Resend.
"""

import email.utils
import logging
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.utils import timezone

from . import conf, mailer, render
from .models import (Attachment, Inject, Mail, MailAttachment, Message, Person, Team, TestRun, TestRunItem,
                     audit)

log = logging.getLogger("simex.delivery")
BCC_CHUNK = 45  # stay under Resend's 50-recipients-per-email limit
STALE_SENDING = timedelta(minutes=2)


def test_team():
    t, _ = Team.objects.get_or_create(code="test", defaults={"name": "Test team", "is_test": True, "sort": 99})
    if not t.is_test:
        Team.objects.filter(pk=t.pk).update(is_test=True)
    return t


def chunk(drafts):
    """Split email drafts whose recipient count is over the Resend limit (Bcc lists)."""
    out = []
    for d in drafts:
        if d.channel != "email" or len(d.to) + len(d.cc) + len(d.bcc) <= 50:
            out.append(d)
            continue
        for i in range(0, len(d.bcc), BCC_CHUNK):
            out.append(render.Draft(**{**d.__dict__, "bcc": d.bcc[i:i + BCC_CHUNK]}))
    return out


def new_message(inj, d, kind, idem_key, run=None):
    token = secrets.token_hex(4)
    email_reply = d.channel == "email" and not d.reply_to
    return Message(inject=inj if inj and inj.pk else None, run=run, kind=kind, channel=d.channel,
                   target=d.target, idem_key=idem_key, from_addr=d.from_addr,
                   reply_to=(d.reply_to or f"{d.reply_local}+{token}@{settings.MAIL_DOMAIN}") if d.channel == "email" else None,
                   to=d.to if d.channel == "email" else [d.to_label], cc=d.cc, bcc=d.bcc, original_to=d.original_to,
                   subject=d.subject, text=d.text, html=d.html, attachment_ids=d.attachment_ids,
                   team_id=d.team_id, person_id=d.person_id, reply_token=token if email_reply else None,
                   inbox_team_id=d.inbox_team_id, banner=d.banner or "", body=d.body, footer=d.footer)


def _put_mail(m):
    """In-app: the Mail a Message delivers (called inside the claiming transaction)."""
    name, addr = email.utils.parseaddr(m.from_addr)
    parent = m.response.mail if m.response_id and m.response and m.response.mail_id else None
    mail = Mail.objects.create(
        team_id=m.inbox_team_id, folder="inbox", thread=m.thread or (parent.thread if parent else ""),
        from_name=name, from_addr=addr, to_label=(m.to or [""])[0], to_person_id=m.person_id,
        persona=addr.split("@")[0], subject=m.subject, body=m.body, footer=m.footer, banner=m.banner,
        message=m, inject_id=m.inject_id, in_reply_to=parent, is_test=m.kind in ("test", "oneoff"))
    for a in Attachment.objects.filter(id__in=m.attachment_ids or []):
        MailAttachment.objects.create(mail=mail, attachment=a, filename=a.filename, content_type=a.content_type,
                                      size=a.size)
    return mail


def deliver(msgs, dry_run=False, respect_pause=True):
    """Deliver pending messages one by one. Stops if sending is paused (the rest stay
    pending and the next tick resumes them)."""
    m_ = None
    for m in msgs:
        m.refresh_from_db()
        if m.status != "pending":
            continue
        if respect_pause and conf.flag("paused"):
            break
        if m.channel == "inapp":
            with transaction.atomic():
                if not Message.objects.filter(pk=m.pk, status="pending").update(status="sending", claimed_at=timezone.now()):
                    continue
                if dry_run:
                    Message.objects.filter(pk=m.pk).update(status="dry", sent_at=timezone.now())
                elif not m.inbox_team_id:
                    Message.objects.filter(pk=m.pk).update(status="failed", error="No inbox.")
                else:
                    _put_mail(m)
                    Message.objects.filter(pk=m.pk).update(status="sent", sent_at=timezone.now(),
                                                           delivery="delivered", delivery_at=timezone.now())
            continue
        # Email
        if not Message.objects.filter(pk=m.pk, status="pending").update(status="sending", claimed_at=timezone.now()):
            continue
        if not m.to:
            Message.objects.filter(pk=m.pk).update(status="failed", error=m.error or "No recipients.")
            continue
        if dry_run:
            Message.objects.filter(pk=m.pk).update(status="dry", sent_at=timezone.now())
            continue
        m_ = m_ or mailer.get()
        atts = list(Attachment.objects.filter(id__in=m.attachment_ids or []))
        try:
            rid = m_.send(mailer.payload(m, atts, m.extra_headers), m.idem_key)
            Message.objects.filter(pk=m.pk).update(status="sent", sent_at=timezone.now(), error=None, resend_id=rid)
        except mailer.SendError as e:
            Message.objects.filter(pk=m.pk).update(status="failed", error=str(e))
        except Exception as e:  # never leave a message half-processed
            log.exception("send failed")
            Message.objects.filter(pk=m.pk).update(status="failed", error=f"{type(e).__name__}: {e}")
    for m in msgs:
        m.refresh_from_db()


def _outcome(msgs):
    if any(m.status in ("pending", "sending") for m in msgs):
        return "sending", None
    ok = [m for m in msgs if m.status in ("sent", "dry")]
    errors = "; ".join(sorted({m.error for m in msgs if m.status == "failed" and m.error}))
    if msgs and len(ok) == len(msgs):
        return "sent", None
    return ("partial" if ok else "failed"), errors or "Nothing to send."


def _create_messages(build_fn, prefix):
    """Create the messages of one attempt; if another process just did, use theirs."""
    msgs = list(Message.objects.filter(idem_key__startswith=prefix).order_by("id"))
    if msgs:
        return msgs, []
    drafts, warnings = build_fn()
    try:
        with transaction.atomic():
            for i, (d, extra) in enumerate(drafts):
                m = extra(d, f"{prefix}{i}")
                m.save()
                msgs.append(m)
    except IntegrityError:
        msgs = list(Message.objects.filter(idem_key__startswith=prefix).order_by("id"))
    return msgs, warnings


# --- live ----------------------------------------------------------------------------------

def process_live(inject_id):
    inj = Inject.objects.filter(pk=inject_id).first()
    if not inj or inj.status != "sending":
        return
    prefix = f"live-{inj.id}-a{inj.attempt}-"

    def build():
        drafts, warnings = render.build(inj, conf.get_all())
        out = []
        for d in chunk(drafts):
            removed = render.strip_placeholders(d) if d.channel == "email" else []

            def make(d, key, removed=removed):
                m = new_message(inj, d, "live", key)
                if removed and not d.to:
                    m.error = "Only placeholder addresses: " + ", ".join(removed)
                return m
            out.append((d, make))
        return out, warnings

    msgs, warnings = _create_messages(build, prefix)
    if not msgs:
        inj.status, inj.last_error = "failed", "; ".join(warnings) or "Nothing to send."
        inj.save(update_fields=["status", "last_error"])
        audit("live_failed", f"{inj.mel_id}: {inj.last_error}")
        return
    deliver(msgs)
    status, err = _outcome(msgs)
    if status != "sending":
        Inject.objects.filter(pk=inj.pk, status="sending").update(status=status, last_error=err, sent_at=timezone.now())
        audit(f"live_{status}", f"{inj.mel_id}: {sum(m.status == 'sent' for m in msgs)}/{len(msgs)} deliveries"
              + (f"; {err}" if err else ""))


def start_live(inj, retry_failed=False, user=""):
    """Manual Send now / Retry: marks the inject for sending; the scheduler (or the caller,
    with process_live) delivers it. Returns an error message or None."""
    if conf.flag("paused"):
        return "Sending is paused (Settings)."
    if not conf.flag("live_armed"):
        return "Live sending is not armed (Settings)."
    if retry_failed:
        if inj.status not in ("partial", "failed"):
            return "Nothing to retry."
        prefix = f"live-{inj.id}-a{inj.attempt}-"
        failed = list(Message.objects.filter(idem_key__startswith=prefix, status="failed"))
        if inj.status == "failed" and not failed:
            inj.attempt += 1
        for m in failed:
            m.status, m.error = "pending", None
            m.idem_key = f"{m.idem_key}-r{uuid.uuid4().hex[:6]}"
            m.save(update_fields=["status", "error", "idem_key"])
    elif inj.status in ("sent", "partial", "sending"):
        return f"Already {inj.status}."
    else:
        inj.attempt += 1
    inj.status = "sending"
    inj.save(update_fields=["status", "attempt"])
    audit("live_retry" if retry_failed else "live_send_now", inj.mel_id, user)
    return None


# --- tests ----------------------------------------------------------------------------------

def _test_draft(d, run_or_label, redirect_pattern):
    label = f"TEST {run_or_label}" if run_or_label else "TEST"
    if d.channel == "inapp":
        return render.redirect_inapp(d, test_team(), label=label)
    return render.redirect(d, redirect_pattern, label=label)


def process_test_item(item_id):
    item = TestRunItem.objects.select_related("run", "inject").filter(pk=item_id).first()
    if not item or item.status != "sending":
        return
    run, inj = item.run, item.inject
    prefix = f"test-{run.id}-{item.id}-"

    def build():
        drafts, warnings = render.build(inj, conf.get_all())
        out = []
        for d in chunk(drafts):
            _test_draft(d, run.name, run.redirect)
            out.append((d, lambda d, key: new_message(inj, d, "test", key, run=run)))
        return out, warnings

    msgs, warnings = _create_messages(build, prefix)
    if not msgs:
        TestRunItem.objects.filter(pk=item.pk).update(status="failed", error="; ".join(warnings) or "Nothing to send.")
        _finish_run(run)
        return
    deliver(msgs, dry_run=run.dry_run)
    status, err = _outcome(msgs)
    if status != "sending":
        TestRunItem.objects.filter(pk=item.pk).update(status=status, error=err, sent_at=timezone.now())
        _finish_run(run)


def _finish_run(run):
    run.refresh_from_db()
    if run.status == "active" and not run.items.filter(status__in=["pending", "sending"]).exists():
        run.status = "done"
        run.save(update_fields=["status"])
        audit("test_run_done", run.name)


def send_oneoff(inj, address="", all_targets=False, user=""):
    """Send test: the inject as it would go out now. In-app -> the Test team's inbox; email
    -> one address. Synchronous."""
    drafts, warnings = render.build(inj, conf.get_all())
    if not drafts:
        return [], warnings
    drafts = drafts if all_targets else drafts[:1]
    msgs = []
    for d in drafts:
        _test_draft(d, "", address)
        m = new_message(inj, d, "oneoff", f"oneoff-{inj.id}-{uuid.uuid4().hex}")
        m.save()
        msgs.append(m)
    audit("send_test", f"{inj.mel_id} -> {'Test team' if inj.channel == 'inapp' else address} ({len(msgs)})", user)
    deliver(msgs, respect_pause=False)
    return msgs, warnings


def create_test_run(name, injects, start, redirect, dry_run, factor=None, interval=None, user=""):
    injects = sorted(injects, key=lambda i: (i.send_at is None, i.send_at or start, i.seq))
    run = TestRun.objects.create(
        name=name, redirect=redirect, dry_run=dry_run,
        description=f"{len(injects)} injects, " + (f"time / {factor:g}" if factor else f"every {interval:g} s"))
    first = next((i.send_at for i in injects if i.send_at), None)
    for n, inj in enumerate(injects):
        if factor and inj.send_at and first:
            due = start + (inj.send_at - first) / factor
        else:
            due = start + timedelta(seconds=(interval or 60) * n)
        TestRunItem.objects.create(run=run, inject=inj, due_at=due)
    audit("test_run_created", f"{name}: {run.description}" + (", dry run" if dry_run else ""), user)
    return run


# --- ExCon answering a participant in character ----------------------------------------------

def answer(response, body, user=""):
    """Reply as the persona the participant wrote to: into their team's inbox (in-app
    thread) or by email (email thread). Returns the Message."""
    from .models import Sender
    persona = response.persona or ""
    sender = Sender.objects.filter(local_part=persona).first()
    name = (sender.display_name if sender else persona).replace('"', "'")
    cfg = conf.get_all()
    subject = response.subject if response.subject.lower().startswith("re:") else f"Re: {response.subject}"
    from_addr = f'"{name}" <{persona}@{settings.MAIL_DOMAIN}>'
    if response.channel == "inapp":
        team = response.team
        d = render.Draft(target=team.name if team else "", from_addr=from_addr, reply_to=None, to=[], cc=[], bcc=[],
                         subject=subject, body=body, footer=cfg.get("footer", ""), attachment_ids=[],
                         team_id=response.team_id, person_id=response.person_id, reply_local=persona,
                         channel="inapp", inbox_team_id=response.team_id,
                         to_label=(response.person.name if response.person else (team.name if team else "")))
        m = new_message(response.inject, d, "reply", f"reply-{response.id}-{uuid.uuid4().hex}")
        m.response = response
        m.thread = response.mail.thread if response.mail else ""
    else:
        quoted = "\n".join("> " + line for line in (response.text or "").splitlines()[:60])
        local_time = timezone.localtime(response.received_at).strftime("%a %d %b %H:%M")
        text_body = body + (f"\n\nOn {local_time}, {response.from_name or response.from_addr} wrote:\n{quoted}"
                            if quoted else "")
        d = render.Draft(target=response.team.name if response.team else response.from_addr, from_addr=from_addr,
                         reply_to=None, to=[response.from_addr], cc=[], bcc=render.split_addresses(cfg.get("excon_bcc")),
                         subject=subject, body=text_body, footer=cfg.get("footer", ""), attachment_ids=[],
                         team_id=response.team_id, person_id=response.person_id, reply_local=persona, channel="email")
        m = new_message(response.inject, d, "reply", f"reply-{response.id}-{uuid.uuid4().hex}")
        m.response = response
        if response.message_id_header:
            m.extra_headers = {"In-Reply-To": response.message_id_header, "References": response.message_id_header}
    m.save()
    audit("reply_answer", f"#{response.id} ({response.channel})", user)
    deliver([m])
    return m


# --- scheduler tick ---------------------------------------------------------------------------

def _locked(qs):
    """select_for_update(skip_locked=True) where the database supports it (Postgres)."""
    if connection.features.has_select_for_update_skip_locked:
        return qs.select_for_update(skip_locked=True)
    return qs


def tick():
    now = timezone.now()
    conf.put("scheduler_tick", now.isoformat())
    grace = timedelta(minutes=float(conf.get("grace_minutes") or 10))
    for inj in Inject.objects.filter(status="scheduled", send_at__isnull=False, send_at__lt=now - grace):
        if Inject.objects.filter(pk=inj.pk, status="scheduled").update(status="overdue"):
            audit("overdue", f"{inj.mel_id} (due {timezone.localtime(inj.send_at):%Y-%m-%d %H:%M})")
    # Messages a crashed process left mid-claim go back to pending (email: Resend's
    # idempotency key prevents a duplicate).
    Message.objects.filter(status="sending", claimed_at__lt=now - STALE_SENDING).update(status="pending")
    if conf.flag("paused"):
        return
    if conf.flag("live_armed"):
        with transaction.atomic():
            due = list(_locked(Inject.objects.filter(status="scheduled", send_at__isnull=False, send_at__lte=now)
                               .order_by("send_at", "seq")).values_list("id", flat=True))
            for iid in due:
                if Inject.objects.filter(pk=iid, status="scheduled").update(status="sending", attempt=F("attempt") + 1):
                    audit("live_due", str(iid))
        for iid in Inject.objects.filter(status="sending").values_list("id", flat=True):
            process_live(iid)
    with transaction.atomic():
        due_items = list(_locked(TestRunItem.objects.filter(run__status="active", status="pending", due_at__lte=now)
                                 .order_by("due_at")).values_list("id", flat=True))
        TestRunItem.objects.filter(id__in=due_items, status="pending").update(status="sending")
    for tid in TestRunItem.objects.filter(run__status="active", status="sending").values_list("id", flat=True):
        process_test_item(tid)


def people_of(team):
    return Person.objects.filter(team=team, active=True)

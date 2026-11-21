"""Delivery: the scheduler tick and the live / test / one-off / answer / follow-up send paths,
for both channels.

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

from . import conf, mailer, notices, push, render
from .models import (Attachment, Inject, Mail, MailAttachment, Message, MessageAttachment, Person, Team, TestRun,
                     TestRunItem, audit)

log = logging.getLogger("simex.delivery")
BCC_CHUNK = 45  # stay under Resend's 50-recipients-per-email limit
STALE_SENDING = timedelta(minutes=2)
EMAIL_FILES_MAX = 28 * 1024 * 1024  # Resend's 40 MB per email, once base64-encoded


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
                   inbox_team_id=d.inbox_team_id, inbox_owner_id=d.inbox_owner_id, banner=d.banner or "",
                   body=d.body, footer=d.footer, persona=d.reply_local or "")


def _put_mail(m):
    """In-app: the Mail a Message delivers (called inside the claiming transaction)."""
    name, addr = email.utils.parseaddr(m.from_addr)
    parent = m.response.mail if m.response_id and m.response and m.response.mail_id else None
    mail = Mail.objects.create(
        team_id=m.inbox_team_id, owner_id=m.inbox_owner_id, folder="inbox",
        thread=m.thread or (parent.thread if parent else ""),
        from_name=name, from_addr=addr, to_label=(m.to or [""])[0],
        to_person_id=None if m.inbox_owner_id else m.person_id,
        persona=m.persona or addr.split("@")[0], subject=m.subject, body=m.body, footer=m.footer, banner=m.banner,
        message=m, inject_id=m.inject_id, in_reply_to=parent, is_test=m.kind in ("test", "oneoff"))
    _attach(mail, m)
    push.notify_mail(mail)
    if not m.inbox_owner_id:
        to_members(mail)
    lead = _leader_sent(m, mail)
    if lead:  # sent as the team leader persona: the leader has it in Sent, in the same conversation
        sent = Mail.objects.create(
            team_id=m.inbox_team_id, owner=lead, folder="sent", thread=mail.thread, from_name=name, from_addr=addr,
            to_label=mail.to_label or mail.team.name, persona="", subject=m.subject, body=m.body, footer=m.footer,
            banner=m.banner, message=m, inject_id=m.inject_id, is_test=mail.is_test, read_at=timezone.now(),
            author_id=m.author_id)
        _attach(sent, m)
    return mail


# What a copy of a Mail in another mailbox keeps (the mailbox state, star, read..., starts afresh).
COPIED = ["thread", "from_name", "from_addr", "to_label", "cc_label", "to_person_id", "persona", "to_team_id", "subject",
          "body", "footer", "banner", "author_id", "message_id", "inject_id", "is_test", "recipients", "created_at"]


def copy_mail(src, **over):
    """src (a Mail) in another mailbox (over: team_id, owner, folder...: fields by their
    COPIED names), with its files (inject documents by reference)."""
    mail = Mail.objects.create(**{**{f: getattr(src, f) for f in COPIED}, "team_id": src.team_id, **over})
    for a in src.files.all():
        MailAttachment.objects.create(mail=mail, attachment=a.attachment, filename=a.filename, size=a.size,
                                      content_type=a.content_type, data=a.data)
    return mail


def to_members(mail, exclude=None):
    """Mail that just landed in a team's shared inbox also goes to its members' own mailboxes
    (Settings → personal mailboxes): every member, or only the one it is for (to_person),
    never exclude (who sent it). No push: the team inbox's already reached their devices."""
    if not conf.flag("personal_mailboxes") or mail.owner_id or mail.folder != "inbox":
        return []
    people = Person.objects.participants().filter(team_id=mail.team_id, active=True)
    if mail.to_person_id:
        people = people.filter(pk=mail.to_person_id)
    return [copy_mail(mail, owner=p, folder="inbox", in_reply_to=None) for p in people.exclude(pk=exclude)]


def _attach(mail, m):
    for a in Attachment.objects.filter(id__in=m.attachment_ids or []):
        MailAttachment.objects.create(mail=mail, attachment=a, filename=a.filename, content_type=a.content_type,
                                      size=a.size)
    for f in m.files.all():  # files ExCon uploaded to an answer: the Mail gets its own copy
        MailAttachment.objects.create(mail=mail, filename=f.filename, content_type=f.content_type, size=f.size,
                                      data=f.data)


def deliver_personal(m, people, to_label, cc_label="", recipients=None):
    """A team leader's message to some of their team's members only (not the team): it goes to
    those members' own mailboxes and the leader's Sent, at once. m is a new Message (kind
    leader or reply), saved here as sent."""
    now = timezone.now()
    m.status, m.sent_at, m.delivery, m.delivery_at = "sent", now, "delivered", now
    m.save()
    name, addr = email.utils.parseaddr(m.from_addr)
    common = dict(team_id=m.inbox_team_id, thread=m.thread or "", from_name=name, from_addr=addr, to_label=to_label,
                  cc_label=cc_label, subject=m.subject, body=m.body, footer=m.footer, message=m, inject_id=m.inject_id,
                  is_test=m.kind in ("test", "oneoff") or Team.objects.filter(pk=m.inbox_team_id, is_test=True).exists(),
                  author_id=m.author_id, recipients=recipients or {})
    sent = Mail.objects.create(**common, owner_id=m.author_id, folder="sent", read_at=now)
    _attach(sent, m)
    for p in people:
        mail = copy_mail(sent, owner=p, folder="inbox", read_at=None, persona=m.persona)
        push.notify_mail(mail)
    return sent


def _leader_sent(m, mail):
    """The team leader who has this delivery to the team inbox in Sent (it comes from the team
    leader persona of that team), or None."""
    if m.inbox_owner_id or not m.persona:
        return None
    lp = render.leader_persona(mail.team)
    return mail.team.leader() if lp and lp["local"] == m.persona else None


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
            try:
                with transaction.atomic():
                    if not Message.objects.filter(pk=m.pk, status="pending").update(status="sending",
                                                                                    claimed_at=timezone.now()):
                        continue
                    if dry_run:
                        Message.objects.filter(pk=m.pk).update(status="dry", sent_at=timezone.now())
                    elif not m.inbox_team_id:
                        Message.objects.filter(pk=m.pk).update(status="failed", error="No inbox.")
                    else:
                        _put_mail(m)
                        Message.objects.filter(pk=m.pk).update(status="sent", sent_at=timezone.now(),
                                                               delivery="delivered", delivery_at=timezone.now())
            except Exception as e:  # rolled back: mark it failed so it can be retried, and go on
                log.exception("in-app delivery of message %s failed", m.pk)
                Message.objects.filter(pk=m.pk, status="pending").update(status="failed",
                                                                         error=f"{type(e).__name__}: {e}"[:500])
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
        if m.parent_id and not m.extra_headers:
            m.extra_headers = _thread_headers(m.parent, m_)
            Message.objects.filter(pk=m.pk).update(extra_headers=m.extra_headers)
        atts = list(Attachment.objects.filter(id__in=m.attachment_ids or [])) + list(m.files.all())
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
        notices.inject(inj, "failed", inj.last_error)
        return
    deliver(msgs)
    status, err = _outcome(msgs)
    if status != "sending":
        Inject.objects.filter(pk=inj.pk, status="sending").update(status=status, last_error=err, sent_at=timezone.now())
        audit(f"live_{status}", f"{inj.mel_id}: {sum(m.status == 'sent' for m in msgs)}/{len(msgs)} deliveries"
              + (f"; {err}" if err else ""))
        if status in ("failed", "partial"):
            notices.inject(inj, status, err)


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
        build_fn = render.build_inapp if run.channel_for(inj) == "inapp" else render.build_email
        drafts, warnings = build_fn(inj, conf.get_all())
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
    audit("send_test", f"{inj.mel_id} -> {'Test team' if inj.effective_channel == 'inapp' else address} ({len(msgs)})", user)
    deliver(msgs, respect_pause=False)
    return msgs, warnings


def create_test_run(name, injects, start, redirect, dry_run, factor=None, interval=None, user="", channel=""):
    injects = sorted(injects, key=lambda i: (i.send_at is None, i.send_at or start, i.seq))
    run = TestRun.objects.create(
        name=name, redirect=redirect, dry_run=dry_run, channel=channel,
        description=f"{len(injects)} injects, " + (f"time / {factor:g}" if factor else f"every {interval:g} s"))
    first = next((i.send_at for i in injects if i.send_at), None)
    for n, inj in enumerate(injects):
        if factor and inj.send_at and first:
            due = start + (inj.send_at - first) / factor
        else:
            due = start + timedelta(seconds=(interval or 60) * n)
        TestRunItem.objects.create(run=run, inject=inj, due_at=due)
    audit("test_run_created", f"{name}: {run.description}" + (f", all {channel}" if channel else "")
          + (", dry run" if dry_run else ""), user)
    return run


# --- ExCon answering a participant in character ----------------------------------------------

def drafted(m, draft, text):
    """Record that m was sent from an AI draft, and whether ExCon changed the draft's text."""
    if draft:
        m.ai_draft = draft
        m.ai_edited = " ".join(text.split()) != " ".join(str(draft.body.get("body") or "").split())


def answer(response, body, user="", files=(), attachments=(), ai_draft=None):
    """Reply as the persona the participant wrote to: into their team's inbox (in-app
    thread) or by email (email thread), with files ExCon uploaded ((filename, content_type,
    data) tuples) and inject documents (Attachment rows). Returns the Message."""
    persona = response.persona or ""
    sender, ctx = render.sender_by_local(persona)
    team = response.team
    ctx = {**ctx, "team_name": team.name} if team else ctx
    name = ((render.fill(sender.display_name, ctx) if sender and sender.display_name else "") or persona).replace('"', "'")
    cfg = conf.get_all()
    subject = response.subject if response.subject.lower().startswith("re:") else f"Re: {response.subject}"
    from_addr = f'"{name}" <{render.persona_address(sender, persona, ctx, response.channel)}>'
    if response.channel == "inapp":
        team = response.team
        d = render.Draft(target=team.name if team else "", from_addr=from_addr, reply_to=None, to=[], cc=[], bcc=[],
                         subject=subject, body=body, footer=cfg.get("footer", ""),
                         attachment_ids=[a.id for a in attachments],
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
                         subject=subject, body=text_body, footer=cfg.get("footer", ""),
                         attachment_ids=[a.id for a in attachments],
                         team_id=response.team_id, person_id=response.person_id, reply_local=persona, channel="email")
        m = new_message(response.inject, d, "reply", f"reply-{response.id}-{uuid.uuid4().hex}")
        m.response = response
        if response.message_id_header:
            m.extra_headers = {"In-Reply-To": response.message_id_header, "References": response.message_id_header}
    drafted(m, ai_draft, body)
    with transaction.atomic():  # the files are in place before anything can deliver the Message
        m.save()
        for name, ctype, data in files:
            MessageAttachment.objects.create(message=m, filename=name[:300], content_type=ctype[:120], size=len(data),
                                             data=data)
    audit("reply_answer", f"#{response.id} ({response.channel})", user)
    deliver([m])
    return m


# --- follow-ups: more from the persona in the thread of a delivered inject ------------------

def followup_targets(inj):
    """The latest live delivery of an inject to each recipient (team inbox, person, set of
    email addresses): what a follow-up can continue."""
    out = {}
    for m in Message.objects.filter(inject=inj, kind="live", status="sent").order_by("id"):
        out[(m.channel, m.target, m.inbox_team_id, m.person_id, tuple(m.to), tuple(m.cc), tuple(m.bcc))] = m
    return sorted(out.values(), key=lambda m: m.id)


def _ctx(m):
    team = m.team or m.inbox_team
    p = m.person
    t = {"team": team.code, "team_name": team.name} if team else {"team": "", "team_name": ""}
    return {**t, "name": p.name if p else t["team_name"], "first_name": p.first_name if p else t["team_name"]}


def _thread_headers(parent, m_):
    """Email: In-Reply-To / References pointing at the original email, so mail clients show
    the follow-up in its thread. Resend reports the Message-ID it gave the original."""
    if not parent.message_id_header and parent.resend_id:
        try:
            mid = (m_.get_sent(parent.resend_id) or {}).get("message_id") or ""
        except Exception as e:
            log.warning("no Message-ID for message %s: %s", parent.id, e)
            mid = ""
        if mid:
            mid = mid if mid.startswith("<") else f"<{mid}>"
            parent.message_id_header = mid[:300]
            Message.objects.filter(pk=parent.pk).update(message_id_header=parent.message_id_header)
    if not parent.message_id_header:
        return {}
    return {"In-Reply-To": parent.message_id_header, "References": parent.message_id_header}


def send_followup(inj, parents, subject, body, attachment_ids, user="", ai_draft=None):
    """The inject's persona writes again to the recipients of some of its deliveries (a
    correction, a missing document), in each one's existing conversation. {subject} in the
    subject is the original subject; the body takes the inject placeholders. In-app is
    delivered now; email is queued for the scheduler. Returns (messages, error)."""
    if conf.flag("paused"):
        return [], "Sending is paused (Settings)."
    if not conf.flag("live_armed"):
        return [], "Live sending is not armed (Settings)."
    if not parents:
        return [], "Choose who gets the follow-up."
    atts = list(Attachment.objects.filter(id__in=attachment_ids, inject=inj))
    if any(p.channel == "email" for p in parents) and sum(a.size for a in atts) > EMAIL_FILES_MAX:
        return [], "Attachments are over Resend's 40 MB per email once encoded."
    footer = render.footer_for(inj, conf.get_all())
    batch = uuid.uuid4().hex[:12]
    msgs = []
    with transaction.atomic():
        for i, p in enumerate(parents):
            ctx = _ctx(p)
            subj = render.fill(subject, ctx).replace("{subject}", p.subject)
            text = render.fill(body, ctx)
            local = p.persona or email.utils.parseaddr(p.from_addr)[1].split("@")[0]
            common = dict(target=p.target, from_addr=p.from_addr, subject=subj, footer=footer,
                          attachment_ids=[a.id for a in atts], team_id=p.team_id, person_id=p.person_id,
                          reply_local=local)
            if p.channel == "inapp":
                d = render.Draft(**common, reply_to=None, to=[], cc=[], bcc=[], body=text, channel="inapp",
                                 inbox_team_id=p.inbox_team_id, inbox_owner_id=p.inbox_owner_id,
                                 to_label=(p.to or [""])[0])
            else:
                quoted = "\n".join("> " + line for line in (p.body or "").splitlines()[:60])
                when = timezone.localtime(p.sent_at or p.created_at).strftime("%a %d %b %H:%M")
                name = email.utils.parseaddr(p.from_addr)[0] or local
                d = render.Draft(**common, reply_to=None if p.reply_token else p.reply_to, to=list(p.to),
                                 cc=list(p.cc), bcc=list(p.bcc), channel="email",
                                 body=text + (f"\n\nOn {when}, {name} wrote:\n{quoted}" if quoted else ""))
            m = new_message(inj, d, "followup", f"followup-{inj.id}-{batch}-{i}")
            m.parent = p
            drafted(m, ai_draft, body)
            if p.channel == "inapp":
                mail = p.mails.filter(folder="inbox").first()
                m.thread = mail.thread if mail else ""
            m.save()
            msgs.append(m)
    audit("followup", f"{inj.mel_id}: {len(msgs)} ({', '.join(sorted({m.target for m in msgs}))})", user)
    deliver([m for m in msgs if m.channel == "inapp"])
    return msgs, None


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
            notices.inject(inj, "overdue")
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
            _guarded(process_live, iid)
        followups = list(Message.objects.filter(kind="followup", channel="email", status="pending").order_by("id"))
        if followups:
            _guarded(deliver, followups)
    # ExCon answers a crashed or slow web request left behind (put back to pending above).
    stale = list(Message.objects.filter(kind="reply", status="pending", claimed_at__isnull=False).order_by("id"))
    if stale:
        _guarded(deliver, stale)
    with transaction.atomic():
        due_items = list(_locked(TestRunItem.objects.filter(run__status="active", status="pending", due_at__lte=now)
                                 .order_by("due_at")).values_list("id", flat=True))
        TestRunItem.objects.filter(id__in=due_items, status="pending").update(status="sending")
    for tid in TestRunItem.objects.filter(run__status="active", status="sending").values_list("id", flat=True):
        _guarded(process_test_item, tid)


def _guarded(fn, arg):
    """One inject (or test item) that fails must not hold up every other one, tick after tick."""
    try:
        fn(arg)
    except Exception as e:
        log.exception("%s(%s) failed", fn.__name__, arg if isinstance(arg, int) else "...")
        err = f"{type(e).__name__}: {e}"[:500]
        if fn is process_live:
            inj = Inject.objects.filter(pk=arg).first()
            if not inj:
                return
            if Message.objects.filter(idem_key__startswith=f"live-{inj.id}-a{inj.attempt}-").exists():
                # Some may be delivered: keep it sending (the next tick resumes it), show why.
                Inject.objects.filter(pk=inj.pk).update(last_error=err)
            elif Inject.objects.filter(pk=inj.pk, status="sending").update(status="failed", last_error=err):
                audit("live_failed", f"{inj.mel_id}: {err}")
                notices.inject(inj, "failed", err)
        elif fn is process_test_item:
            TestRunItem.objects.filter(pk=arg, status="sending").update(status="failed", error=err)


def people_of(team):
    return Person.objects.participants().filter(team=team, active=True)

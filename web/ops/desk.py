"""The persona desk: ExCon role-players (People & logins, role desk; ExCon staff may open it
too) sharing one SIMEX Mail mailbox (Mail team None, owner None; Viewer box "desk",
/inbox/desk/) from which they write as any persona of the Senders tab but the team leader
persona, which the leaders play, to teams or to members one by one.

Its Inbox is a copy of every team message to a persona (on_response: in-app from
inbound.from_mail, by email once fetched), its Sent a copy of every delivery from a persona
(on_delivered, from delivery: injects, follow-ups, answers, the AI personas' too), each with
desk_team = the team on the other side. An in-app conversation keeps the team's thread id, so
writing in it from the desk continues the team's conversation. A reply to a team's message as
the persona it wrote to is that Response's answer (delivery.answer, kind reply, as on the
Replies tab); anything else is a Message of kind desk (no inject: kept apart from the injects'
figures). Answering a conversation as an AI persona skips that persona's queued answer to it.
"""

import email.utils
import logging
import time
import uuid
import zlib
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from . import conf, inbound, push, render
from .addresses import member_name
from .models import Mail, MailAttachment, MessageAttachment, NpcReply, Person, Response, Sender, Team, audit

log = logging.getLogger("simex.desk")


class DeskError(Exception):
    pass


# --- who the desk writes as -------------------------------------------------------------------

def personas():
    """The senders the desk writes as: the active ones, not the team leader persona."""
    return Sender.objects.filter(active=True, team_leader=False)


def colour(sender, local=""):
    """(background, text colour) of a persona on the desk: its own (Senders tab), else one
    picked from its username, so each persona keeps the same colour everywhere."""
    from .views_inbox import LABEL_COLOURS
    bg = sender.colour if sender and sender.colour in LABEL_COLOURS else ""
    if not bg:
        palette = list(LABEL_COLOURS)
        bg = palette[zlib.crc32((sender.local_part if sender else local or "?").encode()) % len(palette)]
    return bg, LABEL_COLOURS[bg]


def describe(local, team=None, memo=None):
    """A persona username as the desk shows it: {"name", "addr", "colour", "fg", "id"} (id: its
    Sender, for the sidebar filter), names filled for team."""
    key = (local, team.id if team else None)
    if memo is not None and key in memo:
        return memo[key]
    s, ctx = render.sender_by_local(local)
    ctx = {**ctx, **render.team_ctx(team)} if team else ctx
    bg, fg = colour(s, local)
    out = {"name": (render.fill(s.display_name, ctx) if s and s.display_name else "") or local,
           "addr": render.persona_address(s, local, ctx), "colour": bg, "fg": fg, "id": s.id if s else None,
           "local": local}
    if memo is not None:
        memo[key] = out
    return out


def is_leader_persona(local):
    s, _ = render.sender_by_local(local or "")
    return bool(s and s.team_leader)


def sender_for_key(key):
    """The Sender behind a desk From choice ("p:<username>", {team} unfilled), or None."""
    return personas().filter(local_part=key[2:]).first() if key and key.startswith("p:") else None


# --- copies into the desk -------------------------------------------------------------------------

def _thread_of(**q):
    return (Mail.objects.filter(team=None, owner=None, **q).exclude(thread="").order_by("id")
            .values_list("thread", flat=True).first())


def _files(desk_mail, mail=None, m=None, response=None):
    if mail is not None:
        for a in mail.files.all():
            MailAttachment.objects.create(mail=desk_mail, attachment=a.attachment, filename=a.filename, size=a.size,
                                          content_type=a.content_type, data=a.data)
    if m is not None:
        from .models import Attachment
        for a in Attachment.objects.filter(id__in=m.attachment_ids or []):
            MailAttachment.objects.create(mail=desk_mail, attachment=a, filename=a.filename,
                                          content_type=a.content_type, size=a.size)
        for f in m.files.all():
            MailAttachment.objects.create(mail=desk_mail, filename=f.filename, content_type=f.content_type,
                                          size=f.size, data=f.data)
    if response is not None:
        for a in response.attachments.all():
            if a.data is not None:  # too large to keep: on the Replies tab only as a name
                MailAttachment.objects.create(mail=desk_mail, filename=a.filename, content_type=a.content_type,
                                              size=a.size, data=a.data)


def on_delivered(m, mail=None):
    """A persona's delivery (m, a Message just sent; mail: the in-app Mail it put in a team's
    inbox) -> its copy in the desk's Sent. Not the team leader persona's, nor the copies in the
    team leaders' own mailboxes or emails to them (but the desk's own messages to a leader).
    Never stops the delivery."""
    try:
        with transaction.atomic():
            return _on_delivered(m, mail)
    except Exception:
        log.exception("desk copy of message %s failed", m.pk)
        return None


def _on_delivered(m, mail):
    to_leader = m.kind != "desk" and ((m.inbox_owner_id and m.inbox_owner.is_leader) or (m.person_id and m.person.is_leader))
    if (m.kind == "leader" or to_leader or not m.persona
            or is_leader_persona(m.persona) or Mail.objects.filter(team=None, message=m).exists()):
        return None
    name, addr = email.utils.parseaddr(m.from_addr)
    team_id = m.inbox_team_id or m.team_id
    if mail is not None:
        thread = mail.thread
    else:
        thread = ((m.kind == "desk" and m.thread) or (m.response_id and _thread_of(desk_response_id=m.response_id))
                  or (m.parent_id and _thread_of(message_id=m.parent_id)) or uuid.uuid4().hex)
    team = Team.objects.filter(pk=team_id).first() if team_id else None
    desk_mail = Mail.objects.create(
        team=None, owner=None, folder="sent", thread=thread, from_name=name, from_addr=addr, to_label=m.target,
        persona=m.persona, desk_team=team, subject=m.subject, body=m.body or "", footer=m.footer, banner=m.banner,
        message=m, inject_id=m.inject_id, is_test=m.kind in ("test", "oneoff") or bool(team and team.is_test),
        read_at=timezone.now(), author_id=m.author_id, sim_at=mail.sim_at if mail is not None else None)
    _files(desk_mail, mail=mail) if mail is not None else _files(desk_mail, m=m)
    return desk_mail


def on_response(r, personas_=None):
    """A team's message to one or more personas (r, its Response; personas_: every persona it
    went to) -> the desk's Inbox, pushed to the desk's devices. Not what was only for the team
    leader persona, an automatic answer or a team leader's own reply."""
    try:
        with transaction.atomic():
            return _on_response(r, personas_)
    except Exception:
        log.exception("desk copy of response %s failed", r.pk)
        return None


def _on_response(r, personas_):
    if r.status == "ignored" or r.match == "leader":
        return None
    to = [p for p in (personas_ or [r.persona]) if p and not is_leader_persona(p)]
    if not to or Mail.objects.filter(team=None, desk_response=r).exists():
        return None
    src = r.mail
    thread = src.thread if src else ((r.message_id and _thread_of(message_id=r.message_id)) or _email_thread(r)
                                     or uuid.uuid4().hex)
    team = r.team
    memo = {}
    body = r.text or ""
    if r.channel == "email":
        body = inbound.split_quoted(body)[0] or body
    mail = Mail.objects.create(
        team=None, owner=None, folder="inbox", thread=thread,
        from_name=(src.from_name if src else r.from_name) or r.from_addr, from_addr=r.from_addr,
        to_label=", ".join(f"{describe(p, team, memo)['name']} <{render.persona_addr(p, r.channel)}>" for p in to),
        cc_label=src.cc_label if src else ", ".join(r.cc or []), persona=to[0], desk_team=team, desk_response=r,
        subject=r.subject, body=body, inject_id=r.inject_id, is_test=r.kind == "test" or bool(team and team.is_test),
        created_at=r.received_at, author_id=r.person_id, sim_at=src.sim_at if src else None)
    _files(mail, mail=src) if src else _files(mail, response=r)
    push.notify_mail(mail)
    return mail


def _email_thread(r):
    """An emailed message the reply token didn't tie to a delivery: the desk conversation of
    the team's earlier email in the same back-and-forth (inbound.thread_key), if any."""
    key = inbound.thread_key(r)
    for prev in (Response.objects.filter(channel="email", persona=r.persona, team_id=r.team_id).exclude(pk=r.pk)
                 .order_by("-id")[:20]):
        if inbound.thread_key(prev) == key:
            t = _thread_of(desk_response=prev)
            if t:
                return t
    return None


# --- writing from the desk ------------------------------------------------------------------------

def answering(parent, team):
    """The team's message a desk message in parent's conversation answers: parent itself if it
    is that team's, else the team's latest in the conversation (a Response), or None."""
    if parent is None:
        return None
    if parent.desk_response_id and parent.desk_team_id == team.id:
        return parent.desk_response
    last = (Mail.objects.filter(team=None, owner=None, thread=parent.thread, desk_team=team, desk_response__isnull=False)
            .select_related("desk_response").order_by("-created_at", "-id").first())
    return last.desk_response if last else None


def skip_ai(response, sender, who):
    """A person answered this conversation as an AI persona: its own queued (or held) answer
    to it goes."""
    if not response.team_id:
        return 0
    return (NpcReply.objects.filter(team_id=response.team_id, thread_key=inbound.thread_key(response)[:300],
                                    npc__sender=sender, status__in=["queued", "held", "failed"])
            .update(status="skipped", note=f"Answered by {who} on the persona desk.", updated_at=timezone.now()))


def send(person, user, from_key, entries, subject, body, files=(), docs=(), parent=None):
    """Write as a persona (from_key "p:<username>") to the teams in entries (address-book
    entries), one Message each: in a conversation (parent, a desk Mail), the team's message it
    answers gets its answer, else (or as another persona) a Message of kind desk in the same
    conversation. files: (filename, content type, data); docs: inject Attachments (forwarded).
    Returns (messages, errors); raises DeskError when nothing could go."""
    s = sender_for_key(from_key)
    if not s:
        raise DeskError("Choose the persona it goes from.")
    if conf.flag("paused"):
        raise DeskError("Sending is paused (ExCon, Settings).")
    targets = []  # (team, member or None): a team's inbox, or one of its members
    for e in entries:
        if e.get("kind") == "team":
            t = Team.objects.filter(pk=int(e["key"][2:]), active=True).first()
            if t and (t, None) not in targets:
                targets.append((t, None))
        elif e.get("kind") == "member":
            p = (Person.objects.participants().filter(pk=int(e["key"][2:]), active=True, team__active=True)
                 .select_related("team").first())
            if p and (p.team, p) not in targets:
                targets.append((p.team, p))
        elif e.get("kind") == "tl":
            p = Person.objects.filter(pk=int(e["key"][2:]), role="tl", active=True, team__active=True).select_related("team").first()
            if p and (p.team, p) not in targets:
                targets.append((p.team, p))
    if not targets:
        raise DeskError("Write to one or more teams, team members or team leaders.")
    who = person.name if person else user
    msgs, errors = [], []
    from . import delivery
    for t, member in targets:
        label = _name(member, t)
        ctx = render.team_ctx(t)
        local = render.fill(s.local_part, ctx).lower()
        response = answering(parent, t)
        if response is not None and member and (response.person_id != member.id or member.is_leader):
            response = None  # another member's message (or a leader's, never a team's): written to them afresh
        if response is not None and (response.persona or "") == local and (response.channel == "email" or response.team_id):
            m = delivery.answer(response, body, user=user, files=files, attachments=docs, author=person)
        elif not conf.flag("live_armed") and not t.is_test:
            errors.append(f"{label}: live sending is not armed (ExCon, Settings).")
            continue
        else:
            m, err = _new(s, local, ctx, t, subject, body, files, docs, parent, response, person, user, member)
            if err:
                errors.append(f"{label}: {err}")
                continue
        if response is not None:
            skip_ai(response, s, who)
        if m.status == "failed":
            errors.append(f"{label}: {m.error or 'not delivered'}")
        msgs.append(m)
    if not [m for m in msgs if m.status != "failed"]:
        raise DeskError("; ".join(errors) or "Nothing was sent.")
    return msgs, errors


def _name(person, team):
    """A desk message's recipient as named in the Log and the desk's Sent."""
    if not person:
        return team.name
    return f"{team.name} leader ({person.name})" if person.is_leader else member_name(person)


def _new(s, local, ctx, team, subject, body, files, docs, parent, response, person, user, member=None):
    """A new message from the persona to a team or one of its members (kind desk), delivered
    now. In-app, a member's goes to their own mailbox only (personal mailboxes; else to the team
    inbox, for them). Returns (Message, error)."""
    from . import delivery
    cfg = conf.get_all()
    name = (render.fill(s.display_name, ctx) or local).replace('"', "'")
    channel = response.channel if response is not None else (
        "email" if conf.get("channel_mode") == "email" else "inapp")
    label = _name(member, team)
    common = dict(target=label, subject=subject, body=body, footer=cfg.get("footer", ""),
                  attachment_ids=[a.id for a in docs], team_id=team.id, reply_local=local,
                  person_id=member.id if member else None)
    thread = ""
    if channel == "inapp":
        # A member's own mailbox (personal mailboxes), a leader's always.
        own = member if member and (member.is_leader or conf.flag("personal_mailboxes")) else None
        if parent is not None and Mail.objects.filter(team=team, owner=own, thread=parent.thread).exists():
            thread = parent.thread  # the conversation goes on
        d = render.Draft(**common, from_addr=f'"{name}" <{render.persona_address(s, local, ctx)}>', reply_to=None,
                         to=[], cc=[], bcc=[], channel="inapp", inbox_team_id=team.id, to_label=label,
                         inbox_owner_id=own.id if own else None)
    else:
        if member:
            to = [member.email] if member.email else []
        else:
            to = [team.email] if team.email else [p.email for p in delivery.people_of(team) if p.email]
        to = [a for a in to if not render.PLACEHOLDER_DOMAIN.search(a)]
        if not to:
            return None, "no email address (People & logins)."
        d = render.Draft(**common, from_addr=f'"{name}" <{local}@{settings.MAIL_DOMAIN}>', reply_to=None, to=to,
                         cc=[], bcc=render.split_addresses(cfg.get("excon_bcc")), channel="email")
        thread = parent.thread if parent is not None else ""
    m = delivery.new_message(None, d, "desk", f"desk-{uuid.uuid4().hex}")
    m.author, m.thread = person, thread
    if channel == "email" and response is not None and response.message_id_header:
        m.extra_headers = {"In-Reply-To": response.message_id_header, "References": response.message_id_header}
    with transaction.atomic():  # the files are in place before anything can deliver the Message
        m.save()
        for fname, ctype, data in files:
            MessageAttachment.objects.create(message=m, filename=fname[:300], content_type=ctype[:120], size=len(data),
                                             data=data)
    audit("desk_send", f"{local} -> {team.name}: {subject[:80]}", user)
    delivery.deliver([m])
    return m, None


# --- who else is on a conversation ------------------------------------------------------------------

PRESENCE_SECONDS = 40


def presence(thread, user_id, name, replying=False):
    """Note that user_id (name) has the desk conversation thread open (replying: writing in it);
    returns the others there now: [{"name", "replying"}]."""
    key = f"desk-presence:{thread[:60]}"
    now = time.time()
    seen = {k: x for k, x in (cache.get(key) or {}).items() if now - x["at"] < PRESENCE_SECONDS}
    seen[str(user_id)] = {"name": name, "at": now, "replying": bool(replying)}
    cache.set(key, seen, PRESENCE_SECONDS * 2)
    return [{"name": x["name"], "replying": x["replying"]} for k, x in seen.items() if k != str(user_id)]


def on_now(threads, user_id=None):
    """{thread: [{"name", "replying"}]}: who else on the desk has each conversation open now
    (the Inbox rows)."""
    keys = {f"desk-presence:{t[:60]}": t for t in threads}
    now, out = time.time(), {}
    for key, seen in cache.get_many(list(keys)).items():
        others = [{"name": x["name"], "replying": x["replying"]} for k, x in (seen or {}).items()
                  if k != str(user_id) and now - x["at"] < PRESENCE_SECONDS]
        if others:
            out[keys[key]] = others
    return out


# --- what still needs an answer -----------------------------------------------------------------
#
# Many people share the desk: read or unread doesn't say whether a team is still waiting. A
# conversation is waiting when a team's latest message in it (by Mail id: an email fetched
# late counts from when the desk got it) has no persona message to that team after it, and
# nobody marked it as needing no reply since. The Inbox puts those first (ATTENTION), then
# those Claude reads as needing no reply (QUIET, ops/desk_ai.py), then follow-ups not due yet
# and conversations an AI persona is about to answer (LATER), then the rest.

ATTENTION, QUIET, LATER, REST = "attention", "quiet", "later", "rest"
SECTIONS = [(ATTENTION, "Needs attention"), (QUIET, "Probably no reply needed (SIMEX AI)"),
            (LATER, "Later"), (REST, "Answered or nothing to answer")]
URGENCY = {"high": 0, "normal": 1, "low": 2}


def conversation(thread, create=False):
    from .models import DeskConversation
    if create:
        return DeskConversation.objects.get_or_create(thread=thread)[0]
    return DeskConversation.objects.filter(thread=thread).first()


def states(threads, user=None, now=None):
    """{thread: state} of desk conversations: section (above), waiting (a team waits for an
    answer) since (its first unanswered message's time), last_in (the latest Mail id of a
    team's in it), follow ({at, note, by, due}) if a follow-up is on, owner (who took it; mine:
    the user did), npc (an AI persona's queued or held answer: {status, due_at}), triage
    (Claude's reading of the waiting message, if current), done_by (marked as needing no reply)."""
    from .models import DeskConversation
    threads = list(dict.fromkeys(threads))
    now = now or timezone.now()
    if not threads:
        return {}
    mails = {}
    for t, mid, folder, team_id, at, rid in (Mail.objects.filter(team=None, owner=None, thread__in=threads,
                                                               trashed_at__isnull=True).exclude(folder="drafts")
                                             .order_by("id").values_list("thread", "id", "folder", "desk_team_id",
                                                                         "created_at", "desk_response_id")):
        mails.setdefault(t, []).append((mid, folder, team_id, at, rid))
    convs = {c.thread: c for c in DeskConversation.objects.filter(thread__in=threads)}
    rids = [m[4] for ms in mails.values() for m in ms if m[4]]
    npc = {}
    for job in (NpcReply.objects.filter(response_id__in=rids, status__in=["queued", "writing", "held", "failed"])
                .order_by("id")):
        npc[job.response_id] = job
    out = {}
    for t in threads:
        ms, c = mails.get(t, []), convs.get(t)
        done_upto = c.done_upto if c else 0
        last_out, first_open, last_in, last_rid, open_id = {}, None, 0, None, 0
        for mid, folder, team_id, at, rid in ms:
            if folder == "sent":
                last_out[team_id] = mid
        for mid, folder, team_id, at, rid in ms:
            if folder == "inbox":
                last_in = max(last_in, mid)
                if mid > max(last_out.get(team_id, 0), done_upto):
                    if first_open is None or at < first_open:
                        first_open, open_id = at, mid
                    last_rid = rid or last_rid
        waiting = first_open is not None
        st = {"waiting": waiting, "since": first_open, "open_id": open_id, "last_in": last_in, "follow": None, "owner": "", "mine": False,
              "npc": None, "triage": None, "done_by": "", "section": REST}
        if c:
            last_sent = max((mid for mid, folder, *_ in ms if folder == "sent"), default=0)
            if c.follow_upto and last_sent <= c.follow_upto:
                st["follow"] = {"at": c.follow_at, "note": c.follow_note, "by": c.follow_by,
                                "due": c.follow_at is None or c.follow_at <= now}
            st["owner"], st["mine"] = c.owner_name, bool(user and c.owner_id and c.owner_id == user.id)
            if c.done_upto and not waiting and c.done_upto >= last_in:
                st["done_by"] = c.done_by or "the desk"
            if waiting and c.ai_status == "done" and c.ai_upto >= last_in:
                st["triage"] = {"need": c.ai_need, "urgency": c.ai_urgency, "ask": c.ai_ask, "reason": c.ai_reason}
        job = npc.get(last_rid) if waiting else None
        if job:
            st["npc"] = {"status": job.status, "due_at": job.due_at}
        if waiting and job and job.status in ("queued", "writing"):
            st["section"] = LATER  # the AI persona answers it
        elif waiting and st["triage"] and st["triage"]["need"] == "none" and not (st["follow"] and st["follow"]["due"]):
            st["section"] = QUIET
        elif waiting or (st["follow"] and st["follow"]["due"]):
            st["section"] = ATTENTION
        elif st["follow"]:
            st["section"] = LATER
        out[t] = st
    return out


def sort_key(st, latest_at):
    """Within a section: the most urgent, then the longest waiting first; later ones by when
    they come up; the rest newest first (latest_at: the row's latest message)."""
    sec = [s for s, _ in SECTIONS].index(st["section"])
    if st["section"] in (ATTENTION, QUIET):
        urg = URGENCY.get((st["triage"] or {}).get("urgency"), 1)
        when = st["since"] or (st["follow"] or {}).get("at") or latest_at
        return (sec, urg, when.timestamp())
    if st["section"] == LATER:
        when = (st["follow"] or {}).get("at") or (st["npc"] or {}).get("due_at") or latest_at
        return (sec, 0, when.timestamp())
    return (sec, 0, -latest_at.timestamp())


ACTIONS = ("take", "release", "done", "reopen", "follow", "unfollow", "triage")


def mark(threads, act, user, name, minutes=None, note=""):
    """A desk member's action on conversations (ACTIONS): take it (it's theirs) or release it;
    done (no reply needed: the teams' messages so far) or reopen; follow up (in minutes, or
    now, with a note) or stop; triage (ask Claude now). Returns how many were changed."""
    from .models import DeskConversation
    if act not in ACTIONS:
        raise DeskError("Unknown action.")
    threads = list(Mail.objects.filter(team=None, owner=None, thread__in=threads).order_by()
                   .values_list("thread", flat=True).distinct())
    now = timezone.now()
    for t in threads:
        c = conversation(t, create=True)
        if act == "take":
            c.owner, c.owner_name = user, name[:150]
        elif act == "release":
            c.owner, c.owner_name = None, ""
        elif act == "done":
            c.done_upto = (Mail.objects.filter(team=None, owner=None, thread=t, folder="inbox")
                           .order_by("-id").values_list("id", flat=True).first() or 0)
            c.done_by, c.done_at, c.follow_upto, c.follow_at, c.follow_note = name[:150], now, 0, None, ""
        elif act == "reopen":
            c.done_upto, c.done_by, c.done_at = 0, "", None
        elif act == "follow":
            c.follow_upto = (Mail.objects.filter(team=None, owner=None, thread=t).exclude(folder="drafts")
                             .order_by("-id").values_list("id", flat=True).first() or 0) or 1
            c.follow_at = now + timedelta(minutes=minutes) if minutes else None
            c.follow_note, c.follow_by = (note or "")[:300], name[:150]
        elif act == "unfollow":
            c.follow_upto, c.follow_at, c.follow_note, c.follow_by = 0, None, "", ""
        elif act == "triage":
            c.ai_status, c.ai_upto, c.ai_error = "queued", 0, ""
        c.save()
    if threads:
        audit("desk_mark", f"{act} x{len(threads)}", name)
    return len(threads)


def attention_count():
    """How many desk conversations need attention now (the desk's tab badge and side bar)."""
    threads = list(Mail.objects.filter(team=None, owner=None, folder="inbox", trashed_at__isnull=True).order_by()
                   .values_list("thread", flat=True).distinct())
    from .models import DeskConversation
    threads += list(DeskConversation.objects.filter(follow_upto__gt=0).values_list("thread", flat=True))
    return sum(1 for st in states(threads).values() if st["section"] == ATTENTION)


def tick(now=None):
    """Scheduler main loop: a Notice for ExCon for each conversation a team has waited
    desk_wait_minutes (Settings) for a persona's answer, once per message waiting; not those
    an AI persona is about to answer or Claude reads as needing no reply. Returns how many."""
    try:
        wait = int(conf.get("desk_wait_minutes") or 0)
    except ValueError:
        wait = 0
    if wait <= 0:
        return 0
    from . import notices
    now = now or timezone.now()
    threads = list(Mail.objects.filter(team=None, owner=None, folder="inbox", trashed_at__isnull=True,
                                       created_at__lte=now - timedelta(minutes=wait))
                   .order_by().values_list("thread", flat=True).distinct())
    n = 0
    for t, st in states(threads, now=now).items():
        if st["section"] != ATTENTION or not st["waiting"] or st["since"] > now - timedelta(minutes=wait):
            continue
        mail = Mail.objects.filter(pk=st["open_id"]).select_related("desk_team").first()
        if mail and notices.desk_wait(t, mail, wait, st["owner"]):
            n += 1
    return n


def prune():
    """Conversations no longer on the desk (cleared, withdrawn): their state goes."""
    from .models import DeskConversation
    DeskConversation.objects.exclude(thread__in=Mail.objects.filter(team=None, owner=None).values("thread")).delete()

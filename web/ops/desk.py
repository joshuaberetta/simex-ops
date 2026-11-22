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
    team leaders' own mailboxes or emails to them. Never stops the delivery."""
    try:
        with transaction.atomic():
            return _on_delivered(m, mail)
    except Exception:
        log.exception("desk copy of message %s failed", m.pk)
        return None


def _on_delivered(m, mail):
    if (m.kind == "leader" or (m.inbox_owner_id and m.inbox_owner.is_leader) or not m.persona or (m.person_id and m.person.is_leader)
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
        read_at=timezone.now(), author_id=m.author_id)
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
        created_at=r.received_at, author_id=r.person_id)
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
    if not targets:
        raise DeskError("Write to one or more teams or team members.")
    who = person.name if person else user
    msgs, errors = [], []
    from . import delivery
    for t, member in targets:
        label = member_name(member) if member else t.name
        ctx = render.team_ctx(t)
        local = render.fill(s.local_part, ctx).lower()
        response = answering(parent, t)
        if response is not None and member and response.person_id != member.id:
            response = None  # another member's message: this one is written to them afresh
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


def _new(s, local, ctx, team, subject, body, files, docs, parent, response, person, user, member=None):
    """A new message from the persona to a team or one of its members (kind desk), delivered
    now. In-app, a member's goes to their own mailbox only (personal mailboxes; else to the team
    inbox, for them). Returns (Message, error)."""
    from . import delivery
    cfg = conf.get_all()
    name = (render.fill(s.display_name, ctx) or local).replace('"', "'")
    channel = response.channel if response is not None else (
        "email" if conf.get("channel_mode") == "email" else "inapp")
    label = member_name(member) if member else team.name
    common = dict(target=label, subject=subject, body=body, footer=cfg.get("footer", ""),
                  attachment_ids=[a.id for a in docs], team_id=team.id, reply_local=local,
                  person_id=member.id if member else None)
    thread = ""
    if channel == "inapp":
        own = member if member and conf.flag("personal_mailboxes") else None
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

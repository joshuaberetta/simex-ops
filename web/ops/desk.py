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

import contextvars
import email.utils
import logging
import time
import uuid
import zlib
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import models, transaction
from django.utils import timezone

from . import conf, inbound, push, render
from .addresses import member_name
from .models import Mail, MailAttachment, Message, NpcReply, Person, Response, Sender, Team, audit

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


def copy_entries():
    """The personas a desk message can also be addressed to (To or Cc, beside its teams): the
    desk's personas as address-book entries, {team} unfilled. They get no mail of their own
    (the desk is every persona's mailbox): the teams see them on the message and reply all
    reaches them."""
    email_mode = conf.get("channel_mode") == "email"
    return [{"key": f"p:{s.local_part}", "name": s.display_name or s.local_part, "kind": "persona",
             "addr": f"{s.local_part}@{settings.MAIL_DOMAIN}" if email_mode else s.inapp_address,
             "org": s.organisation} for s in personas()]


def _copied(copies, sender):
    """{"to": [Sender], "cc": [Sender]} from address-book entries (copy_entries), less the
    persona writing and any no longer on the desk."""
    by = {s.local_part: s for s in personas()}
    out, seen = {"to": [], "cc": []}, {sender.local_part}
    for part in ("to", "cc"):
        for e in (copies or {}).get(part, []):
            s = by.get((e.get("key") or "")[2:]) if (e.get("key") or "").startswith("p:") else None
            if s and s.local_part not in seen:
                seen.add(s.local_part)
                out[part].append(s)
    return out


def _copy_list(copied, ctx, channel):
    """{"to"/"cc": [(key, label, email address)]} of the personas copied, {team} filled for a team."""
    out = {}
    for part, senders in copied.items():
        out[part] = []
        for s in senders:
            local = render.fill(s.local_part, ctx).lower()
            name = (render.fill(s.display_name, ctx) or local).replace('"', "'")
            addr = render.persona_address(s, local, ctx, channel)
            out[part].append((f"p:{local}", f"{name} <{addr}>", f"{local}@{settings.MAIL_DOMAIN}"))
    return out


def _show_copied(m, listed):
    """The personas copied on m, on every Mail it made (the team's, its members', the desk's
    Sent): added to To, in Cc, and in its recipients so that reply all reaches them."""
    if not (listed["to"] or listed["cc"]):
        return
    more_to = ", ".join(x[1] for x in listed["to"])
    cc = ", ".join(x[1] for x in listed["cc"])
    rec = {"to": [x[0] for x in listed["to"]], "cc": [x[0] for x in listed["cc"]]}
    for mail in Mail.objects.filter(message=m):
        mail.to_label = ", ".join(x for x in (mail.to_label, more_to) if x)
        mail.cc_label = ", ".join(x for x in (mail.cc_label, cc) if x)
        mail.recipients = {k: list((mail.recipients or {}).get(k, [])) + rec[k] for k in ("to", "cc")}
        mail.save(update_fields=["to_label", "cc_label", "recipients"])


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
                                          content_type=a.content_type, data=a.data, cid=a.cid)
    if m is not None:
        from .models import Attachment
        for a in Attachment.objects.filter(id__in=m.attachment_ids or []):
            MailAttachment.objects.create(mail=desk_mail, attachment=a, filename=a.filename,
                                          content_type=a.content_type, size=a.size)
        for f in m.files.all():
            MailAttachment.objects.create(mail=desk_mail, filename=f.filename, content_type=f.content_type,
                                          size=f.size, data=f.data, cid=f.cid)
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
        persona=m.persona, desk_team=team, subject=m.subject, body=m.body or "", html=m.body_html, footer=m.footer,
        banner=m.banner,
        message=m, inject_id=m.inject_id, is_test=m.kind in ("test", "oneoff") or bool(team and team.is_test),
        read_at=timezone.now(), author_id=m.author_id, sim_at=mail.sim_at if mail is not None else None)
    _files(desk_mail, mail=mail) if mail is not None else _files(desk_mail, m=m)
    auto_assign(thread, quiet=True)  # the owner hears when the team writes
    _told_out(m, desk_mail)
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
        subject=r.subject, body=body, html=src.html if src else "", inject_id=r.inject_id, is_test=r.kind == "test" or bool(team and team.is_test),
        created_at=r.received_at, author_id=r.person_id, sim_at=src.sim_at if src else None)
    _files(mail, mail=src) if src else _files(mail, response=r)
    push.notify_mail(mail)
    auto_assign(thread)  # before _told_in: the default owner is told the team wrote
    _told_in(mail)
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


PERSONA_KIND = "persona"  # a Message from one persona to others, on the desk (persona mail)


def send(person, user, from_key, entries, subject, body, files=(), docs=(), parent=None, idem=None, extra_files=None,
         body_html="", copies=None, npc=False, persona_idem=None):
    """Write as a persona (from_key "p:<username>") to the teams in entries (address-book
    entries), one Message each: in a conversation (parent, a desk Mail), the team's message it
    answers gets its answer, else (or as another persona) a Message of kind desk in the same
    conversation. files: (filename, content type, data[, cid]: an image in body_html, the clean
    HTML of a formatted body, ops/richtext.py); docs: inject Attachments (forwarded).
    idem(team, member): a deterministic idempotency key per recipient (SIMEX Calendar: per event,
    change and recipient; one already sent is not sent again); extra_files(team, member, channel):
    more files for that recipient (an invitation's .ics by email).
    copies: {"to": [...], "cc": [...]}, other personas it is also addressed to (copy_entries):
    shown on each team's message (by email, in its To / Cc), reply all reaching them, and
    persona mail to them: a Message of kind persona (no team) with the desk's Inbox copy (and,
    with no team, its Sent copy), in the same conversation as the teams' copies, which an AI
    persona in To answers (npc.on_persona_mail). With no team it needs no live arming.
    npc: an AI persona wrote it (Message.npc); persona_idem: the persona Message's key.
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
    copied = _copied(copies, s)
    if not targets and not (copied["to"] or copied["cc"]):
        raise DeskError("Write to one or more teams, team members, team leaders or other personas.")
    who = person.name if person else user
    actor = person.user if person and person.user_id else get_user_model().objects.filter(username=user).first()
    token = ACTOR.set(actor)  # those on the conversation hear of it, but not the sender (_told_out)
    try:
        return _send(s, targets, subject, body, files, docs, parent, person, user, who, idem, extra_files, body_html,
                     copied, npc, persona_idem)
    finally:
        ACTOR.reset(token)


def _send(s, targets, subject, body, files, docs, parent, person, user, who, idem=None, extra_files=None, body_html="",
          copied=None, npc=False, persona_idem=None):
    msgs, errors = [], []
    from . import delivery
    copied = copied or {"to": [], "cc": []}
    to_personas = [x for x in copied["to"] + copied["cc"] if "{" not in x.local_part]
    # Persona mail: one conversation on the desk, the teams' copies in it too (their own thread
    # id is the same), so the personas' answers and the teams' replies all follow it.
    thread = (parent.thread if parent is not None else uuid.uuid4().hex) if to_personas else ""
    for t, member in targets:
        key = idem(t, member) if idem else None
        done = Message.objects.filter(idem_key=key).first() if key else None
        if done is not None:  # sent already (the same change to the same recipient): never twice
            msgs.append(done)
            continue
        label = _name(member, t)
        ctx = render.team_ctx(t)
        local = render.fill(s.local_part, ctx).lower()
        response = answering(parent, t)
        if response is not None and member and (response.person_id != member.id or member.is_leader):
            response = None  # another member's message (or a leader's, never a team's): written to them afresh
        channel = response.channel if response is not None else (
            "email" if conf.get("channel_mode") == "email" else "inapp")
        listed = _copy_list(copied or {"to": [], "cc": []}, ctx, channel)
        by_email = {"to": [x[2] for x in listed["to"]], "cc": [x[2] for x in listed["cc"]]}
        if response is not None and (response.persona or "") == local and (response.channel == "email" or response.team_id):
            from . import calendar
            more = list(extra_files(t, member, response.channel)) if extra_files else []
            m = delivery.answer(response, body, user=user, files=list(files) + more, attachments=docs, author=person,
                                subject=subject if calendar.SENDING.get() else None,  # an invitation's answer
                                idem_key=key, body_html=body_html, also=by_email, npc=npc)
        elif not conf.flag("live_armed") and not t.is_test:
            errors.append(f"{label}: live sending is not armed (ExCon, Settings).")
            continue
        else:
            m, err = _new(s, local, ctx, t, subject, body, files, docs, parent, response, person, user, member,
                          idem_key=key, extra_files=extra_files, body_html=body_html, also=by_email,
                          thread=thread, npc=npc)
            if err:
                errors.append(f"{label}: {err}")
                continue
        _show_copied(m, listed)
        if response is not None:
            skip_ai(response, s, who)
        if m.status == "failed":
            errors.append(f"{label}: {m.error or 'not delivered'}")
        msgs.append(m)
    if to_personas:
        teams = list(dict.fromkeys(t for t, _ in targets))
        msgs.append(_persona_mail(s, copied, thread, subject, body, body_html, files, docs, person, user, teams,
                                  sent_copy=not [m for m in msgs if m.status != "failed"], npc=npc, idem_key=persona_idem))
    if not [m for m in msgs if m.status != "failed"]:
        raise DeskError("; ".join(errors) or "Nothing was sent.")
    return msgs, errors


def _persona_label(senders):
    return ", ".join(f"{(x.display_name or x.local_part)} <{render.persona_address(x, x.local_part, {})}>" for x in senders)


def _persona_mail(s, copied, thread, subject, body, body_html, files, docs, person, user, teams, sent_copy=True,
                  npc=False, idem_key=None):
    """Persona mail: s writes to other personas (copied: {"to", "cc"} Senders), with teams
    (their own copies sent already) in copy. One Message (kind persona, no team) and the desk's
    copies: its Inbox copy for the personas written to, and its Sent copy when no team got it
    (else the teams' copies are the Sent). An AI persona in To answers (npc.on_persona_mail)."""
    from . import delivery, npc as npc_
    key = idem_key or f"pp-{uuid.uuid4().hex}"
    done = Message.objects.filter(idem_key=key).first()
    if done is not None:
        return done
    to = [x for x in copied["to"] if "{" not in x.local_part]
    cc = [x for x in copied["cc"] if "{" not in x.local_part]
    name = (s.display_name or s.local_part).replace('"', "'")
    addr = render.persona_address(s, s.local_part, {})
    team_keys = [f"t:{t.id}" for t in teams]
    rec = {"to": [f"p:{x.local_part}" for x in to], "cc": [f"p:{x.local_part}" for x in cc] + team_keys,
           "from": f"p:{s.local_part}"}
    to_label = _persona_label(to)
    cc_label = ", ".join(x for x in (_persona_label(cc), ", ".join(t.name for t in teams)) if x)
    now = timezone.now()
    test = bool(teams) and all(t.is_test for t in teams)
    m = Message.objects.create(
        kind=PERSONA_KIND, channel="inapp", target=to_label or cc_label, idem_key=key, from_addr=f'"{name}" <{addr}>',
        to=rec["to"], cc=rec["cc"], subject=subject, text=body, body=body, body_html=body_html, persona=s.local_part,
        thread=thread, author=person, npc=npc, status="sent", sent_at=now, attachment_ids=[a.id for a in docs])
    delivery.save_files(m, files)
    common = dict(team=None, owner=None, thread=thread, from_name=name, from_addr=addr, to_label=to_label,
                  cc_label=cc_label, desk_team=None, subject=subject, body=body or "", html=body_html, message=m,
                  is_test=test, author_id=person.id if person else None, recipients=rec)
    if sent_copy:
        out = Mail.objects.create(folder="sent", persona=s.local_part, read_at=now, **common)
        _files(out, m=m)
        if ACTOR.get() is not None:
            watch(thread, ACTOR.get(), "answered")
    inbox = Mail.objects.create(folder="inbox", persona=(to or cc)[0].local_part, **common)
    _files(inbox, m=m)
    if not npc:  # someone answered as s here: its own queued AI answer in this conversation goes
        NpcReply.objects.filter(npc__sender=s, thread_key=f"pp:{thread}"[:300], status__in=["queued", "held", "failed"]).update(
            status="skipped", note=f"Answered by {person.name if person else user} on the persona desk.",
            updated_at=timezone.now())
    auto_assign(thread, quiet=True)  # by the default of the persona written to, else the writer's
    audit("desk_persona_mail", f"{s.local_part} -> {', '.join(x.local_part for x in to + cc)}: {subject[:80]}", user)
    npc_.on_persona_mail(inbox, m, s, to, teams)
    return m


def _name(person, team):
    """A desk message's recipient as named in the Log and the desk's Sent."""
    if not person:
        return team.name
    return f"{team.name} leader ({person.name})" if person.is_leader else member_name(person)


def _new(s, local, ctx, team, subject, body, files, docs, parent, response, person, user, member=None, idem_key=None,
         extra_files=None, body_html="", also=None, thread="", npc=False):
    """A new message from the persona to a team or one of its members (kind desk), delivered
    now. In-app, a member's goes to their own mailbox only (personal mailboxes; else to the team
    inbox, for them). Returns (Message, error)."""
    from . import delivery
    cfg = conf.get_all()
    name = (render.fill(s.display_name, ctx) or local).replace('"', "'")
    channel = response.channel if response is not None else (
        "email" if conf.get("channel_mode") == "email" else "inapp")
    label = _name(member, team)
    forced = thread  # persona mail: the teams' copies in its conversation
    common = dict(target=label, subject=subject, body=body, body_html=body_html, footer=cfg.get("footer", ""),
                  attachment_ids=[a.id for a in docs], team_id=team.id, reply_local=local,
                  person_id=member.id if member else None)
    thread = ""
    if channel == "inapp":
        # A member's own mailbox (personal mailboxes), a leader's always.
        own = member if member and (member.is_leader or conf.flag("personal_mailboxes")) else None
        if forced:
            thread = forced
        elif parent is not None and Mail.objects.filter(team=team, owner=own, thread=parent.thread).exists():
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
        also = also or {}
        d = render.Draft(**common, from_addr=f'"{name}" <{local}@{settings.MAIL_DOMAIN}>', reply_to=None,
                         to=to + list(also.get("to", [])), cc=list(also.get("cc", [])), bcc=render.split_addresses(cfg.get("excon_bcc")), channel="email")
        thread = forced or (parent.thread if parent is not None else "")
    files = list(files) + (list(extra_files(team, member, channel)) if extra_files else [])
    m = delivery.new_message(None, d, "desk", idem_key or f"desk-{uuid.uuid4().hex}")
    m.author, m.thread, m.npc = person, thread, npc
    if channel == "email" and response is not None and response.message_id_header:
        m.extra_headers = {"In-Reply-To": response.message_id_header, "References": response.message_id_header}
    with transaction.atomic():  # the files are in place before anything can deliver the Message
        m.save()
        delivery.save_files(m, files)
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
    for t, mid, folder, team_id, at, rid, kind in (
            Mail.objects.filter(team=None, owner=None, thread__in=threads, trashed_at__isnull=True)
            .exclude(folder="drafts").order_by("id")
            .values_list("thread", "id", "folder", "desk_team_id", "created_at", "desk_response_id", "message__kind")):
        if folder == "inbox" and kind == PERSONA_KIND:
            continue  # persona mail: no team waits on it (an AI persona answers its own; anyone may)
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
              "owner_id": None, "assigned_by": "", "auto": False, "npc": None, "triage": None, "done_by": "", "section": REST}
        if c:
            last_sent = max((mid for mid, folder, *_ in ms if folder == "sent"), default=0)
            if c.follow_upto and last_sent <= c.follow_upto:
                st["follow"] = {"at": c.follow_at, "note": c.follow_note, "by": c.follow_by,
                                "due": c.follow_at is None or c.follow_at <= now}
            st["owner"], st["mine"] = c.owner_name, bool(user and c.owner_id and c.owner_id == user.id)
            st["owner_id"], st["assigned_by"] = c.owner_id, c.assigned_by if c.owner_id else ""
            st["auto"] = bool(c.owner_id and c.auto)
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


ACTIONS = ("take", "assign", "release", "done", "reopen", "follow", "unfollow", "triage", "watch", "unwatch")


def mark(threads, act, user, name, minutes=None, note="", assignee=None):
    """A desk member's action on conversations (ACTIONS): take it (it's theirs), assign it to
    someone on the desk (assignee, a user id of assignees()) or release it; done (no reply
    needed: the teams' messages so far) or reopen; follow up (in minutes, or now, with a note)
    or stop; triage (ask Claude now); watch or unwatch (hear of what happens in it, or not).
    Whoever is given a conversation, or has it taken from them, is told (notify). Returns how
    many were changed."""
    from .models import DeskConversation
    if act not in ACTIONS:
        raise DeskError("Unknown action.")
    to = None
    if act == "assign":
        to = next((u for u in assignee_users() if str(u.id) == str(assignee or "")), None)
        if to is None:
            raise DeskError("Choose who on the desk it goes to.")
    threads = list(Mail.objects.filter(team=None, owner=None, thread__in=threads).order_by()
                   .values_list("thread", flat=True).distinct())
    now = timezone.now()
    given = []  # (thread, previous owner id, new owner)
    for t in threads:
        c = conversation(t, create=True)
        if act in ("take", "assign"):
            new = user if act == "take" else to
            if c.owner_id == new.id:
                continue
            given.append((t, c.owner_id, new))
            c.owner, c.owner_name = new, (name if new.id == user.id else user_name(new))[:150]
            c.assigned_by, c.assigned_at = ("" if new.id == user.id else name[:150]), now
            c.auto, c.auto_done = False, True
            watch(t, new, "owner")
        elif act == "release":
            c.owner, c.owner_name, c.assigned_by, c.assigned_at = None, "", "", None
            c.auto, c.auto_done = False, True  # nobody's, on purpose: the defaults don't give it again
        elif act == "watch":
            watch(t, user, "follow", force=True)
            continue
        elif act == "unwatch":
            unwatch(t, user)
            continue
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
            c.follow_note, c.follow_by, c.follow_user, c.follow_told = (note or "")[:300], name[:150], user, 0
            watch(t, user, "follow_up")
        elif act == "unfollow":
            c.follow_upto, c.follow_at, c.follow_note, c.follow_by, c.follow_user = 0, None, "", "", None
        elif act == "triage":
            c.ai_status, c.ai_upto, c.ai_error = "queued", 0, ""
        c.save()
    if threads:
        audit("desk_mark", f"{act} x{len(threads)}" + (f" -> {user_name(to)}" if to else ""), name)
    if given:
        _told_given(given, user, name)
    return len(threads)


def _told_given(given, user, name):
    """Conversations given (by user, name) to someone else, or taken from someone: both told,
    once for all of them (several cards dropped on the Assign board at once)."""
    info = _about([t for t, _, _ in given])
    User = get_user_model()
    old = {u.id: u for u in User.objects.filter(id__in={o for _, o, _ in given if o})}
    stamp = f"{timezone.now().timestamp():.0f}"
    to_new, to_old = {}, {}
    for t, old_id, new in given:
        if new.id != user.id:
            to_new.setdefault(new.id, (new, []))[1].append(t)
        if old_id and old_id not in (user.id, new.id) and old_id in old:
            to_old.setdefault(old_id, (old[old_id], []))[1].append((t, new))

    def what(t):
        subj, team, _ = info.get(t, ("", None, False))
        return f"{team.name}: {subj}" if team else subj

    def test(ts):
        return all(info.get(t, ("", None, False))[2] for t in ts)

    for u, ts in to_new.values():
        if len(ts) == 1:
            notify([u], f"assign:{ts[0]}:{stamp}", "assign", f"{name} gave you a conversation", what(ts[0]), ts[0],
                   f"/inbox/desk/t/{ts[0]}/", test(ts), push_=True)
        else:
            notify([u], f"assign:{len(ts)}x{ts[0]}:{stamp}", "assign", f"{name} gave you {len(ts)} conversations",
                   "; ".join(what(t) for t in ts[:5]) + ("..." if len(ts) > 5 else ""), "", "/inbox/desk/?folder=mine",
                   test(ts), push_=True)
    for u, xs in to_old.values():
        ts = [t for t, _ in xs]
        if len(xs) == 1:
            t, new = xs[0]
            by = "took it over" if new.id == user.id else f"gave it to {user_name(new)}"
            notify([u], f"unassign:{t}:{stamp}", "assign", f"{name} {by}", what(t), t, f"/inbox/desk/t/{t}/", test(ts))
        else:
            notify([u], f"unassign:{len(xs)}x{ts[0]}:{stamp}", "assign", f"{name} moved {len(xs)} of your conversations",
                   "; ".join(f"{what(t)} -> {user_name(n)}" for t, n in xs[:5]), "", "/inbox/desk/?folder=mine", test(ts))


# --- default assignments (DeskDefault, the Assign tab) --------------------------------------------
#
# A persona's conversations with a team go by default to someone on the desk: the rule for that
# persona and that team, else the persona's rule for all teams. Given once, to a conversation
# nobody has, at its first desk copy (a persona's delivery: quietly, the owner hears when the team
# writes; a team's message: before those on it are told), or by Apply now; never over someone
# who gave, took or released it by hand.

DEFAULTS = "default assignments"  # DeskConversation.assigned_by of the defaults' owners


def persona_senders():
    """{username: Sender} of the desk's personas, a {team} one filled for every team."""
    out, teams = {}, None
    for s in personas():
        if "{team}" in s.local_part:
            teams = teams if teams is not None else list(Team.objects.all())
            for t in teams:
                out[render.fill(s.local_part, render.team_ctx(t)).lower()] = s
        else:
            out[s.local_part.lower()] = s
    return out


def default_rules():
    """{(sender id, team id or None): user} of the default assignments, but to those no longer
    on the desk."""
    from .models import DeskDefault
    ok = {u.id: u for u in assignee_users()}
    return {(d.sender_id, d.team_id): ok[d.user_id] for d in DeskDefault.objects.all() if d.user_id in ok}


def default_owner(local, team_id, rules=None, senders=None):
    """Who has the conversations of the persona local (a username) with the team team_id by
    default, or None."""
    rules = default_rules() if rules is None else rules
    s = (persona_senders() if senders is None else senders).get((local or "").lower()) if rules else None
    if s is None:
        return None
    return (team_id and rules.get((s.id, team_id))) or rules.get((s.id, None))


def sides(threads):
    """{thread: (persona username, team id)} a desk conversation is about: its first team
    message to a persona, else its first persona message."""
    first_in, first = {}, {}
    for t, folder, local, team_id in (Mail.objects.filter(team=None, owner=None, thread__in=threads).exclude(folder="drafts")
                                      .order_by("id").values_list("thread", "folder", "persona", "desk_team_id")):
        first.setdefault(t, (local, team_id))
        if folder == "inbox":
            first_in.setdefault(t, (local, team_id))
    return {t: first_in.get(t) or first[t] for t in first}


def _give_default(c, to, now=None):
    c.owner, c.owner_name, c.assigned_by, c.assigned_at = to, user_name(to)[:150], DEFAULTS, now or timezone.now()
    c.auto = c.auto_done = True
    c.save()
    watch(c.thread, to, "owner")


def auto_assign(thread, quiet=False):
    """Give a desk conversation to its default owner, once: when nobody has it and nobody on the
    desk has given, taken or released it. Quiet or not, no notice of its own (the owner is told
    when the team writes, as anyone on it). Returns the owner, or None."""
    from .models import DeskConversation, DeskDefault
    try:
        if not DeskDefault.objects.exists():
            return None
        c = DeskConversation.objects.filter(thread=thread).first()
        if c and (c.owner_id or c.auto_done):
            return None
        local, team_id = sides([thread]).get(thread, (None, None))
        to = default_owner(local, team_id)
        if to is None and team_id is None:  # persona mail: no team; the writer's default then
            writer = (Mail.objects.filter(team=None, owner=None, thread=thread, message__kind=PERSONA_KIND)
                      .order_by("id").values_list("recipients", flat=True).first() or {}).get("from", "")
            to = default_owner(writer[2:], None) if writer.startswith("p:") else None
        if to is None:
            return None
        with transaction.atomic():
            _give_default(c or conversation(thread, create=True), to)
        return to
    except Exception:
        log.exception("default assignment of %s failed", thread)
        return None


def unassigned_open(kind=None):
    """The desk conversations nobody has that still need something (not in the rest), with the
    owner the defaults would give them: [(thread, user)]."""
    from .models import DeskConversation
    owned = DeskConversation.objects.filter(owner__isnull=False).values("thread")
    threads = list(Mail.objects.filter(team=None, owner=None, trashed_at__isnull=True).exclude(folder="drafts")
                   .exclude(thread__in=owned).order_by().values_list("thread", flat=True).distinct())
    rules = default_rules()
    if not threads or not rules:
        return []
    senders, side = persona_senders(), sides(threads)
    out = []
    for t, st in states(threads).items():
        if st["section"] == REST:
            continue
        to = default_owner(*side.get(t, (None, None)), rules=rules, senders=senders)
        if to is not None:
            out.append((t, to))
    return out


def apply_defaults(user, name):
    """Apply now: the open conversations nobody has, to their default owners (each told once, for
    all theirs). Returns how many were given."""
    given = unassigned_open()
    now = timezone.now()
    for t, to in given:
        _give_default(conversation(t, create=True), to, now)
    if given:
        audit("desk_defaults_apply", f"x{len(given)}", name)
        _told_given([(t, None, to) for t, to in given], user, name)
    return len(given)


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
    an AI persona is about to answer or Claude reads as needing no reply; the owner of each is
    told too, and follow-ups falling due are told (tick_follow_ups). Returns how many waits."""
    try:
        tick_follow_ups(now)
    except Exception:
        log.exception("desk follow-ups not told")
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
        if mail and st["owner_id"]:
            owner = get_user_model().objects.filter(pk=st["owner_id"]).first()
            notify([owner], f"wait:{t}:{mail.id}", "wait", f"{_team_name(mail.desk_team)} has waited {wait} min",
                   mail.subject, t, f"/inbox/desk/t/{t}/", mail.is_test, push_=True)
    return n


def tick_follow_ups(now=None):
    """Scheduler main loop: whoever set a follow-up (and whoever has the conversation) is told
    when it falls due, once per follow-up. Returns how many follow-ups came due."""
    from .models import DeskConversation
    now = now or timezone.now()
    due = list(DeskConversation.objects.filter(follow_upto__gt=0, follow_at__isnull=False, follow_at__lte=now)
               .exclude(follow_told=models.F("follow_upto")).select_related("owner", "follow_user"))
    if not due:
        return 0
    sts = states([c.thread for c in due], now=now)
    info = _about([c.thread for c in due])
    n = 0
    for c in due:
        DeskConversation.objects.filter(pk=c.pk).update(follow_told=c.follow_upto)
        st = sts.get(c.thread) or {}
        if not (st.get("follow") and st["follow"]["due"]):
            continue  # a persona wrote in it since: the follow-up is over
        subj, team, test = info.get(c.thread, ("", None, False))
        title = f"Follow up: {team.name}: {subj}" if team else f"Follow up: {subj}"
        notify([c.follow_user, c.owner], f"fu:{c.thread}:{c.follow_upto}:{c.follow_at.timestamp():.0f}", "follow_up",
               title, c.follow_note or f"Set by {c.follow_by or 'the desk'}.", c.thread, f"/inbox/desk/t/{c.thread}/", test,
               push_=True)
        n += 1
    return n


def prune():
    """Conversations no longer on the desk (cleared, withdrawn): their state goes, with who
    followed them and the notices about them."""
    from .models import DeskConversation, DeskNotice, DeskWatch
    on = Mail.objects.filter(team=None, owner=None).values("thread")
    DeskConversation.objects.exclude(thread__in=on).delete()
    DeskWatch.objects.exclude(thread__in=on).delete()
    DeskNotice.objects.exclude(thread="").exclude(thread__in=on).delete()


# --- who is on a conversation, and what they hear ----------------------------------------------------
#
# Anyone signed in on the desk (a desk member or ExCon staff) is a login (User). A conversation
# can be given to one of them (DeskConversation.owner: Assigned to me), and they are involved in
# the ones they answered, took, were given, set a follow-up on or follow (DeskWatch: Following).
# DeskNotice is each one's own feed (the bell): the owner hears of everything in theirs, those
# who follow a conversation on purpose too, and the others involved only while nobody has it.

ACTOR = contextvars.ContextVar("desk_actor", default=None)  # who on the desk is sending (desk.send)


def user_name(user):
    """How the desk names a login: its Person's name, else the account's."""
    person = getattr(user, "person", None) if user else None
    if person:
        return person.name
    return (user.get_full_name() or user.get_username()) if user else ""


def assignee_users():
    """Who a conversation can be given to: the active desk members with a login, and ExCon
    staff."""
    from django.db.models import Q
    User = get_user_model()
    return list(User.objects.filter(is_active=True).filter(
        Q(person__role="desk", person__active=True) | Q(is_staff=True) | Q(is_superuser=True) | Q(groups__name="excon"))
        .distinct().select_related("person").order_by("first_name", "username"))


def assignees(me=None):
    """The "Assign to" choices: [{"id", "name", "desk" (a desk member, not ExCon), "me"}], desk
    members first."""
    out = [{"id": u.id, "name": user_name(u), "desk": bool(getattr(u, "person", None) and u.person.role == "desk"),
            "me": bool(me and u.id == me.id)} for u in assignee_users()]
    return sorted(out, key=lambda x: (not x["desk"], x["name"].lower()))


def watch(thread, user, reason, force=False):
    """user is involved in the desk conversation thread (reason). force: following it on
    purpose, even after they stopped."""
    from .models import DeskWatch
    if not (user and getattr(user, "pk", None)):
        return None
    w, made = DeskWatch.objects.get_or_create(thread=thread, user=user, defaults={"reason": reason})
    if made:
        return w
    if force and (w.off or w.reason != "follow"):
        w.off, w.reason, w.at = False, "follow", timezone.now()
        w.save()
    return w


def unwatch(thread, user):
    from .models import DeskWatch
    DeskWatch.objects.update_or_create(thread=thread, user=user, defaults={"off": True, "reason": "follow"})


def watching(thread, user):
    """Is user following this conversation (involved and not stopped)?"""
    from .models import DeskWatch
    return DeskWatch.objects.filter(thread=thread, user=user, off=False).exists()


def audience(thread, exclude=()):
    """Who hears of what happens in a conversation: its owner and those following it on
    purpose; while nobody has it, everyone involved. exclude: user ids."""
    from .models import DeskWatch
    c = conversation(thread)
    ws = list(DeskWatch.objects.filter(thread=thread, off=False).select_related("user"))
    out = {}
    if c and c.owner_id and c.owner:
        out[c.owner_id] = c.owner
    for w in ws:
        if w.reason == "follow" or not (c and c.owner_id):
            out[w.user_id] = w.user
    return [u for uid, u in out.items() if uid not in set(exclude) and u.is_active]


def notify(users, key, kind, title, body="", thread="", url="", test=False, push_=False):
    """A DeskNotice for each of users (once per key), pushed to their devices (push_; desk
    members only, ExCon staff have no Person). Never breaks what it reports on. Returns how
    many were new."""
    from .models import DeskNotice
    seen, new = set(), []
    try:
        for u in users:
            if not u or u.id in seen:
                continue
            seen.add(u.id)
            _, made = DeskNotice.objects.get_or_create(user=u, key=key[:120], defaults=dict(
                kind=kind, title=title[:300], body=(body or "")[:500], thread=thread, url=url, test=test))
            if made:
                new.append(u)
        people = [p.id for p in Person.objects.filter(user__in=new, role="desk", active=True)] if push_ else []
        if people:
            push.notify_people(people, {"title": title[:120], "body": (body or "")[:160], "url": url or "/inbox/desk/",
                                        "tag": f"desk-{kind}-{thread[:20]}"})
    except Exception:
        log.exception("desk notice %s not written", key)
    return len(new)


def notices_of(user):
    """user's notices not dismissed, newest first."""
    from .models import DeskNotice
    return DeskNotice.objects.filter(user=user, dismissed_at__isnull=True)


def notice_counts(user):
    """{"unread", "latest"} of user's notices (the bell, the poll)."""
    from django.db.models import Max
    if not (user and user.is_authenticated):
        return {"unread": 0, "latest": 0}
    qs = notices_of(user)
    return {"unread": qs.filter(read_at__isnull=True).count(), "latest": qs.aggregate(m=Max("id"))["m"] or 0}


def _team_name(team):
    return team.name if team else "A team"


def _about(threads):
    """{thread: (subject, team, test)} of desk conversations: their first message's subject and
    the team on the other side."""
    out = {}
    for m in (Mail.objects.filter(team=None, owner=None, thread__in=threads).exclude(folder="drafts")
              .select_related("desk_team").order_by("id")):
        if m.thread not in out:
            out[m.thread] = (m.subject, m.desk_team, m.is_test)
        elif out[m.thread][1] is None and m.desk_team:
            out[m.thread] = (out[m.thread][0], m.desk_team, out[m.thread][2])
    return out


def _told_in(mail):
    """A team wrote in a desk conversation (mail, the desk's Inbox copy): those on it hear. An
    invitation, a change or a cancellation has its own notice (calendar._tell_desk)."""
    if mail.invite in ("request", "update", "cancel"):
        return
    who = mail.from_name or mail.from_addr
    notify(audience(mail.thread), f"in:{mail.id}", "reply", f"{_team_name(mail.desk_team)} wrote: {mail.subject}",
           f"{who}: {(mail.body or '').strip()[:200]}", mail.thread, f"/inbox/desk/t/{mail.thread}/", mail.is_test)


def _told_out(m, desk_mail):
    """A persona answered in a desk conversation (m, the Message): those on it hear, but not
    whoever sent it; the sender is involved from now on."""
    if not (m.kind in ("reply", "desk") or m.npc):
        return
    actor = ACTOR.get()
    if actor is not None:
        watch(desk_mail.thread, actor, "answered")
    if m.npc:
        who = "The AI persona"
    elif actor is not None:
        who = user_name(actor)
    else:
        who = m.author.name if m.author_id else "ExCon"
    exclude = [actor.id] if actor is not None else []
    if m.author_id and m.author.user_id:
        exclude.append(m.author.user_id)
    persona = describe(m.persona, desk_mail.desk_team)["name"]
    notify(audience(desk_mail.thread, exclude), f"out:{desk_mail.id}", "answer",
           f"{who} answered {_team_name(desk_mail.desk_team)} as {persona}", desk_mail.subject, desk_mail.thread,
           f"/inbox/desk/t/{desk_mail.thread}/", desk_mail.is_test)


def told_npc(job):
    """An AI persona's answer held for ExCon or failed: those on its desk conversation hear."""
    if job.status not in ("held", "failed") or not (job.response_id or job.mail_id):
        return
    mail = (job.mail if job.mail_id else
            Mail.objects.filter(team=None, owner=None, desk_response_id=job.response_id).select_related("desk_team").first())
    if not mail:
        return
    who = job.npc.sender.display_name or job.npc.sender.local_part
    what = "is held for ExCon" if job.status == "held" else "failed: answer it here"
    notify(audience(mail.thread), f"npc:{job.id}:{job.status}", "npc", f"AI persona {who}'s answer {what}",
           f"{_team_name(mail.desk_team)}: {mail.subject}", mail.thread, f"/inbox/desk/t/{mail.thread}/", mail.is_test)

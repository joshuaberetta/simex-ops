"""The persona desk's side of SIMEX Messenger (web/docs/MESSENGER_PLAN.md, step 3): personas in
rooms, a team's turns to them as Responses, the personas' posts as Messages, and the desk's
Chats console (/chat/desk/: views_messenger, mode "desk").

A team-side *turn* is the consecutive posts of one author in a room with a persona, closed by a
gap of TURN_GAP, a post by anyone else, or TURN_MAX posts: one Response (channel "chat",
headers {"room": id}, thread key c:<room>), grown as posts are added and marked fetched when it
closes, so SIMEX AI reads it once, whole (ai.pending_replies only reads fetched ones). Its persona
is who it is addressed to: the persona of a direct room, an @mention, a reply to a persona's post,
the room's only persona; else nobody (it is "Chatter" on the desk, never queued for an AI persona).

A persona's post (desk.send's counterpart, post_as) is a Message of kind "chat" (channel "chat",
deterministic idem_key, made before the Post, status sent at once) with the Post pointing at it.

The desk's state of a room is DeskConversation "chat:<room id>" (owner, done_upto and follow_upto
are Post ids), so take, assign, follow-ups, watchers and DeskNotices work as for mail. No desk
Mail is ever made for chat: the desk reads the rooms themselves."""

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from . import conf, desk, notices
from . import messenger as mx
from .models import DeskConversation, Message, NpcReply, Post, PostAttachment, Response, Room, RoomMember

log = logging.getLogger(__name__)

TURN_GAP = timedelta(seconds=90)
TURN_MAX = 20
ATTENTION, CHATTER, LATER, REST = "attention", "chatter", "later", "rest"
SECTIONS = [(ATTENTION, "Needs attention"), (CHATTER, "Chatter (no persona asked)"), (LATER, "Later"),
            (REST, "Answered or nothing to answer")]


def thread(room_or_id):
    return f"chat:{getattr(room_or_id, 'id', room_or_id)}"


def room_id(thread_key):
    return int(thread_key[5:]) if thread_key.startswith("chat:") and thread_key[5:].isdigit() else None


def existing(threads):
    """The chat: threads that are rooms with a persona (desk.mark)."""
    ids = {room_id(t) for t in threads} - {None}
    have = set(Room.objects.filter(pk__in=ids, members__key__startswith="p:").values_list("id", flat=True))
    return [t for t in threads if room_id(t) in have]


def about(threads):
    """{thread: (title, team, test)} (desk._about)."""
    v = _quiet_viewer()
    names = mx.Names(v)
    out = {}
    for r in Room.objects.filter(pk__in=[room_id(t) for t in threads]).select_related("team"):
        out[thread(r)] = (f"Messenger: {mx.title_of(v, r, names)}", r.team, r.is_test)
    return out


def _quiet_viewer():
    v = mx.ChatViewer.__new__(mx.ChatViewer)
    v.mode, v.desk, v.readonly, v.person, v.member, v.leader = "desk", True, False, None, None, None
    v.team, v.keys, v.key, v.base = None, [], "", "/chat/desk"
    v.sim_only = v.sees_ai = v.sees_history = False
    return v


def personas_in(room, current=True):
    qs = RoomMember.objects.filter(room=room, key__startswith="p:")
    if current:
        qs = qs.filter(left_at__isnull=True)
    return [m.persona for m in qs.order_by("id")]


# --- a team's turns ----------------------------------------------------------------------------------

def _address(p, room, personas):
    """Who the post is for: a direct room's persona, an @mention, a reply to a persona, the only
    persona of the room; else "" (nobody asked)."""
    if not personas:
        return ""
    for k in p.mentions or []:
        if k.startswith("p:") and k[2:] in personas:
            return k[2:]
    if p.reply_to_id and p.reply_to and p.reply_to.persona in personas:
        return p.reply_to.persona
    if len(personas) == 1 and (room.kind == "direct" or len(set(personas)) == 1):
        return personas[0]
    return ""


def close_turns(room, except_response=None):
    """Every open turn of the room closed (fetched: SIMEX AI may read it)."""
    qs = Response.objects.filter(channel="chat", fetched=False, headers__room=room.id)
    if except_response is not None:
        qs = qs.exclude(pk=except_response.pk)
    return qs.update(fetched=True)


def on_team_post(p):
    """messenger.HOOKS: a member's or leader's post in a room with a persona -> its turn."""
    room = p.room
    if p.kind != "text" or not p.author_id or not RoomMember.objects.filter(
            room=room, key__startswith="p:", left_at__isnull=True).exists():
        return None
    if p.author.role == "tl":
        return None  # a team leader is ExCon: never a team's message to a persona (as in mail)
    personas = personas_in(room)
    persona = _address(p, room, personas)
    prev = (Post.objects.filter(room=room, kind="text", id__lt=p.id).exclude(deleted_at__isnull=False)
            .select_related("response").order_by("-id").first())
    r = prev.response if prev and prev.author_id == p.author_id and prev.response_id else None
    if r and (r.fetched or p.created_at - prev.created_at > TURN_GAP or r.posts.count() >= TURN_MAX):
        r = None
    text = p.body or "(sent " + ", ".join(f.filename for f in p.files.all()) + ")"
    if r is not None:
        r.text = ((r.text or "") + "\n" + text)[:20000]
        fields = ["text"]
        if persona and not r.persona:
            r.persona, r.to = persona, [f"p:{persona}"]
            fields += ["persona", "to"]
        r.save(update_fields=fields)
        Post.objects.filter(pk=p.pk).update(response=r)
        if "persona" in fields:
            _queue_npc(r)
        return r
    close_turns(room)
    v = _quiet_viewer()
    names = mx.Names(v)
    team = p.author.team
    from .views_inbox import member_address
    r = Response.objects.create(
        channel="chat", received_at=p.created_at, from_addr=member_address(p.author), from_name=p.author.name,
        to=[f"p:{persona}"] if persona else [f"p:{x}" for x in personas], subject=f"Messenger: {mx.title_of(v, room, names)}"[:300],
        text=text, persona=persona or None, team=team, person=p.author, match="chat", fetched=False,
        kind="test" if team.is_test else "live", headers={"room": room.id, "personas": personas})
    Post.objects.filter(pk=p.pk).update(response=r)
    p.response = r
    if persona:
        notices.reply(r)
    _queue_npc(r)
    t = thread(room)
    _auto_assign(t, persona or personas[0], team.id)
    job = desk.ai_answering(r)
    who = names.name(mx.post_key(p))
    if job is None:
        desk.notify(desk.audience(t), f"chat-in:{r.id}", "reply", f"{who} wrote in Messenger",
                    (p.body or "")[:160], t, f"/chat/desk/r/{room.id}/", test=room.is_test)
    return r


def _queue_npc(r):
    from . import npc
    if not conf.flag("chat_npc"):
        return None
    return npc.on_response(r)


def _auto_assign(t, persona, team_id):
    """The default owner (DeskDefault) of the persona and the team gets a room nobody has, once."""
    c = desk.conversation(t, create=True)
    if c.owner_id or c.auto_done:
        return
    to = desk.default_owner(persona, team_id)
    if to is not None:
        desk._give_default(c, to)
    else:
        c.auto_done = True
        c.save(update_fields=["auto_done"])


def tick(now=None):
    """The scheduler's main loop: turns quiet for TURN_GAP are closed (SIMEX AI reads them)."""
    now = now or timezone.now()
    n = 0
    for r in Response.objects.filter(channel="chat", fetched=False).only("id", "received_at"):
        last = Post.objects.filter(response=r).order_by("-id").values_list("created_at", flat=True).first() or r.received_at
        if now - last >= TURN_GAP:
            n += Response.objects.filter(pk=r.pk).update(fetched=True)
    return n


# --- a persona's posts -------------------------------------------------------------------------------

class PersonaPostError(Exception):
    pass


def post_as(room, persona, body, files=(), reply_to=None, client_key="", user=None, person=None, npc=False,
            idem=None, ai_draft=None):
    """A persona's post in room (the desk, ExCon with user and no person, or an AI persona: npc,
    idem npc-<job>). Its Message first (kind chat, the room's latest turn as response), then the
    Post. Returns the Post (the same one again for a repeated key)."""
    body = (body or "").replace("\r\n", "\n").strip()[:mx.BODY_MAX]
    if not body and not files:
        raise PersonaPostError("Write something first.")
    if persona not in personas_in(room):
        raise PersonaPostError("That persona isn't in this chat.")
    if room.frozen:
        raise PersonaPostError("ExCon has frozen this chat.")
    key = idem or f"chat-{room.id}-{(client_key or '')[:40] or timezone.now().timestamp()}"
    old = Message.objects.filter(idem_key=key).first()
    if old:
        got = Post.objects.filter(message=old).first()
        if got:
            return got
    turn = (Response.objects.filter(channel="chat", headers__room=room.id).exclude(status="ignored")
            .order_by("-id").first())
    from . import render
    s, ctx = render.sender_by_local(persona)
    names = mx.Names(_quiet_viewer())
    now = timezone.now()
    with transaction.atomic():
        m = Message.objects.create(
            kind="chat", channel="chat", idem_key=key[:200], from_addr=render.persona_addr(persona),
            to=[k for k in RoomMember.objects.filter(room=room, left_at__isnull=True).exclude(key__startswith="p:")
                .values_list("key", flat=True)],
            subject=f"Messenger: {mx.title_of(_quiet_viewer(), room, names)}"[:300], text=body, body=body,
            status="sent", sent_at=now, team=room.team, persona=persona, author=person, npc=npc, ai_draft=ai_draft,
            response=turn if turn and turn.team_id else None, thread=thread(room), delivery="delivered")
        reply = Post.objects.filter(room=room, pk=reply_to, kind="text").first() if reply_to else None
        p = Post.objects.create(room=room, persona=persona, author_user=user, body=body, reply_to=reply, message=m,
                                npc=npc, client_key=(client_key or "")[:40], is_test=room.is_test)
        for name, ctype, data in files:
            PostAttachment.objects.create(post=p, filename=(name or "file")[:300],
                                          content_type=(ctype or "application/octet-stream")[:120], size=len(data), data=data)
        Room.objects.filter(pk=room.pk).update(last_post_id=p.id, last_post_at=p.created_at)
        mx.change(room, "post", post_id=p.id)
        close_turns(room)
        if s and not npc and turn:
            desk.skip_ai(turn, s, desk.user_name(user) if user else "ExCon")
        token = desk.ACTOR.set(user.id if user else None)
        try:
            t = thread(room)
            desk.notify(desk.audience(t, exclude=[user.id] if user else ()), f"chat-out:{p.id}", "answer",
                        f"{names.persona(persona)['name']} answered in Messenger", body[:160], t, f"/chat/desk/r/{room.id}/",
                        test=room.is_test)
        finally:
            desk.ACTOR.reset(token)
        mx.notify(room, p, title=names.persona(persona, room.team)["name"])
    if user is not None:
        desk.watch(thread(room), user, "answered")
    return p


def open_room(v, persona, key, user=None):
    """The desk opens a chat between a persona and a team (t:), a member (m:) or a leader (l:)."""
    from .models import Sender
    if not Sender.objects.filter(active=True, team_leader=False).exists():
        raise PersonaPostError("No personas yet.")
    names = mx.Names(v)
    kind, _, val = key.partition(":")
    if kind not in ("t", "m", "l") or not val.isdigit():
        raise PersonaPostError("Choose a team, a member or a team leader.")
    pair = "|".join(sorted([f"p:{persona}", key]))
    room = Room.objects.filter(kind="direct", direct_key=pair).first()
    if room:
        return room
    with transaction.atomic():
        room = Room.objects.create(kind="direct", direct_key=pair, created_by_user=user)
        mx._member_row(room, f"p:{persona}", names)
        mx._member_row(room, key, names)
        mx._refresh_teams(room)
        mx.change(room, "room")
        mx.change(room, "member", key=key)
    return room


# --- the desk's state of its rooms -------------------------------------------------------------------

def states(ids, user=None, now=None):
    """{room id: state}: section, waiting (since), owner (mine), npc, done_by, unread (team posts
    since the personas' last)."""
    ids = list(ids)
    now = now or timezone.now()
    if not ids:
        return {}
    last_out = dict(Post.objects.filter(room_id__in=ids, kind="text").exclude(persona="").order_by()
                    .values("room_id").annotate(m=Max("id"))
                    .values_list("room_id", "m"))
    convs = {room_id(c.thread): c for c in DeskConversation.objects.filter(thread__in=[thread(i) for i in ids])}
    floor = {i: max(last_out.get(i, 0), convs[i].done_upto if i in convs else 0) for i in ids}
    q = None
    for i in ids:
        x = Q(room_id=i, id__gt=floor[i])
        q = x if q is None else q | x
    open_posts = {}
    for rid, pid, at, resp, persona in (Post.objects.filter(q, kind="text", author__isnull=False, deleted_at__isnull=True)
                                        .exclude(author__role="tl").order_by("id")
                                        .values_list("room_id", "id", "created_at", "response_id", "response__persona")):
        open_posts.setdefault(rid, []).append((pid, at, resp, persona))
    jobs = {}
    rids = [p[2] for ps in open_posts.values() for p in ps if p[2]]
    for job in NpcReply.objects.filter(response_id__in=rids, status__in=["queued", "writing", "held", "failed", "skipped"]
                                       ).select_related("npc__sender"):
        jobs[job.response_id] = job
    out = {}
    for i in ids:
        ps, c = open_posts.get(i, []), convs.get(i)
        addressed = [p for p in ps if p[3]]
        st = {"section": REST, "waiting": bool(addressed), "since": (addressed or ps or [(0, None)])[0][1],
              "unread": len(ps), "owner": c.owner_name if c and c.owner_id else "", "owner_id": c.owner_id if c else None,
              "mine": bool(user and c and c.owner_id == user.id), "npc": None, "done_by": "", "follow": None}
        if c and c.follow_upto and last_out.get(i, 0) <= c.follow_upto:
            st["follow"] = {"at": c.follow_at, "note": c.follow_note, "due": c.follow_at is None or c.follow_at <= now}
        if c and c.done_upto and not ps:
            st["done_by"] = c.done_by
        job = next((jobs[p[2]] for p in reversed(addressed) if p[2] in jobs), None)
        if job:
            st["npc"] = desk.npc_state(job)
        if job and job.status in ("queued", "writing"):
            st["section"] = LATER
        elif addressed or (st["follow"] and st["follow"]["due"]):
            st["section"] = ATTENTION
        elif ps:
            st["section"] = CHATTER
        elif st["follow"]:
            st["section"] = LATER
        out[i] = st
    return out


def attention_count():
    ids = list(Room.objects.filter(members__key__startswith="p:", members__left_at__isnull=True).distinct()
               .values_list("id", flat=True))
    return sum(1 for s in states(ids).values() if s["section"] == ATTENTION)


def desk_rooms(v, only=None):
    """The desk's room list (messenger.room_list) with each room's state, in sections."""
    rows = mx.room_list(v, only=only)
    sts = states([r["id"] for r in rows], v.user)
    for r in rows:
        st = sts.get(r["id"], {})
        r["section"] = st.get("section", REST)
        r["unread"] = st.get("unread", 0)
        r["owner"], r["mine"] = st.get("owner", ""), st.get("mine", False)
        r["since"] = int(st["since"].timestamp() * 1000) if st.get("since") else None
        r["npc"] = (st.get("npc") or {}).get("who", "") if st.get("npc") else ""
    order = {k: n for n, (k, _) in enumerate(SECTIONS)}
    rows.sort(key=lambda r: (order.get(r["section"], 9), r["since"] or 0 if r["section"] == ATTENTION else
                             -(r["last"]["id"] if r["last"] else 0)))
    return rows


def persona_names(room, team=None):
    names = mx.Names(_quiet_viewer())
    return [{"local": p, "name": names.persona(p, team or room.team)["name"],
             "colour": names.persona(p, team or room.team)["colour"]} for p in personas_in(room)]


def _register():
    if on_team_post not in mx.HOOKS:
        mx.HOOKS.append(on_team_post)


_register()

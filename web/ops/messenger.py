"""SIMEX Messenger (web/docs/MESSENGER_PLAN.md): chat inside the exercise, like Signal or
WhatsApp, so the teams' side conversations stay in the platform where ExCon can follow them.

A room (Room) has members by address-book key: m:<pid> a team member, l:<pid> a team leader,
p:<username> a persona (played by the persona desk), t:<team id> every participant of a team.
Every team has its team room (kind "team": the team and its leader); direct rooms hold two keys
(one room per pair); groups are named, with admins. Who may chat with whom is the address book
(book): teammates, their leader, the personas, other teams only with team_to_team; a room never
holds two teams without it.

Every read of a room goes through a ChatViewer (rooms, member_of, visible_posts): teams never
cross, and a member added later reads from when they joined. Every change is a RoomChange, the
feed the clients poll by cursor ("anything new?" is one lookup, cached a second per process);
the room ids of a viewer are cached per process too, until a member change concerns them.

Times: members see simulation time only (Post.sim_at), everyone else both."""

import logging
import re
from datetime import timedelta

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from . import conf, push, simclock
from .access import is_excon, leader_of, participant_of
from .models import (MailAction, Person, Post, PostAttachment, PostEdit, PostReaction, ReadMark, Room, RoomChange,
                     RoomMember, RoomState, Team)

log = logging.getLogger(__name__)

EMOJI = ["👍", "❤️", "😂", "😮", "😢", "🙏"]
BODY_MAX = 6000
TITLE_MAX = 80
GROUP_MAX = 60  # keys in a group
PAGE = 50  # posts loaded at a time
CHANGES_MAX = 400  # changes returned by one poll
TYPING_SECONDS = 6
HEAD_TTL = 1  # seconds the newest change id is cached per process
ROOMS_TTL = 30  # seconds a viewer's room ids are cached per process
PUSH_GAP = timedelta(seconds=30)  # at most one push per room per person in a burst
MENTION = re.compile(r"@\[([mlpt]:[\w.\-{}@+]+)\]")


class ChatError(Exception):
    pass


def enabled():
    return conf.flag("chat_enabled")


# --- who is looking ----------------------------------------------------------------------------------

class ChatViewer:
    """Who is looking at SIMEX Messenger and what they may see. mode "": a team member or leader
    in their own Messenger; "team": a team leader reading every room of their team, read-only;
    "excon": ExCon staff, every room, read-only."""

    def __init__(self, user, mode="", person=None):
        self.user, self.mode = user, mode
        if person is not None:  # not a request: a member's or leader's own view (badges)
            self.member = person if person.role == "member" else None
            self.leader = person if person.role == "tl" else None
        else:
            self.member, self.leader = participant_of(user), leader_of(user)
        self.person = self.member or self.leader
        self.readonly = mode in ("team", "excon")
        if mode == "desk":  # the persona desk (and ExCon there): every room with a persona, posting as one
            from .access import desk_of
            desk_person = desk_of(user)
            if not (desk_person or is_excon(user)):
                raise PermissionDenied("Persona desk only.")
            self.member = self.leader = None
            self.person = desk_person
            self.team, self.keys, self.key, self.base = None, [], "", "/chat/desk"
        elif mode == "excon":
            if not is_excon(user):
                raise PermissionDenied("ExCon only.")
            self.person = self.member = self.leader = None
            self.team, self.keys, self.key, self.base = None, [], "", "/chats"
        elif mode == "team":
            if not self.leader:
                raise PermissionDenied("Team leaders only.")
            self.team, self.keys, self.key, self.base = self.leader.team, [], f"l:{self.leader.id}", "/chat/team"
        else:
            if not self.person:
                raise PermissionDenied("SIMEX Messenger is for the teams and their leaders.")
            self.team = self.person.team
            if self.member:
                self.key = f"m:{self.member.id}"
                self.keys = [self.key, f"t:{self.team.id}"]
            else:
                self.key = f"l:{self.leader.id}"
                self.keys = [self.key]
            self.base = "/chat"
        self.desk = mode == "desk"
        self.sim_only = bool(self.member) and not self.readonly
        self.sees_ai = mode in ("team", "excon")
        self.sees_history = mode == "excon"  # edits and deleted text

    def cache_key(self):
        return f"msgr-rooms:{self.mode}:{self.person.id if self.person else 0}:{self.team.id if self.team else 0}"


def rooms(v):
    """The rooms this viewer may open."""
    if v.mode == "excon":
        return Room.objects.all()
    if v.mode == "team":
        return Room.objects.filter(Q(team=v.team) | Q(members__team=v.team)).distinct()
    if v.mode == "desk":
        return Room.objects.filter(members__key__startswith="p:").distinct()
    return Room.objects.filter(members__key__in=v.keys, members__left_at__isnull=True).distinct()


def room_ids(v, fresh=False):
    """rooms(v)'s ids, cached per process for ROOMS_TTL (poll() refreshes it on a member change)."""
    if v.mode == "excon":
        return None
    key = v.cache_key()
    ids = None if fresh else cache.get(key)
    if ids is None:
        ids = set(rooms(v).values_list("id", flat=True))
        cache.set(key, ids, ROOMS_TTL)
    return ids


def forget(v):
    cache.delete(v.cache_key())


def get_room(v, rid):
    ids = room_ids(v)
    if ids is not None and rid not in ids:
        ids = room_ids(v, fresh=True)
        if rid not in ids:
            return None
    return Room.objects.filter(pk=rid).first()


def joined_from(v, room):
    """The first post id this viewer may read in room (0: all), or None if they aren't in it."""
    if v.readonly:
        return 0
    if v.mode == "desk":  # what the personas in it can read: from the first one's joining
        got = RoomMember.objects.filter(room=room, key__startswith="p:").aggregate(n=Count("id"), first=Min("joined_post_id"))
        return got["first"] or 0 if got["n"] else None
    got = (RoomMember.objects.filter(room=room, key__in=v.keys, left_at__isnull=True)
           .aggregate(n=Count("id"), first=Min("joined_post_id")))
    return got["first"] or 0 if got["n"] else None


def can_post(v, room):
    if v.mode == "desk":
        return not room.frozen and RoomMember.objects.filter(room=room, key__startswith="p:", left_at__isnull=True).exists()
    return not v.readonly and not room.frozen and joined_from(v, room) is not None


def visible_posts(v, room):
    start = joined_from(v, room)
    if start is None:
        return Post.objects.none()
    return Post.objects.filter(room=room, id__gt=start)


# --- names -------------------------------------------------------------------------------------------

class Names:
    """Display names of keys, read once per request: a member by name (with their team when it
    isn't the viewer's), a leader as their team's leader, a team by name, a persona as the desk
    shows it."""

    def __init__(self, v=None):
        self.v = v
        self.people, self.teams, self.memo = {}, {}, {}

    def _person(self, pid):
        if pid not in self.people:
            self.people[pid] = Person.objects.select_related("team").filter(pk=pid).first()
        return self.people[pid]

    def _team(self, tid):
        if tid not in self.teams:
            self.teams[tid] = Team.objects.filter(pk=tid).first()
        return self.teams[tid]

    def load(self, keys):
        pids = {int(k[2:]) for k in keys if k[:2] in ("m:", "l:") and k[2:].isdigit()} - set(self.people)
        for p in Person.objects.select_related("team").filter(pk__in=pids):
            self.people[p.id] = p
        tids = {int(k[2:]) for k in keys if k.startswith("t:") and k[2:].isdigit()} - set(self.teams)
        for t in Team.objects.filter(pk__in=tids):
            self.teams[t.id] = t

    def name(self, key):
        kind, _, val = (key or "").partition(":")
        if kind in ("m", "l") and val.isdigit():
            p = self._person(int(val))
            if not p:
                return "Someone"
            if kind == "l":
                return f"{p.team.name} leader ({p.name})" if p.team_id else p.name
            if p.team_id and self.v and (self.v.team and p.team_id != self.v.team.id or getattr(self.v, "desk", False)):
                return f"{p.name} ({p.team.name})"
            return p.name
        if kind == "t" and val.isdigit():
            t = self._team(int(val))
            return t.name if t else "A team"
        if kind == "p":
            return self.persona(val)["name"]
        return key or ""

    def persona(self, local, team=None):
        from . import desk
        return desk.describe(local, team or (self.v.team if self.v else None), self.memo)

    def colour(self, key):
        from .templatetags.ops import avatar_colour
        if key.startswith("p:"):
            return self.persona(key[2:])["colour"]
        return avatar_colour(self.name(key))


def key_of_person(p):
    return f"l:{p.id}" if p.role == "tl" else f"m:{p.id}"


def post_key(post):
    if post.persona:
        return f"p:{post.persona}"
    return key_of_person(post.author) if post.author_id else ""


# --- who may chat with whom --------------------------------------------------------------------------

def book(v):
    """The keys this viewer may start a chat or a group with, or add to one: [{key, name, kind,
    org}]. A member: their teammates (with chat_direct), their leader, the personas (with
    chat_personas; never the team leader persona: the leader is here as themselves) and, with
    team_to_team, the other teams. A leader: their team's members."""
    if v.readonly:
        return []
    team = v.team
    out = []
    mates = Person.objects.participants().filter(team=team, active=True).order_by("name")
    if v.member:
        if conf.flag("chat_direct"):
            out += [{"key": f"m:{p.id}", "name": p.name, "kind": "member"} for p in mates if p.id != v.member.id]
        lead = team.leader()
        if lead:
            out.append({"key": f"l:{lead.id}", "name": f"{team.name} leader ({lead.name})", "kind": "leader"})
        if conf.flag("chat_personas"):
            out += persona_entries(team)
        if conf.flag("team_to_team"):
            out += [{"key": f"t:{t.id}", "name": t.name, "kind": "team"}
                    for t in Team.objects.filter(active=True, is_test=False).exclude(pk=team.pk)]
    else:
        out += [{"key": f"m:{p.id}", "name": p.name, "kind": "member"} for p in mates]
    return out


def persona_entries(team):
    from . import render
    from .models import Sender
    ctx = render.team_ctx(team)
    out = []
    for s in Sender.objects.filter(active=True, team_leader=False).order_by("display_name"):
        local = render.fill(s.local_part, ctx).lower()
        out.append({"key": f"p:{local}", "name": render.fill(s.display_name, ctx) or local, "kind": "persona",
                    "org": s.organisation})
    return out


def _team_of_key(key, names):
    kind, _, val = key.partition(":")
    if kind == "t" and val.isdigit():
        return int(val)
    if kind in ("m", "l") and val.isdigit():
        p = names._person(int(val))
        return p.team_id if p else None
    return None


def _check_keys(v, keys, names, existing=()):
    """Raise ChatError unless every key is in the viewer's book and the room keeps to one team
    (two only with team_to_team)."""
    allowed = {e["key"] for e in book(v)}
    for k in keys:
        if k not in allowed:
            raise ChatError("You can't chat with that person here.")
    teams = {t for t in (_team_of_key(k, names) for k in list(keys) + list(existing) + [v.key]) if t}
    if len(teams) > 1 and not conf.flag("team_to_team"):
        raise ChatError("Teams can't chat with each other in this exercise.")
    return teams


# --- rooms -------------------------------------------------------------------------------------------

def _member_row(room, key, names, admin=False, added_by="", joined_post_id=0):
    kind, _, val = key.partition(":")
    person = names._person(int(val)) if kind in ("m", "l") and val.isdigit() else None
    team_id = int(val) if kind == "t" else (person.team_id if person else None)
    return RoomMember.objects.create(room=room, key=key, person=person, persona=val if kind == "p" else "",
                                     team_id=team_id, admin=admin, added_by=added_by[:150], joined_post_id=joined_post_id)


def _refresh_teams(room):
    teams = sorted({t for t in RoomMember.objects.filter(room=room, left_at__isnull=True, team__isnull=False)
                   .values_list("team_id", flat=True)})
    room.teams = teams
    room.team_id = teams[0] if len(teams) == 1 else None
    room.has_persona = RoomMember.objects.filter(room=room, left_at__isnull=True, key__startswith="p:").exists()
    room.is_test = bool(teams) and Team.objects.filter(pk__in=teams, is_test=True).count() == len(teams)
    room.save(update_fields=["teams", "team", "has_persona", "is_test"])


def change(room, kind, post_id=None, key="", name=""):
    c = RoomChange.objects.create(room=room, kind=kind, post_id=post_id, key=key[:130], name=name[:200])
    cache.delete("msgr-head")
    return c


def team_room(team):
    """The team's own room (its participants and its leader), made the first time it is asked for;
    a new leader is added."""
    room = Room.objects.filter(kind="team", team=team).first()
    with transaction.atomic():
        if room is None:
            room = Room.objects.create(kind="team", team=team, teams=[team.id], title=team.name, is_test=team.is_test)
            RoomMember.objects.create(room=room, key=f"t:{team.id}", team=team)
            change(room, "room")
        lead = team.leader()
        if lead and not RoomMember.objects.filter(room=room, key=f"l:{lead.id}", left_at__isnull=True).exists():
            RoomMember.objects.create(room=room, key=f"l:{lead.id}", person=lead, team=team, admin=True)
            change(room, "member", key=f"l:{lead.id}")
    return room


def direct(v, key):
    """The direct room between the viewer and key, opened if new."""
    names = Names(v)
    _check_keys(v, [key], names)
    pair = "|".join(sorted([v.key, key]))
    room = Room.objects.filter(kind="direct", direct_key=pair).first()
    if room:
        return room, False
    with transaction.atomic():
        room = Room.objects.create(kind="direct", direct_key=pair, created_by=v.person)
        for k in (v.key, key):
            _member_row(room, k, names)
        _refresh_teams(room)
        change(room, "room")
        for k in (v.key, key):
            change(room, "member", key=k)
    forget(v)
    track(v, "chat_room", room, act="direct")
    return room, True


def create_group(v, title, keys):
    title = (title or "").strip()[:TITLE_MAX]
    keys = list(dict.fromkeys(k for k in keys if k and k != v.key))
    if not title:
        raise ChatError("Give the group a name.")
    if not keys:
        raise ChatError("Add someone to the group.")
    if len(keys) > GROUP_MAX:
        raise ChatError(f"A group has at most {GROUP_MAX} members.")
    if v.member and not conf.flag("chat_groups"):
        raise ChatError("Members can't start groups in this exercise.")
    names = Names(v)
    names.load(keys + [v.key])
    _check_keys(v, keys, names)
    with transaction.atomic():
        room = Room.objects.create(kind="group", title=title, created_by=v.person)
        _member_row(room, v.key, names, admin=True)
        for k in keys:
            _member_row(room, k, names, added_by=names.name(v.key))
        _refresh_teams(room)
        change(room, "room")
        system(room, f"{names.name(v.key)} created the group “{title}”")
        for k in [v.key] + keys:
            change(room, "member", key=k)
    forget(v)
    track(v, "chat_room", room, act="create", members=len(keys) + 1)
    return room


def system(room, text):
    p = Post.objects.create(room=room, kind="system", body=text[:500], is_test=room.is_test)
    Room.objects.filter(pk=room.pk).update(last_post_id=p.id, last_post_at=p.created_at)
    change(room, "post", post_id=p.id)
    return p


def _admin(v, room):
    return RoomMember.objects.filter(room=room, key=v.key, left_at__isnull=True, admin=True).exists()


def settings_action(v, room, act, keys=(), title=""):
    """rename | add | remove | leave | mute | unmute | pin | unpin."""
    if v.readonly or joined_from(v, room) is None:
        raise PermissionDenied
    names = Names(v)
    if act in ("mute", "unmute", "pin", "unpin"):
        st, _ = RoomState.objects.get_or_create(room=room, person=v.person)
        setattr(st, "muted" if act in ("mute", "unmute") else "pinned", act in ("mute", "pin"))
        st.save(update_fields=["muted", "pinned"])
        if act in ("mute", "unmute"):
            track(v, "chat_mute", room, on=act == "mute")
        return
    if room.kind != "group":
        raise ChatError("Only groups can be changed.")
    if room.frozen:
        raise ChatError("ExCon has frozen this chat.")
    me = names.name(v.key)
    if act == "leave":
        RoomMember.objects.filter(room=room, key=v.key, left_at__isnull=True).update(left_at=timezone.now())
        if not RoomMember.objects.filter(room=room, left_at__isnull=True, admin=True).exists():
            nxt = RoomMember.objects.filter(room=room, left_at__isnull=True).exclude(key__startswith="p:").order_by("id").first()
            if nxt:
                RoomMember.objects.filter(pk=nxt.pk).update(admin=True)
        _refresh_teams(room)
        system(room, f"{me} left")
        change(room, "member", key=v.key)
        forget(v)
        track(v, "chat_room", room, act="leave")
        return
    if not _admin(v, room):
        raise ChatError("Only the group's admins can do that.")
    if act == "rename":
        title = (title or "").strip()[:TITLE_MAX]
        if not title:
            raise ChatError("Give the group a name.")
        room.title = title
        room.save(update_fields=["title"])
        system(room, f"{me} renamed the group “{title}”")
        change(room, "room")
        track(v, "chat_room", room, act="rename")
    elif act == "add":
        have = set(RoomMember.objects.filter(room=room, left_at__isnull=True).values_list("key", flat=True))
        keys = [k for k in dict.fromkeys(keys) if k not in have]
        if not keys:
            raise ChatError("They are in the group already.")
        if len(have) + len(keys) > GROUP_MAX:
            raise ChatError(f"A group has at most {GROUP_MAX} members.")
        names.load(keys)
        _check_keys(v, keys, names, existing=have)
        with transaction.atomic():
            at = Room.objects.filter(pk=room.pk).values_list("last_post_id", flat=True).first() or 0
            for k in keys:
                _member_row(room, k, names, added_by=me, joined_post_id=at)  # reads from now on (plan, decision 5)
                change(room, "member", key=k)
            _refresh_teams(room)
            system(room, f"{me} added {', '.join(names.name(k) for k in keys)}")
        track(v, "chat_room", room, act="add", n=len(keys))
    elif act == "remove":
        gone = [k for k in keys if k != v.key]
        n = RoomMember.objects.filter(room=room, key__in=gone, left_at__isnull=True).update(left_at=timezone.now())
        if n:
            _refresh_teams(room)
            system(room, f"{me} removed {', '.join(names.name(k) for k in gone)}")
            for k in gone:
                change(room, "member", key=k)
            track(v, "chat_room", room, act="remove", n=n)
    else:
        raise ChatError("Unknown action.")


def members(room, names, current=True):
    qs = RoomMember.objects.filter(room=room)
    if current:
        qs = qs.filter(left_at__isnull=True)
    rows = list(qs.order_by("id"))
    names.load([m.key for m in rows])
    out = []
    for m in rows:
        d = {"key": m.key, "name": names.name(m.key), "admin": m.admin, "kind": m.key[0], "left": bool(m.left_at),
             "colour": names.colour(m.key)}
        if m.key.startswith("p:"):
            d["org"] = _org(m.persona)
        out.append(d)
    return out


def _org(local):
    from . import render
    s, _ = render.sender_by_local(local)
    return s.organisation if s else ""


def people_of(room, exclude=()):
    """The Person ids behind a room's current members (a team key: its active participants)."""
    ids, teams = set(), set()
    for key, pid, tid in RoomMember.objects.filter(room=room, left_at__isnull=True).values_list("key", "person_id", "team_id"):
        if key.startswith("t:"):
            teams.add(tid)
        elif pid:
            ids.add(pid)
    if teams:
        ids |= set(Person.objects.participants().filter(team_id__in=teams, active=True).values_list("id", flat=True))
    return ids - set(exclude)


def title_of(v, room, names, mems=None):
    if room.kind == "group":
        return room.title
    if room.kind == "team":
        return room.title or names.name(f"t:{room.team_id}")
    keys = [m["key"] for m in (mems if mems is not None else members(room, names, current=False))]
    others = [k for k in keys if k not in v.keys] or keys
    if v.mode == "desk":  # the team side, then the persona: "Jane Doe (Team A) ↔ Minister"
        side = [k for k in keys if not k.startswith("p:")]
        return " ↔ ".join([names.name(k) for k in side] + [names.name(k) for k in keys if k.startswith("p:")])
    if v.readonly:
        return " ↔ ".join(names.name(k) for k in keys)
    return names.name(others[0]) if others else "Chat"


# --- posting -----------------------------------------------------------------------------------------

def _mentions(room, keys, body):
    keys = list(keys) + MENTION.findall(body or "")
    have = set(RoomMember.objects.filter(room=room, left_at__isnull=True).values_list("key", flat=True))
    return [k for k in dict.fromkeys(keys) if k in have][:30]


HOOKS = []  # called after a team side post is saved, in its transaction: fn(post) (the persona desk, ops/messenger_desk.py)


def post(v, room, body, files=(), reply_to=None, mentions=(), client_key=""):
    """A team member's or leader's post. files: (name, content type, data). Returns (post, new)."""
    body = (body or "").replace("\r\n", "\n").strip()[:BODY_MAX]
    if not body and not files:
        raise ChatError("Write something first.")
    if not can_post(v, room):
        raise ChatError("ExCon has frozen this chat." if room.frozen else "You are not in this chat.")
    client_key = (client_key or "")[:40]
    if client_key:
        old = Post.objects.filter(room=room, client_key=client_key).first()
        if old:
            return old, False
    limit = int(conf.get("max_upload_mb") or 10) * 1024 * 1024
    if sum(len(f[2]) for f in files) > limit:
        raise ChatError(f"Files can be {conf.get('max_upload_mb')} MB in all.")
    reply = visible_posts(v, room).filter(pk=reply_to, kind="text").first() if reply_to else None
    with transaction.atomic():
        p = Post.objects.create(room=room, author=v.person, body=body, reply_to=reply, client_key=client_key,
                                mentions=_mentions(room, mentions, body), is_test=room.is_test)
        for name, ctype, data in files:
            PostAttachment.objects.create(post=p, filename=(name or "file")[:300],
                                          content_type=(ctype or "application/octet-stream")[:120], size=len(data), data=data)
        Room.objects.filter(pk=room.pk).update(last_post_id=p.id, last_post_at=p.created_at)
        change(room, "post", post_id=p.id)
        st, _ = RoomState.objects.get_or_create(room=room, person=v.person)
        if st.upto < p.id:
            RoomState.objects.filter(pk=st.pk).update(upto=p.id, read_at=p.created_at)
        for fn in HOOKS:
            fn(p)
        notify(room, p, exclude={v.person.id})
    track(v, "chat_post", room, files=len(files), reply=bool(reply))
    return p, True


def notify(room, p, exclude=(), title=None):
    """Push a new post to the room's people (not the author, not who muted it; once per burst:
    nobody is pushed again about a room until they read it or PUSH_GAP has passed)."""
    if not (push.enabled() or push._sender):
        return
    ids = people_of(room, exclude=exclude)
    if not ids:
        return
    now = timezone.now()
    states = {s.person_id: s for s in RoomState.objects.filter(room=room, person_id__in=ids)}
    send = []
    for pid in ids:
        st = states.get(pid)
        if st and st.muted:
            continue
        if st and st.pushed_at and (st.read_at is None or st.pushed_at > st.read_at) and now - st.pushed_at < PUSH_GAP:
            continue
        send.append(pid)
    if not send:
        return
    RoomState.objects.filter(room=room, person_id__in=send).update(pushed_at=now)
    for pid in set(send) - set(states):
        RoomState.objects.get_or_create(room=room, person_id=pid, defaults={"pushed_at": now})
    names = Names()
    who = title or names.name(post_key(p))
    where = room.title if room.kind in ("group", "team") else ""
    data = {"title": who if not where else f"{who} · {where}", "body": "New message" if not p.files.exists() else "Sent a file",
            "url": f"/chat/r/{room.id}/", "tag": f"room-{room.id}"}
    if conf.flag("chat_push_preview") and p.body:
        data["body"] = p.body[:80]
    push.notify_people(send, data, app="chat")


def edit(v, p, body):
    if v.readonly or p.author_id != (v.person.id if v.person else None) or p.kind != "text" or p.deleted_at:
        raise PermissionDenied
    body = (body or "").replace("\r\n", "\n").strip()[:BODY_MAX]
    if not body:
        raise ChatError("A message can't be empty: delete it instead.")
    minutes = int(conf.get("chat_edit_minutes") or 15)
    if timezone.now() - p.created_at > timedelta(minutes=minutes):
        raise ChatError(f"Messages can be edited for {minutes} minutes.")
    if body == p.body:
        return p
    with transaction.atomic():
        PostEdit.objects.create(post=p, body=p.body, by=v.person.name[:150])
        p.body, p.edited_at = body, timezone.now()
        p.save(update_fields=["body", "edited_at"])
        change(p.room, "edit", post_id=p.id)
    track(v, "chat_edit", p.room)
    return p


def delete(v, p, by_excon=""):
    """Deleted for everyone: its text stays for ExCon (and the AI readings, reports)."""
    if by_excon:
        who = f"ExCon ({by_excon})"
    else:
        if v.readonly or p.author_id != (v.person.id if v.person else None) or p.kind != "text":
            raise PermissionDenied
        who = v.person.name
    if p.deleted_at:
        return p
    p.deleted_at, p.deleted_by = timezone.now(), who[:150]
    p.save(update_fields=["deleted_at", "deleted_by"])
    change(p.room, "edit", post_id=p.id)
    if not by_excon:
        track(v, "chat_delete", p.room)
    return p


def react(v, p, emoji):
    if v.readonly or emoji not in EMOJI or p.kind != "text" or p.deleted_at:
        raise ChatError("Not here.")
    if not can_post(v, p.room):
        raise ChatError("You are not in this chat.")
    old = PostReaction.objects.filter(post=p, key=v.key).first()
    if old and old.emoji == emoji:
        old.delete()
    else:
        PostReaction.objects.update_or_create(post=p, key=v.key, defaults={"emoji": emoji, "name": Names(v).name(v.key)[:200],
                                                                            "at": timezone.now()})
    change(p.room, "react", post_id=p.id)
    track(v, "chat_react", p.room)


def mark_read(v, room, upto):
    """The viewer read room up to post id upto (never in a read-only view)."""
    if v.readonly or not v.person or v.desk:  # the desk's reading is its DeskConversation, never a receipt
        return
    upto = min(int(upto or 0), room.last_post_id or 0)
    st, _ = RoomState.objects.get_or_create(room=room, person=v.person)
    if upto <= st.upto:
        return
    now = timezone.now()
    RoomState.objects.filter(pk=st.pk).update(upto=upto, read_at=now)
    ReadMark.objects.create(room=room, person=v.person, upto=upto, at=now)
    change(room, "read", post_id=upto, key=v.key)


def typing(v, room):
    if not can_post(v, room):
        return
    if RoomChange.objects.filter(room=room, kind="typing", key=v.key,
                                 at__gte=timezone.now() - timedelta(seconds=3)).exists():
        return
    change(room, "typing", key=v.key, name=Names(v).name(v.key))


def track(v, action, room, **detail):
    """A participant's (or leader's) action, for the reports (MailAction chat_*, left out of the
    inbox figures). Reads are ReadMarks."""
    if v.readonly or not v.person:
        return None
    return MailAction.objects.create(team=v.team, owner=None, person=v.person, action=action,
                                     threads=[f"chat:{room.id}"], detail=detail)


# --- what the clients read ---------------------------------------------------------------------------

def when(v, created, sim_at):
    """{"t", "day", "ms", "sim"?}: a post's time as this viewer sees it (members: the scenario's)."""
    on = sim_at is not None and simclock.enabled()
    shown = sim_at if (on and v.sim_only) else created
    local = timezone.localtime(shown)
    out = {"t": local.strftime("%H:%M"), "day": local.strftime("%a %d %b"), "ms": int(created.timestamp() * 1000)}
    if on and not v.sim_only:
        out["sim"] = timezone.localtime(sim_at).strftime("%a %d %b %H:%M")
    return out


def post_json(v, p, names, reads=None):
    key = post_key(p)
    deleted = bool(p.deleted_at)
    d = {"id": p.id, "room": p.room_id, "kind": p.kind, "by": key, "name": names.name(key) if key else "",
         "colour": names.colour(key) if key else "",
         "mine": bool(key and key == v.key and not v.readonly) or (v.desk and bool(p.persona)),
         "body": "" if deleted and not v.sees_history else p.body, "deleted": deleted, "edited": bool(p.edited_at),
         "mentions": p.mentions or [], **when(v, p.created_at, p.sim_at)}
    if p.reply_to_id and p.reply_to:
        r = p.reply_to
        rk = post_key(r)
        d["reply"] = {"id": r.id, "name": names.name(rk), "body": "This message was deleted" if r.deleted_at and not
                      v.sees_history else r.body[:140]}
    if not deleted or v.sees_history:
        d["files"] = [{"id": f.id, "name": f.filename, "size": f.size, "image": (f.content_type or "").startswith("image/"),
                       "url": f"{v.base}/f/{f.id}/"} for f in p.files.all()]
    reacts = {}
    for r in p.reactions.all():
        reacts.setdefault(r.emoji, []).append(r.name if r.key != v.key else "You")
    d["reactions"] = reacts
    d["my_reaction"] = next((r.emoji for r in p.reactions.all() if r.key == v.key), "")
    if v.sees_history:
        if deleted:
            d["deleted_by"] = p.deleted_by
            d["deleted_at"] = timezone.localtime(p.deleted_at).strftime("%a %d %b %H:%M")
        d["edits"] = [{"body": e.body, "at": timezone.localtime(e.at).strftime("%H:%M")} for e in p.edits.all()]
        if p.npc:
            d["npc"] = True
    if reads is not None and d["mine"]:
        d["seen"] = reads.get(p.id, 0)
    return d


def _with(qs):
    return qs.select_related("author", "author__team", "reply_to", "reply_to__author").prefetch_related(
        "files", "reactions", "edits")


def page(v, room, before=None, names=None):
    """The latest PAGE posts (before a post id), oldest first, and whether there are older ones."""
    names = names or Names(v)
    qs = visible_posts(v, room)
    if before:
        qs = qs.filter(id__lt=before)
    got = list(_with(qs.order_by("-id"))[:PAGE + 1])
    more = len(got) > PAGE
    got = got[:PAGE][::-1]
    names.load({post_key(p) for p in got if post_key(p)})
    reads = receipts(v, room)
    return [post_json(v, p, names, reads) for p in got], more


def receipts(v, room):
    """{post id: how many others have read it}, for the viewer's own posts (the ticks)."""
    if v.readonly or not v.person:
        return {}
    others = sorted(RoomState.objects.filter(room=room).exclude(person=v.person).values_list("upto", flat=True))
    mine = list(Post.objects.filter(room=room, author=v.person).order_by("-id").values_list("id", flat=True)[:PAGE])
    out = {}
    for pid in mine:
        out[pid] = sum(1 for u in others if u >= pid)
    return out


def room_list(v, names=None, only=None):
    """The viewer's rooms, newest first (pinned first): name, kind, last post, unread."""
    names = names or Names(v)
    qs = rooms(v)
    if only is not None:
        qs = qs.filter(id__in=only)
    rs = list(qs.order_by("-last_post_id"))
    if not rs:
        return []
    states = {s.room_id: s for s in RoomState.objects.filter(room__in=rs, person=v.person)} if v.person and not v.readonly else {}
    mems = {}
    for m in RoomMember.objects.filter(room__in=rs).order_by("id"):
        mems.setdefault(m.room_id, []).append(m)
    names.load({m.key for ms in mems.values() for m in ms})
    last_ids = [r.last_post_id for r in rs if r.last_post_id]
    lasts = {p.id: p for p in Post.objects.filter(id__in=last_ids).select_related("author").prefetch_related("files")}
    joined = {}
    if not v.readonly:
        for m in RoomMember.objects.filter(room__in=rs, key__in=v.keys, left_at__isnull=True):
            joined[m.room_id] = min(joined.get(m.room_id, m.joined_post_id), m.joined_post_id)
    unread = {}
    if states or (not v.readonly and v.person):
        q = Q()
        for r in rs:
            floor = max(states[r.id].upto if r.id in states else 0, joined.get(r.id, 0))
            if r.last_post_id > floor:
                q |= Q(room_id=r.id, id__gt=floor)
        if q:
            for rid, n in (Post.objects.filter(q, kind="text", deleted_at__isnull=True).exclude(author=v.person)
                           .values_list("room_id").annotate(n=Count("id"))):
                unread[rid] = n
    out = []
    for r in rs:
        ms = [{"key": m.key, "left": bool(m.left_at)} for m in mems.get(r.id, [])]
        last = lasts.get(r.last_post_id)
        if last and joined.get(r.id, 0) >= last.id:
            last = None  # before they joined
        st = states.get(r.id)
        others = [m["key"] for m in ms if not m["left"] and m["key"] not in v.keys]
        avatar = others[0] if r.kind == "direct" and others else (f"t:{r.team_id}" if r.kind == "team" else "")
        d = {"id": r.id, "kind": r.kind, "name": title_of(v, r, names, ms), "unread": unread.get(r.id, 0),
             "muted": bool(st and st.muted), "pinned": bool(st and st.pinned), "frozen": r.frozen,
             "colour": names.colour(avatar) if avatar else "#5f6368", "avatar": avatar[:1],
             "persona": avatar.startswith("p:"), "teams": r.teams, "has_persona": r.has_persona,
             "members": sum(1 for m in ms if not m["left"]), "last": None}
        if last:
            key = post_key(last)
            text = ("This message was deleted" if last.deleted_at and not v.sees_history else
                    last.body or ("📎 " + ", ".join(f.filename for f in last.files.all())))
            text = MENTION.sub(lambda m: "@" + names.name(m.group(1)).split(" (")[0], text)
            d["last"] = {"id": last.id, "body": text[:120], "name": "You" if key == v.key and not v.readonly else
                         (names.name(key).split(" (")[0] if key else ""), "system": last.kind == "system",
                         **when(v, last.created_at, last.sim_at)}
        out.append(d)
    out.sort(key=lambda d: (not d["pinned"], -(d["last"]["id"] if d["last"] else 0)))
    return out


def head():
    """The newest change's id, cached HEAD_TTL per process (changes made here clear it)."""
    h = cache.get("msgr-head")
    if h is None:
        h = RoomChange.objects.aggregate(m=Max("id"))["m"] or 0
        cache.set("msgr-head", h, HEAD_TTL)
    return h


def poll(v, cursor, open_room=None):
    """What changed since cursor for this viewer: None when nothing did (the client gets a 204),
    else {"c": new cursor, "rooms": [changed rooms], "posts": [the open room's new or changed
    posts], "typing": [names], "unread": total}."""
    h = head()
    if cursor and cursor >= h:
        return None
    if not cursor:
        return {"c": h, "rooms": _lister(v)(v), "posts": [], "typing": [], "unread": None}
    ids = room_ids(v)
    if v.mode != "excon":
        moved = RoomChange.objects.filter(id__gt=cursor, kind__in=("member", "room"))
        if v.mode == "team" or moved.filter(Q(key__in=v.keys) | Q(room_id__in=ids)).exists():
            ids = room_ids(v, fresh=True)
    qs = RoomChange.objects.filter(id__gt=cursor, id__lte=h)
    if ids is not None:
        qs = qs.filter(room_id__in=ids)
    changes = list(qs.order_by("id")[:CHANGES_MAX])
    new_c = changes[-1].id if len(changes) == CHANGES_MAX else h
    rooms_changed = {c.room_id for c in changes if c.kind != "typing"}
    names = Names(v)
    out = {"c": new_c, "rooms": _lister(v)(v, only=rooms_changed) if rooms_changed else [], "posts": [],
           "typing": [], "unread": None}
    if open_room is not None and open_room.id in (ids if ids is not None else {open_room.id}):
        pids = {c.post_id for c in changes if c.room_id == open_room.id and c.kind in ("post", "edit", "react")
                and c.post_id}
        if pids:
            got = list(_with(visible_posts(v, open_room).filter(id__in=pids)).order_by("id"))
            names.load({post_key(p) for p in got if post_key(p)})
            reads = receipts(v, open_room)
            out["posts"] = [post_json(v, p, names, reads) for p in got]
        if any(c.room_id == open_room.id and c.kind == "read" for c in changes):
            out["seen"] = receipts(v, open_room)
        since = timezone.now() - timedelta(seconds=TYPING_SECONDS)
        out["typing"] = sorted({c.name for c in RoomChange.objects.filter(room=open_room, kind="typing", at__gte=since)
                               .exclude(key=v.key)})
    if rooms_changed:
        out["unread"] = unread_total(v)
    return out


def _lister(v):
    if v.desk:
        return messenger_desk.desk_rooms
    return lambda v, only=None: room_list(v, only=only)


def unread_total(v):
    """Rooms with unread posts (the app's badge); on the desk, the rooms needing attention."""
    if v.desk:
        return messenger_desk.attention_count()
    if v.readonly or not v.person:
        return 0
    return sum(1 for r in room_list(v) if r["unread"] and not r["muted"])


def unread_count(person):
    """For SIMEX Mail's app drawer: this member's or leader's rooms with unread posts (None if off)."""
    if not enabled() or not person or not person.team_id:
        return None
    try:
        return unread_total(ChatViewer(None, person=person))
    except Exception:
        log.exception("chat unread count failed")
        return None


# --- the Files view (ops/file_index.py) -------------------------------------------------------------

def _file_rows(ctx, f):
    from .file_index import file_type
    qs = PostAttachment.objects.filter(post__deleted_at__isnull=True).select_related(
        "post", "post__room", "post__author").defer("data", "post__body")
    if ctx.who == "leader":
        qs = qs.filter(Q(post__room__team=ctx.team) | Q(post__room__members__team=ctx.team)).distinct()
    elif f.team:
        qs = qs.filter(Q(post__room__team=f.team) | Q(post__room__members__team=f.team)).distinct()
    if not f.test:
        qs = qs.filter(post__room__is_test=False)
    if not f.injects:
        qs = qs.filter(attachment__isnull=True)
    names = Names()
    for a in qs.order_by("-id")[:20000]:
        p, room = a.post, a.post.room
        key = post_key(p)
        team_id = room.team_id or (p.author.team_id if p.author_id else None) or (room.teams[0] if room.teams else None)
        if not team_id:
            continue
        if p.persona:
            direction = "from_persona"
        elif room.has_persona:
            direction = "to_persona"
        elif len(room.teams or []) > 1:
            direction = "to_team"
        else:
            direction = "internal"
        url = {"leader": f"/chat/team/r/{room.id}/", "excon": f"/chats/{room.id}/#p{p.id}"}.get(ctx.who, "")
        title = room.title if room.kind != "direct" else "Direct chat"
        yield {
            "group_key": ("c", a.id), "src": "c", "fid": a.id, "source": "chat", "filename": a.filename,
            "content_type": a.content_type, "size": a.size, "sha": a.sha256, "type": file_type(a.filename, a.content_type),
            "team_id": team_id, "direction": direction, "sender": names.name(key) if key else "",
            "sender_key": key, "colour": names.colour(key) if key.startswith("p:") else "", "to": title,
            "at": p.created_at, "sim_at": p.sim_at, "inject_id": None, "inject": "", "subject": f"Messenger: {title}",
            "box": "Messenger", "url": url, "thread": f"c:{room.id}", "desk_copy": False,
            "is_inject_doc": bool(a.attachment_id)}


def _register():
    from . import file_index
    file_index.register(_file_rows)


_register()


# --- clearing (ops/reset.py) -------------------------------------------------------------------------

def clear(team=None, direct_only=False):
    """Every chat (team None), or a team's: every room it has a member in (direct_only: only its
    members' direct chats). Rooms, posts, files, reads and the change feed go together; a team's
    own room comes back empty the next time it is opened."""
    qs = Room.objects.all()
    if team is not None:
        qs = qs.filter(Q(team=team) | Q(members__team=team)).distinct()
    if direct_only:
        qs = qs.filter(kind="direct")
    ids = list(qs.values_list("id", flat=True))
    n = Post.objects.filter(room_id__in=ids).exclude(kind="system").count()
    Room.objects.filter(id__in=ids).delete()
    if team is None:
        RoomChange.objects.all().delete()
    MailAction.objects.filter(action__startswith="chat_", **({"team": team} if team is not None else {})).delete()
    cache.delete("msgr-head")
    return n


from . import messenger_desk  # noqa: E402,F401  (the persona desk's hook on team posts)

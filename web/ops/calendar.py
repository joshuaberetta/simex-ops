"""SIMEX Calendar (plan: docs/CALENDAR_PLAN.md): meetings and appointments on the calendars of
the mailboxes. A calendar belongs to a mailbox, (team, owner) as Mail: the team's shared
calendar, a member's or a team leader's own, and the persona desk's, one layer per persona. An
event belongs to its organiser's mailbox and shows on each calendar with a CalendarAttendee row
for it: the organiser's own, and one per guest (a team, a member, a team leader, a persona: on the
persona desk's calendar; the team leader persona: on its leader's).

Guests are invited by mail from the organiser's mailbox, through the same paths as any message
sent from it (views_inbox._send / _send_as_leader, desk.send), so push, the persona desk's copies
and sections, the Network, MailActions, the simulation time and withdrawing work as for any mail.
Every Mail an invitation makes, in every mailbox, carries Mail.event and Mail.invite (request,
update, cancel, reply): SENDING, read by Mail.save. Who may be invited is the organiser
mailbox's address book, checked on save as on send. Guests answer Yes / Maybe / No (respond),
with a short reply mail to the organiser in the same conversation. AI personas never answer an
invitation (npc.on_response leaves them to the desk).

Times are kept real, like everything in the DB. A member of a team sees and enters them in the
simulation time when the clock is on (ops/simclock.py: at / real_at), and never a real date;
team leaders, the persona desk and ExCon see the real time with the simulation time beside it.
So when ExCon moves the clock, an event keeps its real time and its simulation time moves. The
invitation mails, which the same Mail's copies show to everyone, give the scenario's time when
the clock is on.

Every query goes through the viewer's layers (layers(v)), which come from the Viewer's own
mailboxes: teams never cross. Participant-facing only: nothing control-side is read here.
"""

import contextvars
import copy
import re
import zlib
from contextlib import contextmanager
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import addresses, conf, desk, render as rnd, simclock
from .models import CalendarAttendee, CalendarEvent, Mail, Person, Team

MODES = [("in_person", "In person"), ("phone", "Phone"), ("vtc", "VTC"), ("other", "Other")]
TITLE_MAX, LOCATION_MAX, DESCRIPTION_MAX, NOTE_MAX = 200, 200, 4000, 500
MAX_DAYS = 14  # the longest event
MAX_RANGE_DAYS = 62  # the longest range one request lists
MAX_GUESTS = 40
FMT = "%Y-%m-%dT%H:%M"
ANSWERS = {"accepted": "Accepted", "tentative": "Maybe", "declined": "Declined"}
# Layer colours: the team's calendar, the viewer's own, a team leader's (the persona desk's
# layers take each persona's colour, desk.colour).
TEAM_COLOUR, OWN_COLOUR, LEADER_COLOUR = ("#4a86e8", "#fff"), ("#16a766", "#fff"), ("#a479e2", "#fff")
MEMBER_COLOURS = [("#ffad47", "#462a00"), ("#f691b3", "#4d0f25"), ("#43d692", "#063d22"), ("#fad165", "#4a3b00"),
                  ("#fb4c2f", "#fff"), ("#8e8e8e", "#fff")]
# (event id, invite kind, thread) while an invitation is being sent: Mail.save puts them on every
# Mail it makes, in every mailbox (the team's, members', the leader's, the desk's).
SENDING = contextvars.ContextVar("calendar_sending", default=None)


class CalendarError(ValueError):
    pass


@contextmanager
def _sending(ev, kind):
    token = SENDING.set((ev.id, kind, ev.uid))
    try:
        yield
    finally:
        SENDING.reset(token)


# --- time: what the viewer sees ------------------------------------------------------------------

def clock(v):
    """"sim" when the viewer sees (and enters) the simulation time only (a member, with the
    clock on), else "real"."""
    from .views_inbox import sees_sim_only
    return "sim" if sees_sim_only(v) and simclock.enabled() else "real"


def shows_both(v):
    """The viewer sees the simulation time beside the real time."""
    return clock(v) == "real" and simclock.enabled()


def shown(dt, clk):
    """A real (aware) time as the viewer's clock reads it: "YYYY-MM-DDTHH:MM"."""
    if dt is None:
        return ""
    if clk == "sim":
        dt = simclock.at(dt) or dt
    return timezone.localtime(dt + timedelta(seconds=30)).strftime(FMT)  # to the nearest minute


def parse_shown(text, clk):
    """A "YYYY-MM-DDTHH:MM" (or date) read on the viewer's clock -> the real aware time, or None."""
    text = (text or "").strip()
    if len(text) == 10:
        text += "T00:00"
    try:
        naive = datetime.strptime(text[:16], FMT)
    except ValueError:
        return None
    aware = timezone.make_aware(naive, timezone.get_current_timezone())
    return simclock.real_at(aware) if clk == "sim" else aware


def now_shown(clk):
    """Now, on the viewer's clock, and how fast that clock runs (for the "now" line)."""
    now = timezone.now()
    return shown(now, clk), (simclock.rate() or 1.0) if clk == "sim" else 1.0


def when_text(ev, clk):
    """"Tue 20 May 2035, 11:00 – 12:00" on that clock (all day, several days as such)."""
    a, b = shown(ev.starts_at, clk), shown(ev.ends_at, clk)
    da, db = datetime.strptime(a, FMT), datetime.strptime(b, FMT)
    day = lambda d: f"{d:%a} {d.day} {d:%b %Y}"
    if ev.all_day:
        last = db - timedelta(days=1) if db.time() == datetime.min.time() else db
        return f"{day(da)} (all day)" if last.date() <= da.date() else f"{day(da)} – {day(last)} (all day)"
    if da.date() == db.date() or (db - da <= timedelta(days=1) and db.time() == datetime.min.time()):
        return f"{day(da)}, {da:%H:%M} – {db:%H:%M}"
    return f"{day(da)} {da:%H:%M} – {day(db)} {db:%H:%M}"


def mail_when(ev):
    """The time an invitation mail gives: the scenario's when the clock is on (everyone who
    reads the mail, members included, sees it), else the real one."""
    if simclock.enabled():
        return when_text(ev, "sim") + " (scenario time)"
    return when_text(ev, "real")


# --- layers: the calendars a viewer sees -------------------------------------------------------

def _layer(key, name, colour, team, owner, persona="", writable=False, on=True, org_key="", org_name="", kind="",
           base=""):
    return {"key": key, "name": name, "colour": colour[0], "fg": colour[1], "team": team, "owner": owner,
            "persona": persona, "writable": writable, "on": on, "org_key": org_key, "org_name": org_name, "kind": kind,
            "base": base}


def _member_colour(p):
    return MEMBER_COLOURS[zlib.crc32(str(p.id).encode()) % len(MEMBER_COLOURS)]


def layers(v):
    """The calendars the viewer sees, as layers: the persona desk one per persona; a member the
    team's and (personal mailboxes) their own; a team leader their own, the team's and their
    members' (read-only); ExCon (and the desk) viewing a team every mailbox of that team,
    read-only, the one viewed shown. Another team leader's mailbox (peer) has no calendar here.
    Each layer's base is the SIMEX Mail address of its mailbox for this viewer."""
    from .views_inbox import leader_name, team_members
    out = []
    write = not v.readonly
    if v.desk:
        for s in desk.personas():
            out.append(_layer(f"p:{s.local_part}", s.display_name or s.local_part, desk.colour(s), None, None,
                              persona=s.local_part, writable=write, org_key=f"p:{s.local_part}",
                              org_name=s.display_name or s.local_part, kind="persona", base="/inbox/desk"))
        return out
    if v.peer:
        return []
    team = v.team
    personal = conf.flag("personal_mailboxes")
    team_layer = lambda **kw: _layer("t", f"{team.name} calendar", TEAM_COLOUR, team, None, org_key=f"t:{team.id}",
                                     org_name=team.name, kind="team", **kw)
    own_layer = lambda p, **kw: _layer(f"o{p.id}", "My calendar", OWN_COLOUR, team, p, org_key=f"l:{p.id}" if p.is_leader
                                       else f"m:{p.id}", org_name=leader_name(p, team) if p.is_leader
                                       else addresses.member_name(p, team), kind="own", **kw)
    member_layers = lambda base, on_pid=None: [
        _layer(f"o{p.id}", p.name, _member_colour(p), team, p, on=p.id == on_pid, org_key=f"m:{p.id}",
               org_name=addresses.member_name(p, team), kind="member", base=base(p))
        for p in team_members(team).filter(active=True)] if personal else []
    if v.leader:  # their own (theirs to change from /inbox/ only), the team's and their members', read-only
        own = v.leader
        out.append(own_layer(own, writable=write and v.owner == own and not v.leader_mode, base="/inbox"))
        out.append(team_layer(on=True, base="/inbox/team"))
        out += member_layers(lambda p: f"/inbox/members/{p.id}", v.owner.id if v.member_box else None)
        return out
    if v.person:  # a member: the team's and their own
        out.append(team_layer(writable=write, base="/inbox"))
        if personal:
            out.append(own_layer(v.person, writable=write, base="/inbox/me"))
        return out
    # ExCon or the persona desk viewing a team: every mailbox of it, read-only; the one viewed shown.
    viewed = v.owner.id if v.owner else None
    lead = team.leader()
    root = f"/inbox/as/{team.id}"
    out.append(team_layer(on=viewed is None, base=root))
    if lead:
        out.append(_layer(f"o{lead.id}", f"{team.name} leader ({lead.name})", LEADER_COLOUR, team, lead,
                          on=viewed == lead.id, org_key=f"l:{lead.id}", org_name=leader_name(lead, team), kind="leader",
                          base=f"{root}/leader"))
    out += member_layers(lambda p: f"{root}/member/{p.id}", viewed)
    return out


def _tid(x):
    return x.id if x is not None else None


def _box_q(layer):
    q = Q(team=layer["team"], owner=layer["owner"])
    if layer["persona"]:
        q &= Q(persona=layer["persona"])
    return q


def _rows(layer_list):
    if not layer_list:
        return CalendarAttendee.objects.none()
    q = Q()
    for lay in layer_list:
        q |= _box_q(lay)
    return CalendarAttendee.objects.filter(q, hidden=False)


def _layer_of(row, layer_list):
    for lay in layer_list:
        if row.team_id == _tid(lay["team"]) and row.owner_id == _tid(lay["owner"]) and \
                (not lay["persona"] or row.persona == lay["persona"]):
            return lay
    return None


def layer_of_box(v, team_id, owner_id, persona="", lays=None):
    """The viewer's layer for a mailbox (persona: on the desk), or None."""
    for lay in (layers(v) if lays is None else lays):
        if _tid(lay["team"]) == team_id and _tid(lay["owner"]) == owner_id and (not lay["persona"] or lay["persona"] == persona):
            return lay
    return None


# --- guests: who an event's mailbox may invite --------------------------------------------------

def _guest_box(e):
    """The mailbox whose calendar a guest's row is on: {team, owner, persona, person}, or None.
    A member's own (personal mailboxes; else the team's, for them), a team leader's own, the
    team leader persona's leader, a persona's on the desk."""
    k = e["key"]
    if k.startswith("t:"):
        t = Team.objects.filter(pk=int(k[2:]), active=True).first()
        return t and {"team": t, "owner": None, "persona": "", "person": None}
    if k.startswith(("m:", "l:")):
        p = Person.objects.filter(pk=int(k[2:]), active=True, team__isnull=False).select_related("team").first()
        if not p:
            return None
        own = p if (p.is_leader or conf.flag("personal_mailboxes")) else None
        return {"team": p.team, "owner": own, "persona": "", "person": p}
    if k.startswith("p:"):
        local = k[2:]
        tid = rnd.leader_locals().get(local)
        if tid:  # the team leader persona: its leader plays it
            t = Team.objects.filter(pk=tid).first()
            lead = t.leader() if t else None
            return lead and {"team": t, "owner": lead, "persona": "", "person": lead}
        return {"team": None, "owner": None, "persona": local, "person": None}
    return None


def guest_entries(w, keys, lay):
    """Address-book entries of the guests keys, from the organiser's mailbox w: exactly who it
    may write to (views_inbox.address_book, as on send), less the organiser itself."""
    from .views_inbox import _resolve, address_book
    keys = [k for k in dict.fromkeys((k or "").strip() for k in keys) if k and k != lay["org_key"]]
    if len(keys) > MAX_GUESTS:
        raise CalendarError(f"{MAX_GUESTS} guests at most.")
    entries = _resolve(keys, address_book(w), w)
    got = {e["key"] for e in entries}
    bad = [k for k in keys if k not in got]
    if bad:
        raise CalendarError("This calendar can't invite " + ", ".join(b.split(":", 1)[-1] for b in bad)
                            + ": only those its mailbox can write to.")
    if w.leader_box or w.desk:
        pass
    elif not conf.flag("team_to_team") and any(e["kind"] == "team" and e["key"] != f"t:{w.team.id}" for e in entries):
        raise CalendarError("Other teams can't be invited (Settings: team-to-team messages are off).")
    out = []
    for e in entries:
        box = _guest_box(e)
        if box is None:
            raise CalendarError(f"{e['name']} can't be invited.")
        if (_tid(box["team"]), _tid(box["owner"]), box["persona"]) == (_tid(lay["team"]), _tid(lay["owner"]), lay["persona"]):
            raise CalendarError(f"{e['name']}: that is this calendar's own mailbox.")
        out.append((e, box))
    return out


# --- reading ------------------------------------------------------------------------------------

def _guests(ev):
    return [{"key": a.key, "name": a.name, "response": a.response, "role": a.role}
            for a in ev.attendees.all() if a.role != "organiser" and not a.hidden]


def _thread_url(ev, lay):
    """The invitation's conversation in that layer's mailbox, if it has it."""
    if not lay or not lay["base"]:
        return ""
    m = (Mail.objects.filter(event=ev, team=lay["team"], owner=lay["owner"]).exclude(folder="drafts")
         .order_by("-id").values_list("thread", flat=True).first())
    return f"{lay['base']}/t/{m}/" if m else ""


def as_json(ev, layer, clk, both, row=None, lays=None):
    """An event as SIMEX Calendar's page uses it: times on the viewer's clock only (a member
    with the clock on never gets a real time), the simulation times beside them for the others."""
    guest_row = row is not None and row.role != "organiser"
    out = {"id": ev.id, "uid": ev.uid, "title": ev.title, "location": ev.location, "description": ev.description,
           "mode": ev.mode, "allDay": ev.all_day, "status": ev.status, "start": shown(ev.starts_at, clk),
           "end": shown(ev.ends_at, clk), "layer": layer["key"], "colour": layer["colour"], "fg": layer["fg"],
           "organiser": ev.organiser_name, "calendar": layer["name"],
           "editable": bool(layer["writable"] and not guest_row and ev.status == "confirmed"),
           "guests": _guests(ev), "guestKeys": [a.key for a in ev.attendees.all() if a.role != "organiser"],
           "response": row.response if guest_row else "", "comment": row.comment if guest_row else "",
           "answerable": bool(layer["writable"] and guest_row and ev.status == "confirmed"),
           "invite": _thread_url(ev, layer)}
    if both:
        out["sim"] = {"start": shown(ev.starts_at, "sim"), "end": shown(ev.ends_at, "sim")}
    if ev.source_mail_id and lays is not None:
        src = ev.source_mail
        lay = layer_of_box(None, src.team_id, src.owner_id, src.persona if src.team_id is None else "", lays) if src else None
        if lay and lay["base"]:
            out["source"] = {"subject": src.subject, "url": f"{lay['base']}/t/{src.thread}/"}
    return out


def events(v, start_text, end_text, only=None):
    """The events of the viewer's layers (only: those keys) that overlap [start, end) on the
    viewer's clock, as as_json dicts (cancelled ones too: shown struck through)."""
    clk, both = clock(v), shows_both(v)
    start, end = parse_shown(start_text, clk), parse_shown(end_text, clk)
    if not start or not end or end <= start:
        raise CalendarError("Give a range: from and to.")
    if end - start > timedelta(days=MAX_RANGE_DAYS):
        end = start + timedelta(days=MAX_RANGE_DAYS)
    all_lays = layers(v)
    lays = [lay for lay in all_lays if only is None or lay["key"] in only]
    rows = (_rows(lays).filter(event__starts_at__lt=end, event__ends_at__gt=start)
            .select_related("event", "event__source_mail").prefetch_related("event__attendees")
            .order_by("event__starts_at", "id"))
    # One entry per event, on every layer it is on (layers): shown as the organiser's row if the
    # viewer has it, else as the first guest row.
    best, order = {}, []
    for row in rows:
        lay = _layer_of(row, lays)
        if not lay:
            continue
        cur = best.get(row.event_id)
        if cur is None:
            best[row.event_id] = cur = [row, lay, [], None]
            order.append(row.event_id)
        elif row.role == "organiser" and cur[0].role != "organiser":
            cur[0], cur[1] = row, lay
        cur[2].append(lay["key"])
        if row.role != "organiser" and lay["writable"] and cur[3] is None:
            cur[3] = (row, lay)  # a guest row the viewer answers from
    out = []
    for eid in order:
        row, lay, keys, guest = best[eid]
        e = as_json(row.event, lay, clk, both, row, all_lays)
        e["layers"] = list(dict.fromkeys(keys))
        e["answerLayer"] = lay["key"]
        if guest and not e["answerable"] and row.event.status == "confirmed":
            e.update(answerable=True, response=guest[0].response, comment=guest[0].comment, answerLayer=guest[1]["key"])
        out.append(e)
    return out


def page_data(v, prefill=None):
    """What the Calendar page's script starts from (prefill: an event to create from an email)."""
    from .views_inbox import contacts
    clk = clock(v)
    now, rate = now_shown(clk)
    lays = layers(v)
    writable = any(lay["writable"] for lay in lays)
    return {"base": v.base, "clock": clk, "both": shows_both(v), "now": now, "rate": rate,
            "readonly": v.readonly or not writable,
            "layers": [{k: lay[k] for k in ("key", "name", "colour", "fg", "writable", "on", "kind")} for lay in lays],
            "modes": MODES, "simNow": shown(timezone.now(), "sim") if shows_both(v) else "",
            "simRate": (simclock.rate() or 1.0) if shows_both(v) else None,
            "book": [{k: b[k] for k in ("key", "name", "addr", "kind")} for b in contacts(v)] if writable else [],
            "prefill": prefill}


def prefill(v, thread):
    """A new event from a conversation (a thread's Create event): its subject, the people in it
    the viewer may invite, and the latest message as the source."""
    from .views_inbox import _key_for_sender, _resolve, address_book
    mails = list(v.mails().filter(thread=thread).exclude(folder="drafts").order_by("id"))
    if not mails:
        return None
    keys = []
    memo = {}
    for m in mails:
        if v.desk:
            if m.desk_team_id:
                keys.append(f"t:{m.desk_team_id}")
            continue
        if m.folder == "inbox":
            keys.append(_key_for_sender(m, memo))
        r = m.recipients or {}
        keys += list(r.get("to", [])) + list(r.get("cc", []))
    lays = [lay for lay in layers(v) if lay["writable"]]
    lay = lays[0] if lays else None
    if v.desk:
        lay = next((x for x in lays if x["persona"] == next((m.persona for m in mails if m.persona), "")), lay)
    mine = {lay["org_key"]} if lay else set()
    if v.member_box:
        mine.add(f"m:{v.owner.id}")
    book = address_book(v)
    guests = [{k: e[k] for k in ("key", "name", "addr", "kind")}
              for e in _resolve([k for k in dict.fromkeys(keys) if k and k not in mine], book, v)]
    title = re.sub(r"^\s*((re|fwd?|aw|tr)\s*:\s*)+", "", mails[0].subject or "", flags=re.I).strip()
    return {"title": title[:TITLE_MAX], "guests": guests, "source": mails[-1].id, "subject": mails[-1].subject,
            "layer": lay["key"] if lay else ""}


def cards(v, mails):
    """{mail id: card} for the invitation mails of a conversation (request, update, cancel): the
    event as the viewer's clock reads it, its guests and their answers, and, on the latest
    invitation of an event in a mailbox the viewer writes from, Yes / Maybe / No."""
    ids = {m.event_id for m in mails if m.event_id}
    if not ids:
        return {}
    evs = {e.id: e for e in CalendarEvent.objects.filter(pk__in=ids).prefetch_related("attendees")}
    clk, both = clock(v), shows_both(v)
    lays = layers(v)
    latest = {}
    for m in mails:
        if m.event_id and m.invite in ("request", "update"):
            latest[m.event_id] = m.id
    out = {}
    for m in mails:
        ev = evs.get(m.event_id)
        if not ev or m.invite not in ("request", "update", "cancel"):
            continue
        persona = m.persona if v.desk else ""
        lay = layer_of_box(v, _tid(v.team) if not v.desk else None, _tid(v.owner), persona, lays)
        row = None
        if lay:
            row = next((a for a in ev.attendees.all() if a.role != "organiser" and a.team_id == _tid(lay["team"])
                        and a.owner_id == _tid(lay["owner"]) and (not lay["persona"] or a.persona == lay["persona"])), None)
        day = shown(ev.starts_at, clk)[:10]
        out[m.id] = {
            "event": ev, "when": when_text(ev, clk), "sim": when_text(ev, "sim") if both else "",
            "mode": dict(MODES).get(ev.mode, ev.mode), "guests": _guests(ev), "kind": m.invite,
            "cancelled": ev.status == "cancelled", "response": row.response if row else "",
            "comment": row.comment if row else "",
            "answer": bool(row and lay["writable"] and ev.status == "confirmed" and latest.get(ev.id) == m.id),
            "layer": lay["key"] if lay else "",
            "cal_url": f"{v.base}/calendar/?view=day&date={day}"}
    return out


def thread_events(v, mails):
    """The events made from this conversation (Create event), on the viewer's calendars."""
    ids = [m.id for m in mails]
    lays = layers(v)
    out = []
    for ev in CalendarEvent.objects.filter(source_mail_id__in=ids).prefetch_related("attendees"):
        if any(_layer_of(a, lays) for a in ev.attendees.all()):
            day = shown(ev.starts_at, clock(v))[:10]
            out.append({"title": ev.title, "when": when_text(ev, clock(v)), "cancelled": ev.status == "cancelled",
                        "url": f"{v.base}/calendar/?view=day&date={day}"})
    return out


# --- writing ------------------------------------------------------------------------------------

def _writable(v, key):
    lay = next((lay for lay in layers(v) if lay["key"] == key), None)
    if not lay or not lay["writable"]:
        raise CalendarError("You can't add to that calendar.")
    return lay


def _own_event(v, eid):
    """(event, its organiser's layer) for an event the viewer may change: organised from one of
    the calendars they write to. Anything else is not theirs to see either: a 404."""
    lays = [lay for lay in layers(v) if lay["writable"]]
    row = _rows(lays).filter(event_id=eid, role="organiser").select_related("event").first()
    if not row:
        raise CalendarEvent.DoesNotExist
    return row.event, _layer_of(row, lays)


def _box_viewer(v, lay):
    """The Viewer of a layer's mailbox, to send and log from it (like views_inbox.sender_box)."""
    if v.desk:
        return v
    w = copy.copy(v)
    w.owner = lay["owner"]
    w.box = {"team": lay["team"], "owner": lay["owner"]}
    w.base = lay["base"] or v.base
    return w


def _next_day(text):
    try:
        return (datetime.strptime((text or "")[:10], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _clean(data, clk):
    title = (data.get("title") or "").strip()[:TITLE_MAX] or "(No title)"
    location = (data.get("location") or "").strip()[:LOCATION_MAX]
    description = (data.get("description") or "").strip()[:DESCRIPTION_MAX]
    mode = data.get("mode") if data.get("mode") in dict(MODES) else "in_person"
    all_day = data.get("all_day") in ("1", "true", "on", True)
    if all_day:
        start = parse_shown((data.get("start") or "")[:10], clk)
        end = parse_shown(_next_day(data.get("end") or data.get("start")), clk)
    else:
        start, end = parse_shown(data.get("start"), clk), parse_shown(data.get("end"), clk)
    if not start or not end:
        raise CalendarError("Give the date and time it starts and ends.")
    if end <= start:
        raise CalendarError("It has to end after it starts.")
    if end - start > timedelta(days=MAX_DAYS):
        raise CalendarError(f"An event can last {MAX_DAYS} days at most.")
    return {"title": title, "location": location, "description": description, "mode": mode, "all_day": all_day,
            "starts_at": start, "ends_at": end}


def _listify(data, name):
    if hasattr(data, "getlist"):
        return data.getlist(name)
    val = data.get(name) or []
    return [val] if isinstance(val, str) else list(val)


# --- the mails ----------------------------------------------------------------------------------

def _subject(ev, kind):
    when = mail_when(ev).replace(" (scenario time)", "")
    return {"request": f"Invitation: {ev.title} @ {when}", "update": f"Updated invitation: {ev.title} @ {when}",
            "cancel": f"Cancelled: {ev.title} @ {when}"}[kind]


def _body(ev, kind):
    org = ev.organiser_name or "The organiser"
    head = {"request": f"{org} invites you to this event.", "update": f"{org} has changed this event.",
            "cancel": f"{org} has cancelled this event."}[kind]
    lines = [head, "", ev.title, f"When: {mail_when(ev)}"]
    how = dict(MODES).get(ev.mode, "")
    lines.append(f"How: {how}" + (f" · Where: {ev.location}" if ev.location else ""))
    lines.append(f"Organiser: {org}")
    guests = [a.name for a in ev.attendees.all() if a.role != "organiser" and not a.hidden]
    if guests:
        lines.append("Guests: " + ", ".join(guests))
    if ev.description:
        lines += ["", ev.description]
    if kind != "cancel":
        lines += ["", "Answer Yes, Maybe or No on this invitation, or in SIMEX Calendar."]
    return "\n".join(lines)


def _mail_in(w, ev):
    """The latest mail of an event in mailbox w: what the next one continues."""
    return w.mails().filter(event=ev).exclude(folder="drafts").order_by("-id").first()


def _send(v, lay, entries, ev, kind, request, subject=None, body=None, from_key=None, parent=None):
    """One mail about ev (kind) from the mailbox of layer lay to entries (address-book entries of
    that mailbox), through that mailbox's own way of sending. Returns warnings; raises
    CalendarError when nothing could go."""
    from . import views_inbox as vi
    if not entries:
        return []
    w = _box_viewer(v, lay)
    subject = subject or _subject(ev, kind)
    body = body or _body(ev, kind)
    parent = parent if parent is not None else _mail_in(w, ev)
    user = request.user.get_username() if request else ""
    with _sending(ev, kind):
        if w.desk:
            try:
                _, errors = desk.send(w.person, user, from_key or lay["org_key"], entries, subject, body, parent=parent)
            except desk.DeskError as e:
                raise CalendarError(str(e))
            return errors
        try:
            if w.leader_box:
                sent = vi._send_as_leader(request, w, {}, entries, [], [], subject, body, [], parent, None)
            else:
                sent = vi._send(request, w, {}, entries, [], [], subject, body, [], parent, None)
        except vi.SendFailed as e:
            raise CalendarError(str(e))
    vi.track(w, "send", [sent.thread], kind="invite" if kind != "reply" else "invite_reply", to=len(entries), cc=0,
             bcc=0, files=0)
    return []


def save(v, data, eid=None, request=None):
    """Create (eid None) or change an event on a calendar the viewer writes to (data["layer"]),
    with its guests (data "guests": address-book keys, when data "guests_given"): new guests get
    an invitation, the others an update when something changed (asked to answer again if the
    time or place did), removed ones a cancellation. Returns (event, layer, warnings). Raises
    CalendarError on bad input, CalendarEvent.DoesNotExist for an event that isn't theirs."""
    from .views_inbox import track
    clk = clock(v)
    fields = _clean(data, clk)
    lay = _writable(v, data.get("layer") or "")
    given = bool(data.get("guests_given"))
    w = _box_viewer(v, lay)
    wanted = guest_entries(w, _listify(data, "guests"), lay) if given else None
    warnings = []
    with transaction.atomic():
        if eid:
            ev, old = _own_event(v, eid)
            if ev.status != "confirmed":
                raise CalendarError("It was cancelled: create a new event.")
            timing = (ev.starts_at, ev.ends_at, ev.location, ev.all_day) != \
                (fields["starts_at"], fields["ends_at"], fields["location"], fields["all_day"])
            changed = timing or any(getattr(ev, k) != val for k, val in fields.items())
            moved = old["key"] != lay["key"]
            guests = list(ev.attendees.exclude(role="organiser"))
            if moved and (guests or wanted):
                raise CalendarError("An event with guests stays on the calendar it was made on.")
            for k, val in fields.items():
                setattr(ev, k, val)
            if timing:
                ev.sequence += 1
            if moved:  # to another of their calendars
                ev.team, ev.owner, ev.organiser_key, ev.organiser_name = lay["team"], lay["owner"], lay["org_key"], lay["org_name"]
            ev.save()
            if moved:
                ev.attendees.filter(role="organiser").update(team=lay["team"], owner=lay["owner"], persona=lay["persona"],
                                                              key=lay["org_key"], name=lay["org_name"])
            if timing:
                ev.attendees.update(reminded_at=None)
                ev.attendees.exclude(role="organiser").update(response="needs_action", responded_at=None)
            action = "cal_update"
        else:
            source = None
            if data.get("source"):
                try:
                    source = v.mails().filter(pk=int(data.get("source"))).first()
                except (TypeError, ValueError):
                    source = None
            ev = CalendarEvent.objects.create(
                team=lay["team"], owner=lay["owner"], organiser_key=lay["org_key"], organiser_name=lay["org_name"],
                created_by=v.person, source_mail=source,
                is_test=bool(lay["team"] and lay["team"].is_test), **fields)
            CalendarAttendee.objects.create(event=ev, key=lay["org_key"], team=lay["team"], owner=lay["owner"],
                                            persona=lay["persona"], person=lay["owner"], name=lay["org_name"],
                                            role="organiser", response="accepted", responded_at=timezone.now(),
                                            responded_by=v.person)
            guests, changed = [], False
            action = "cal_create"
        added, removed, kept = [], [], guests
        if wanted is not None:
            have = {a.key: a for a in guests}
            want = {e["key"]: (e, box) for e, box in wanted}
            removed = [a for k, a in have.items() if k not in want]
            kept = [a for k, a in have.items() if k in want]
            boxes = {(a.team_id, a.owner_id, a.persona) for a in kept} | {(_tid(lay["team"]), _tid(lay["owner"]), lay["persona"])}
            for k, (e, box) in want.items():
                if k in have:
                    continue
                key3 = (_tid(box["team"]), _tid(box["owner"]), box["persona"])
                if key3 in boxes:
                    continue  # already on that calendar (e.g. a member and their team with one mailbox)
                boxes.add(key3)
                added.append((e, CalendarAttendee.objects.create(
                    event=ev, key=k, team=box["team"], owner=box["owner"], persona=box["persona"], person=box["person"],
                    name=e["name"][:200], addr=e["addr"][:320], role="required")))
        from .views_inbox import _resolve, address_book
        book = address_book(w)
        ent = lambda rows: _resolve([a.key for a in rows], book, w)
        if removed:
            ids = [a.id for a in removed]
            warnings += _send(v, lay, ent(removed), ev, "cancel", request)
            CalendarAttendee.objects.filter(pk__in=ids).delete()
        if added:
            warnings += _send(v, lay, [e for e, _ in added], ev, "request", request)
        if kept and changed:
            warnings += _send(v, lay, ent(kept), ev, "update", request)
    track(_box_viewer(v, lay), action, name=ev.title, event=ev.uid, start=shown(ev.starts_at, clk),
          guests=len(added) + len(kept))
    return ev, lay, warnings


def delete(v, eid, request=None):
    """Delete an event the viewer organised. With guests it is cancelled instead (a
    cancellation to each), so their calendars show it struck through. Returns warnings."""
    from .views_inbox import _resolve, address_book, track
    warnings = []
    with transaction.atomic():
        ev, lay = _own_event(v, eid)
        title, uid = ev.title, ev.uid
        guests = list(ev.attendees.exclude(role="organiser"))
        if guests:
            if ev.status == "confirmed":
                ev.status, ev.sequence = "cancelled", ev.sequence + 1
                ev.save(update_fields=["status", "sequence", "updated_at"])
                w = _box_viewer(v, lay)
                warnings = _send(v, lay, _resolve([a.key for a in guests], address_book(w), w), ev, "cancel", request)
            else:  # cancelled already: off the organiser's calendar
                ev.attendees.filter(role="organiser").update(hidden=True)
        else:
            ev.delete()
    track(_box_viewer(v, lay), "cal_cancel", name=title, event=uid)
    return warnings


def respond(v, eid, layer_key, answer, note="", request=None):
    """A guest's answer (accepted | tentative | declined, with a note) from a calendar the viewer
    writes to: their row, a MailAction and a short reply mail to the organiser in the same
    conversation, where their mailbox can write to the organiser (a team leader can't write to
    a persona: then the answer is only on the calendar). On the desk, as the persona invited.
    Returns (event, warnings)."""
    from .views_inbox import _resolve, address_book, track
    if answer not in ANSWERS:
        raise CalendarError("Answer Yes, Maybe or No.")
    lay = _writable(v, layer_key)
    row = (CalendarAttendee.objects.filter(_box_q(lay), event_id=eid).exclude(role="organiser")
           .select_related("event").first())
    if not row:
        raise CalendarEvent.DoesNotExist
    ev = row.event
    if ev.status != "confirmed":
        raise CalendarError("This event was cancelled.")
    note = (note or "").strip()[:NOTE_MAX]
    warnings = []
    with transaction.atomic():
        row.response, row.responded_at, row.comment = answer, timezone.now(), note
        row.responded_by = v.person
        row.save(update_fields=["response", "responded_at", "comment", "responded_by"])
        w = _box_viewer(v, lay)
        key = ev.organiser_key
        if key.startswith("l:") and not w.desk:  # a team's leader is reached as the team leader persona
            lp = rnd.leader_persona(w.team) if w.team else None
            key = f"p:{lp['local']}" if lp else key
        to = _resolve([key], address_book(w), w)
        if to:
            who = row.name or lay["org_name"]
            subject = f"{ANSWERS[answer]}: {ev.title}"
            verb = {"accepted": "has accepted", "tentative": "may attend", "declined": "has declined"}[answer]
            body = f"{who} {verb}: {ev.title}\nWhen: {mail_when(ev)}" + (f"\n\n{note}" if note else "")
            try:
                warnings += _send(v, lay, to, ev, "reply", request, subject=subject, body=body,
                                  from_key=f"p:{row.persona}" if w.desk else None)
            except CalendarError as e:
                warnings.append(f"Your answer is on the calendar; no mail could go to the organiser: {e}")
        else:
            warnings.append("Your answer is on the calendar (this mailbox can't write to the organiser).")
    track(w, "cal_rsvp", name=ev.title, event=ev.uid, answer=answer)
    if to and key.startswith("p:") and not w.desk:
        _answered_on_desk(ev, w.team)
    return ev, warnings


def _answered_on_desk(ev, team):
    """A team's answer to a persona's invitation needs no answer from the desk: the desk's
    conversation is marked so (no reply needed) up to it."""
    last = (Mail.objects.filter(team=None, owner=None, event=ev, desk_team=team, folder="inbox", invite="reply")
            .order_by("-id").first())
    if last:
        c = desk.conversation(last.thread, create=True)
        if c.done_upto < last.id:
            c.done_upto, c.done_by, c.done_at = last.id, "SIMEX Calendar (an answer to an invitation)", timezone.now()
            c.save(update_fields=["done_upto", "done_by", "done_at"])


def withdrawn(mails, team_ids):
    """A delivery withdrawn from these teams (delivery.withdraw): the invitations it made are
    taken back, their guests' rows in those teams go, and an event left without guests is
    cancelled."""
    evs = {m.event_id for m in mails if m.event_id and m.invite in ("request", "update")}
    for ev in CalendarEvent.objects.filter(pk__in=evs):
        ev.attendees.exclude(role="organiser").filter(team_id__in=team_ids).delete()
        if not ev.attendees.exclude(role="organiser").exists() and ev.status == "confirmed":
            ev.status, ev.sequence = "cancelled", ev.sequence + 1
            ev.save(update_fields=["status", "sequence", "updated_at"])


# --- clearing ---------------------------------------------------------------------------------

def clear(team=None, owners=None):
    """Remove calendars' events (ops/reset.py): every one (team None), a team's (organised in it,
    and its rows on other calendars) or, with owners, those members' own only."""
    if team is None:
        CalendarEvent.objects.all().delete()
        return
    if owners is not None:
        CalendarEvent.objects.filter(team=team, owner__in=owners).delete()
        CalendarAttendee.objects.filter(team=team, owner__in=owners).delete()
        return
    CalendarEvent.objects.filter(team=team).delete()
    CalendarAttendee.objects.filter(team=team).delete()

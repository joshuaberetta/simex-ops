"""SIMEX Calendar (plan: docs/CALENDAR_PLAN.md): meetings and appointments on the calendars of
the mailboxes. A calendar belongs to a mailbox, (team, owner) as Mail: the team's shared
calendar, a member's or a team leader's own, and the persona desk's, one layer per persona. An
event belongs to its organiser's mailbox and shows on each calendar with a CalendarAttendee row
for it (step 1: the organiser's own row only; guests and invitations are step 2).

Times are kept real, like everything in the DB. A member of a team sees and enters them in the
simulation time when the clock is on (ops/simclock.py: at / real_at), and never a real date;
team leaders, the persona desk and ExCon see the real time with the simulation time beside it.
So when ExCon moves the clock, an event keeps its real time and its simulation time moves.

Every query goes through the viewer's layers (layers(v)), which come from the Viewer's own
mailboxes: teams never cross. Participant-facing only: nothing control-side is read here.
"""

import copy
import zlib
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import addresses, conf, desk, simclock
from .models import CalendarAttendee, CalendarEvent

MODES = [("in_person", "In person"), ("phone", "Phone"), ("vtc", "VTC"), ("other", "Other")]
TITLE_MAX, LOCATION_MAX, DESCRIPTION_MAX = 200, 200, 4000
MAX_DAYS = 14  # the longest event
MAX_RANGE_DAYS = 62  # the longest range one request lists
FMT = "%Y-%m-%dT%H:%M"
# Layer colours: the team's calendar, the viewer's own, a team leader's (the persona desk's
# layers take each persona's colour, desk.colour).
TEAM_COLOUR, OWN_COLOUR, LEADER_COLOUR = ("#4a86e8", "#fff"), ("#16a766", "#fff"), ("#a479e2", "#fff")
MEMBER_COLOURS = [("#ffad47", "#462a00"), ("#f691b3", "#4d0f25"), ("#43d692", "#063d22"), ("#fad165", "#4a3b00"),
                  ("#fb4c2f", "#fff"), ("#8e8e8e", "#fff")]


class CalendarError(ValueError):
    pass


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


# --- layers: the calendars a viewer sees -------------------------------------------------------

def _layer(key, name, colour, team, owner, persona="", writable=False, on=True, org_key="", org_name="", kind=""):
    return {"key": key, "name": name, "colour": colour[0], "fg": colour[1], "team": team, "owner": owner,
            "persona": persona, "writable": writable, "on": on, "org_key": org_key, "org_name": org_name, "kind": kind}


def _member_colour(p):
    return MEMBER_COLOURS[zlib.crc32(str(p.id).encode()) % len(MEMBER_COLOURS)]


def layers(v):
    """The calendars the viewer sees, as layers: the persona desk one per persona; a member the
    team's and (personal mailboxes) their own; a team leader their own, the team's and their
    members' (read-only); ExCon (and the desk) viewing a team every mailbox of that team,
    read-only, the one viewed shown. Another team leader's mailbox (peer) has no calendar here."""
    from .views_inbox import leader_name, team_members
    out = []
    write = not v.readonly
    if v.desk:
        for s in desk.personas():
            out.append(_layer(f"p:{s.local_part}", s.display_name or s.local_part, desk.colour(s), None, None,
                              persona=s.local_part, writable=write, org_key=f"p:{s.local_part}",
                              org_name=s.display_name or s.local_part, kind="persona"))
        return out
    team = v.team
    personal = conf.flag("personal_mailboxes")
    team_layer = lambda **kw: _layer("t", f"{team.name} calendar", TEAM_COLOUR, team, None, org_key=f"t:{team.id}",
                                     org_name=team.name, kind="team", **kw)
    own_layer = lambda p, **kw: _layer(f"o{p.id}", "My calendar", OWN_COLOUR, team, p, org_key=f"l:{p.id}" if p.is_leader
                                       else f"m:{p.id}", org_name=leader_name(p, team) if p.is_leader
                                       else addresses.member_name(p, team), kind="own", **kw)
    member_layers = lambda on_pid=None: [
        _layer(f"o{p.id}", p.name, _member_colour(p), team, p, on=p.id == on_pid, org_key=f"m:{p.id}",
               org_name=addresses.member_name(p, team), kind="member")
        for p in team_members(team).filter(active=True)] if personal else []
    if v.leader:  # their own (theirs to change from /inbox/ only), the team's and their members', read-only
        own = v.leader
        out.append(own_layer(own, writable=write and v.owner == own and not v.leader_mode))
        out.append(team_layer(on=True))
        out += member_layers(v.owner.id if v.member_box else None)
        return out
    if v.person:  # a member: the team's and their own
        out.append(team_layer(writable=write))
        if personal:
            out.append(own_layer(v.person, writable=write))
        return out
    # ExCon or the persona desk viewing a team: every mailbox of it, read-only; the one viewed shown.
    viewed = v.owner.id if v.owner else None
    lead = team.leader()
    out.append(team_layer(on=viewed is None))
    if lead:
        out.append(_layer(f"o{lead.id}", f"{team.name} leader ({lead.name})", LEADER_COLOUR, team, lead,
                          on=viewed == lead.id, org_key=f"l:{lead.id}", org_name=leader_name(lead, team), kind="leader"))
    out += member_layers(viewed)
    return out


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
        if row.team_id == (lay["team"].id if lay["team"] else None) and \
                row.owner_id == (lay["owner"].id if lay["owner"] else None) and \
                (not lay["persona"] or row.persona == lay["persona"]):
            return lay
    return None


# --- reading ------------------------------------------------------------------------------------

def as_json(ev, layer, clk, both, row=None):
    """An event as SIMEX Calendar's page uses it: times on the viewer's clock only (a member
    with the clock on never gets a real time), the simulation times beside them for the others."""
    out = {"id": ev.id, "uid": ev.uid, "title": ev.title, "location": ev.location, "description": ev.description,
           "mode": ev.mode, "allDay": ev.all_day, "status": ev.status, "start": shown(ev.starts_at, clk),
           "end": shown(ev.ends_at, clk), "layer": layer["key"], "colour": layer["colour"], "fg": layer["fg"],
           "organiser": ev.organiser_name, "calendar": layer["name"],
           "editable": bool(layer["writable"] and (row is None or row.role == "organiser"))}
    if both:
        out["sim"] = {"start": shown(ev.starts_at, "sim"), "end": shown(ev.ends_at, "sim")}
    return out


def events(v, start_text, end_text, only=None):
    """The events of the viewer's layers (only: those keys) that overlap [start, end) on the
    viewer's clock, as as_json dicts."""
    clk, both = clock(v), shows_both(v)
    start, end = parse_shown(start_text, clk), parse_shown(end_text, clk)
    if not start or not end or end <= start:
        raise CalendarError("Give a range: from and to.")
    if end - start > timedelta(days=MAX_RANGE_DAYS):
        end = start + timedelta(days=MAX_RANGE_DAYS)
    lays = [lay for lay in layers(v) if only is None or lay["key"] in only]
    rows = (_rows(lays).filter(event__starts_at__lt=end, event__ends_at__gt=start, event__status="confirmed")
            .select_related("event").order_by("event__starts_at", "id"))
    out, seen = [], set()
    for row in rows:
        if row.event_id in seen:
            continue
        lay = _layer_of(row, lays)
        if lay:
            seen.add(row.event_id)
            out.append(as_json(row.event, lay, clk, both, row))
    return out


def page_data(v):
    """What the Calendar page's script starts from."""
    clk = clock(v)
    now, rate = now_shown(clk)
    lays = layers(v)
    return {"base": v.base, "clock": clk, "both": shows_both(v), "now": now, "rate": rate,
            "readonly": v.readonly or not any(lay["writable"] for lay in lays),
            "layers": [{k: lay[k] for k in ("key", "name", "colour", "fg", "writable", "on", "kind")} for lay in lays],
            "modes": MODES, "simNow": shown(timezone.now(), "sim") if shows_both(v) else "",
            "simRate": (simclock.rate() or 1.0) if shows_both(v) else None}


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
    """The Viewer of the mailbox a change is logged in (MailAction), like views_inbox.sender_box."""
    w = copy.copy(v)
    w.owner = lay["owner"]
    w.box = {"team": lay["team"], "owner": lay["owner"]}
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


def save(v, data, eid=None):
    """Create (eid None) or change an event on a calendar the viewer writes to (data["layer"]).
    Returns (event, layer). Raises CalendarError on bad input, CalendarEvent.DoesNotExist for an
    event that isn't theirs."""
    from .views_inbox import track
    clk = clock(v)
    fields = _clean(data, clk)
    lay = _writable(v, data.get("layer") or "")
    with transaction.atomic():
        if eid:
            ev, old = _own_event(v, eid)
            timing = (ev.starts_at, ev.ends_at, ev.location, ev.all_day) != \
                (fields["starts_at"], fields["ends_at"], fields["location"], fields["all_day"])
            for k, val in fields.items():
                setattr(ev, k, val)
            if timing:
                ev.sequence += 1
            moved = old["key"] != lay["key"]
            if moved:  # to another of their calendars
                ev.team, ev.owner, ev.organiser_key, ev.organiser_name = lay["team"], lay["owner"], lay["org_key"], lay["org_name"]
            ev.save()
            if moved:
                ev.attendees.filter(role="organiser").update(team=lay["team"], owner=lay["owner"], persona=lay["persona"],
                                                              key=lay["org_key"], name=lay["org_name"])
            if timing:
                ev.attendees.update(reminded_at=None)
            action = "cal_update"
        else:
            ev = CalendarEvent.objects.create(
                team=lay["team"], owner=lay["owner"], organiser_key=lay["org_key"], organiser_name=lay["org_name"],
                created_by=v.person,
                is_test=bool(lay["team"] and lay["team"].is_test), **fields)
            CalendarAttendee.objects.create(event=ev, key=lay["org_key"], team=lay["team"], owner=lay["owner"],
                                            persona=lay["persona"], person=lay["owner"], name=lay["org_name"],
                                            role="organiser", response="accepted", responded_at=timezone.now(),
                                            responded_by=v.person)
            action = "cal_create"
    track(_box_viewer(v, lay), action, name=ev.title, event=ev.uid, start=shown(ev.starts_at, "sim" if clk == "sim" else "real"))
    return ev, lay


def delete(v, eid):
    """Delete an event the viewer organised. With guests (step 2) it is cancelled instead, so
    their calendars show it struck through."""
    from .views_inbox import track
    with transaction.atomic():
        ev, lay = _own_event(v, eid)
        title, uid = ev.title, ev.uid
        if ev.attendees.exclude(role="organiser").exists():
            ev.status, ev.sequence = "cancelled", ev.sequence + 1
            ev.save(update_fields=["status", "sequence", "updated_at"])
        else:
            ev.delete()
    track(_box_viewer(v, lay), "cal_cancel", name=title, event=uid)


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

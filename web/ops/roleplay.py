"""Role-player coordination: the role-play schedule (RolePlay, one per MEL serial played face
to face, by phone or VTC, with its sittings, RolePlaySlot), imported from the MEL-MIL and a
role-player schedule sheet, run from ExCon's Role play tab (views_roleplay.py) and followed by
the role players on their phones (/inbox/rp/, views_rp.py), who are reminded by push (and
email) before each sitting.

Control-side by nature: scripts and expected outcomes live here and in the RolePlay models only,
read by ExCon and the role players assigned, never by the participants' or leaders' mail
views, the AI, the AI personas or the reports.

Times: a slot's start is a timezone-aware datetime. The sheets only name the day ("Monday"):
conf rp_days maps day names to dates, guessed from the MEL's email rows (Day + Send date) and
corrected by ExCon on the import plan."""

import datetime
import io
import json
import logging
import re
import uuid
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import conf
from .models import Person, PushSubscription, RolePlay, RolePlaySlot, Team, audit

log = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
RP_METHODS = re.compile(r"^\s*(f2f|face|phone|vtc|ms ?teams|teams|signal|whatsapp|call|meeting)", re.I)
# Fields an import sets; their last imported values are kept in RolePlay.imported.
FIELDS = ["serial", "seq", "day", "time_text", "duration", "title", "role", "players_text", "location", "method",
          "script", "outcomes", "documents", "optional"]
NONE_WORDS = {"", "none", "n/a", "na", "-", "tbc", "tbd"}
DEFAULT_LEAD = 15  # minutes before a slot its players are reminded


class RolePlayImportError(ValueError):
    pass


# --- small text helpers --------------------------------------------------------------------------

def _text(v):
    if v is None:
        return ""
    if isinstance(v, datetime.time):
        return v.strftime("%H:%M")
    if isinstance(v, datetime.datetime):
        return v.strftime("%H:%M") if v.year < 1950 else v.date().isoformat()
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _head(v):
    return " ".join(_text(v).lower().split())


def _none(s):
    return (s or "").strip().lower() in NONE_WORDS


def weekday(s):
    """"Monday", "Monday \n27 Sep" -> "monday", else None."""
    m = re.match(r"\s*([a-z]+)", (s or "").lower())
    return m.group(1) if m and m.group(1) in WEEKDAYS else None


# --- times -> slots --------------------------------------------------------------------------------

TIME = r"(?:[01]?\d|2[0-3]):[0-5]\d(?::\d\d)?|(?:[01]\d|2[0-3])[0-5]\d"
TEAMS = r"[A-Z](?:\s*(?:-|–|&|,|\+|/|and)\s*[A-Z])*"
TEAM_LINE = re.compile(rf"^\s*({TIME})\s*(?:[-–:]\s*)?({TEAMS})\s*$")
ANY_TIME = re.compile(rf"(?<![\d:])({TIME})(?![\d])")
ROLE_TEAMS = re.compile(rf"\bteams?\s+({TEAMS})\b")
ORDER = re.compile(r"(?:\(|^)\s*([A-Z](?:[\s,]+[A-Z])+)\s*(?:\)|$)", re.M)
COUNT = re.compile(r"(\d+)\s*x\s*(\d+)\s*min", re.I)
MINUTES = re.compile(r"(\d+)\s*min", re.I)


def _hm(token):
    token = token.strip()
    if ":" in token:
        h, m = token.split(":")[:2]
    else:
        h, m = token[:-2], token[-2:]
    return int(h), int(m)


def _letters(s):
    return [c.lower() for c in re.findall(r"[A-Z]", s or "")]


def minutes_of(duration):
    """Minutes of one sitting: "4 x 5 min" -> 5, "60 min max" -> 60, else None."""
    m = COUNT.search(duration or "")
    if m:
        return int(m.group(2))
    m = MINUTES.search(duration or "")
    return int(m.group(1)) if m else None


def parse_slots(time_text, duration="", role="", team_order=()):
    """The sittings a schedule row describes: [(hour, minute, minutes, [team codes])], team
    codes lower case ([] = every team together). team_order: the teams' codes in order, for
    "4 x 5 min" without an order of its own."""
    minutes = minutes_of(duration)
    lines = [ln.strip() for ln in (time_text or "").splitlines() if ln.strip()]
    per_line = [TEAM_LINE.match(ln) for ln in lines]
    found = [m for m in per_line if m]
    count = COUNT.search(duration or "")
    if len(found) == 1 and count and int(count.group(1)) > 1 and len(_letters(found[0].group(2))) == 1:
        # "15:35 - A" for "4 x 5 min": four sittings, from team A on in the teams' order.
        first, order = _letters(found[0].group(2))[0], list(team_order)
        if first in order and int(count.group(1)) == len(order):
            order = order[order.index(first):] + order[:order.index(first)]
            return _series(*_hm(found[0].group(1)), int(count.group(1)), int(count.group(2)), order)
    if found:
        return [(*_hm(m.group(1)), minutes, _letters(m.group(2))) for m in found]
    times = [_hm(t) for t in ANY_TIME.findall(time_text or "")]
    if not times:
        return []
    role_teams = ROLE_TEAMS.search(role or "")
    role_teams = _letters(role_teams.group(1)) if role_teams else []
    if len(times) > 1:
        if len(role_teams) == len(times):
            return [(h, m, minutes, [t]) for (h, m), t in zip(times, role_teams)]
        return [(h, m, minutes, list(role_teams)) for h, m in times]
    h, m = times[0]
    if count and int(count.group(1)) > 1:
        n, step = int(count.group(1)), int(count.group(2))
        order = ORDER.search(duration or "")
        order = _letters(order.group(1)) if order else role_teams or list(team_order)
        return _series(h, m, n, step, order if len(order) == n else [])
    return [(h, m, minutes, role_teams)]


def _series(h, m, n, step, order):
    """n sittings of step minutes from h:m, one team each in order (if given)."""
    start = h * 60 + m
    return [((start + i * step) // 60 % 24, (start + i * step) % 60, step, [order[i]] if order else [])
            for i in range(n)]


def team_order():
    return [t.code.lower() for t in Team.objects.filter(active=True, is_test=False).order_by("sort", "name")]


def days_map():
    """conf rp_days: {"monday": "2026-11-23", ...}."""
    try:
        d = json.loads(conf.get("rp_days") or "{}")
        return {k.lower(): v for k, v in d.items() if weekday(k)} if isinstance(d, dict) else {}
    except ValueError:
        return {}


def at(day_name, h, m, days=None):
    """A slot's start (aware) for day "Monday" at h:m, or None when the day has no date."""
    date = (days if days is not None else days_map()).get(weekday(day_name) or "")
    if not date:
        return None
    try:
        d = datetime.date.fromisoformat(date)
    except ValueError:
        return None
    return datetime.datetime.combine(d, datetime.time(h, m), tzinfo=ZoneInfo(settings.TIME_ZONE))


# --- reading the sheets ----------------------------------------------------------------------------

MEL_HEADS = {"serial": ("inject #", "serial", "ser"), "time": ("time", "dtg (all times gmt+2) i.e. local"),
             "duration": ("duration of event (min)", "duration"), "title": ("description",),
             "method": ("inject method", "method"), "script": ("content",),
             "outcomes": ("expected outcomes",), "documents": ("supporting documents", "documents to be printed"),
             "day": ("day",), "location": ("location",)}
SCHEDULE_HEADS = {"serial": ("serial", "ser", "inject #"), "time": ("time", "dtg (all times gmt+2) i.e. local"),
                  "duration": ("duration", "duration of event (min)"), "role": ("role", "role players/character"),
                  "players": ("role player", "role player name", "role players"), "title": ("event", "description"),
                  "location": ("location",), "day": ("day",), "documents": ("documents", "documents to be printed")}


def _columns(row, heads):
    cells = [_head(c) for c in row]
    found = {}
    for field, names in heads.items():
        for n in names:
            for i, c in enumerate(cells):
                if field not in found and i not in found.values() and (c == n or c.startswith(n + " (")
                                                                       or c.startswith(n + " (")):
                    found[field] = i
    return found


def read_workbook(data):
    """Every sheet that reads as a role-play source: [{"sheet", "kind" (mel | schedule), "rows"}],
    each row a dict of the fields above plus "line". Raises RolePlayImportError."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        raise RolePlayImportError(f"Not a readable .xlsx workbook ({e}).") from e
    out = []
    for ws in wb.worksheets:
        grid = list(ws.iter_rows(values_only=True))
        for i, row in enumerate(grid[:15]):
            sched = _columns(row, SCHEDULE_HEADS)
            mel = _columns(row, MEL_HEADS)
            kind = ("schedule" if "players" in sched and "serial" in sched else
                    "mel" if {"serial", "method", "script"} <= set(mel) else None)
            if kind:
                cols = sched if kind == "schedule" else mel
                rows = _rows(grid[i + 1:], cols, kind, i + 2)
                if rows:
                    out.append({"sheet": ws.title, "kind": kind, "rows": rows})
                break
    if not out:
        raise RolePlayImportError("No role-play rows found: expected a MEL-MIL sheet (Serial, Inject method, "
                                  "Content) or a role-player schedule (Serial, Time, Role, Role player).")
    return out


def _rows(grid, cols, kind, first_line):
    rows, day = [], ""
    for n, row in enumerate(grid, first_line):
        def cell(f):
            j = cols.get(f)
            return _text(row[j]) if j is not None and j < len(row) else ""
        # The day: its own column, or a (merged) cell holding just a weekday name, carried forward.
        for c in row:
            if isinstance(c, str) and weekday(c) and len(c.strip()) < 30 and weekday(c) == c.strip().split()[0].lower():
                day = c.strip().split()[0].capitalize()
                break
        serial = re.match(r"[A-Za-z]?\d[\w.-]*", cell("serial"))  # "1-38a New" -> 1-38a
        if not serial:
            continue
        serial = serial.group()
        if kind == "mel" and not RP_METHODS.match(cell("method")):
            continue
        r = {"line": n, "serial": serial[:40], "time": cell("time"), "duration": cell("duration"),
             "title": cell("title"), "day": (weekday(cell("day")) or "").capitalize() or day,
             "location": cell("location"), "documents": cell("documents")}
        if kind == "mel":
            r.update(method=cell("method"), script=cell("script"), outcomes=cell("outcomes"))
        else:
            r.update(role=cell("role"), players=cell("players"))
        rows.append(r)
    return rows


def default_sheets(sources):
    """The sheets an import reads unless ExCon says otherwise: the MEL-MIL, and the schedule
    sheets whose serials are mostly the MEL's (a workbook keeps old schedules from past
    exercises); without a MEL-MIL, every schedule sheet."""
    mel = {r["serial"] for s in sources if s["kind"] == "mel" for r in s["rows"]}
    out = set()
    for s in sources:
        serials = {r["serial"] for r in s["rows"]}
        if s["kind"] == "mel" or not mel or len(serials & mel) >= len(serials) / 2:
            out.add(s["sheet"])
    return out


def guess_days(data):
    """{"monday": "2026-11-23"} from the MEL-MIL rows with both a Day and a Send date."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        return {}
    votes = defaultdict(Counter)
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        cols = None
        for i, row in enumerate(rows):
            if cols is None:
                heads = [_head(c) for c in row]
                if "day" in heads and "send date" in heads:
                    cols = heads.index("day"), heads.index("send date")
                if i > 15 and cols is None:
                    break
                continue
            d, s = (row[cols[0]] if cols[0] < len(row) else None), (row[cols[1]] if cols[1] < len(row) else None)
            name = weekday(_text(d))
            if name and isinstance(s, (datetime.date, datetime.datetime)):
                date = s.date() if isinstance(s, datetime.datetime) else s
                if WEEKDAYS[date.weekday()] == name:
                    votes[name][date.isoformat()] += 1
    return {k: c.most_common(1)[0][0] for k, c in votes.items()}


def documents_lines(s):
    lines = [ln.strip() for ln in (s or "").splitlines()]
    return "\n".join(ln for ln in lines if not _none(ln))


def records(sources, sheets=None):
    """Import records from the sheets read (only those named in sheets, if given): one per
    event, keyed by serial (-b, -c... for more rows of one serial in the schedule)."""
    chosen = [s for s in sources if sheets is None or s["sheet"] in sheets]
    mel = {}
    order = []
    for s in chosen:
        if s["kind"] != "mel":
            continue
        for r in s["rows"]:
            if r["serial"] not in mel:
                mel[r["serial"]] = r
                order.append(r["serial"])
    sched = defaultdict(list)
    for s in chosen:
        if s["kind"] != "schedule":
            continue
        for r in s["rows"]:
            if r["serial"] not in sched and r["serial"] not in mel:
                order.append(r["serial"])
            sched[r["serial"]].append(r)
    out = []
    for seq, serial in enumerate(order):
        m = mel.get(serial, {})
        for n, s in enumerate(sched.get(serial) or [{}]):
            title = s.get("title") or m.get("title") or ""
            out.append({
                "key": (serial if n == 0 else f"{serial}-{chr(ord('a') + n)}")[:40],
                "serial": serial, "seq": seq * 10 + n,
                "day": (s.get("day") or m.get("day") or "")[:40],
                "time_text": (s.get("time") or m.get("time") or "")[:200],
                "duration": (s.get("duration") or m.get("duration") or "")[:120],
                "title": title, "role": (s.get("role") or "")[:200],
                "players_text": (s.get("players") or "")[:300],
                "location": (s.get("location") or m.get("location") or "")[:200],
                "method": (m.get("method") or "")[:60],
                "script": m.get("script") or "", "outcomes": m.get("outcomes") or "",
                "documents": documents_lines(s.get("documents") or m.get("documents")),
                "optional": title.strip().lower().startswith("optional"),
            })
    return out


# --- role players ----------------------------------------------------------------------------------

SPLIT = re.compile(r"\s*(?:\+|,|&|/|;|\n|\s[-–]\s|\band\b)\s*")


def split_players(text):
    """"Kenyan RP 1 + Pierre" -> ["Kenyan RP 1", "Pierre"]; "n/a ..." and blanks -> []."""
    t = (text or "").strip()
    if _none(t) or t.lower().startswith(("n/a", "na ", "tbc", "tbd")):
        return []
    return [p for p in (" ".join(x.split()) for x in SPLIT.split(t)) if p and not _none(p)]


def candidates():
    """Who may be assigned a role play: role players, team leaders and the persona desk (never
    participants)."""
    return list(Person.objects.filter(role__in=("rp", "tl", "desk"), active=True).select_related("team"))


def match_player(name, people):
    """The Person a name in the sheet is (same name, else the only one with that first name), or None."""
    from .roster import key
    k = key(name)
    same = [p for p in people if key(p.name) == k]
    if len(same) == 1:
        return same[0]
    first = [p for p in people if key(p.name).split()[:1] == k.split()[:1]] if len(k.split()) == 1 else []
    return first[0] if len(first) == 1 else None


def resolve_players(text, people):
    """[(name, Person or None)] for a sheet's Role player cell."""
    return [(n, match_player(n, people)) for n in split_players(text)]


# --- plan and apply --------------------------------------------------------------------------------

def _value(play, f):
    return getattr(play, f)


def plan(recs):
    """Per record: new / unchanged / update / conflict (an ExCon edit the file would overwrite),
    and the events no longer in the file."""
    existing = {p.key: p for p in RolePlay.objects.all()}
    people = candidates()
    rows, new_names = [], set()
    for r in recs:
        play = existing.pop(r["key"], None)
        players = resolve_players(r["players_text"], people)
        new_names.update(n for n, p in players if not p)
        if not play:
            rows.append({"rec": r, "action": "new", "changes": [], "players": players})
            continue
        snap = play.imported or {}
        changes = []
        for f in FIELDS:
            new, cur = r.get(f), _value(play, f)
            if f == "seq" or (f in snap and new == snap.get(f)) or new == cur:
                continue
            changes.append({"field": f, "current": cur, "new": new, "local_edit": f in snap and cur != snap.get(f)})
        action = "unchanged" if not changes else "conflict" if any(c["local_edit"] for c in changes) else "update"
        rows.append({"rec": r, "play": play, "action": action, "changes": changes, "players": players})
    missing = list(existing.values())
    return rows, missing, sorted(new_names)


def _started(play):
    return play.slots.exclude(status="pending").exists()


def build_slots(play, days=None, order=None):
    """(Re)create a role play's slots from its time text, duration and role. Never when one has
    started (in position, done or skipped): those stay as they are."""
    if _started(play):
        return False
    order = team_order() if order is None else order
    teams = {t.code.lower(): t for t in Team.objects.all()}
    play.slots.all().delete()
    for h, m, minutes, codes in parse_slots(play.time_text, play.duration, play.role, order):
        start = at(play.day, h, m, days)
        s = RolePlaySlot.objects.create(play=play, start=start, minutes=minutes, imported_start=start)
        s.teams.set([teams[c] for c in codes if c in teams])
    return True


@transaction.atomic
def apply(recs, overwrite_local=False, delete_missing=False, days=None, user=""):
    if days is not None:
        conf.put("rp_days", json.dumps(days))
    days = days_map()
    order = team_order()
    existing = {p.key: p for p in RolePlay.objects.all()}
    people = candidates()
    counts = {"new": 0, "updated": 0, "kept_local": 0, "players_added": 0, "deleted": 0}
    for r in recs:
        play = existing.pop(r["key"], None)
        is_new = play is None
        if is_new:
            play = RolePlay(key=r["key"])
        snap = play.imported or {}
        changed = set()
        for f in FIELDS:
            new = r.get(f)
            if not is_new and f in snap and new == snap.get(f):
                continue  # not changed in the file since the last import: ExCon's edit (if any) stays
            if not is_new and f in snap and getattr(play, f) != snap.get(f) and not overwrite_local:
                counts["kept_local"] += 1
                continue
            if getattr(play, f) != new:
                setattr(play, f, new)
                changed.add(f)
        play.imported = {f: r.get(f) for f in FIELDS}
        play.save()
        if is_new or "players_text" in changed:
            assigned = []
            for name, p in resolve_players(play.players_text, people):
                if not p:
                    p = Person.objects.create(name=name[:200], role="rp")
                    people.append(p)
                    counts["players_added"] += 1
                assigned.append(p)
            play.players.set(assigned)
        if is_new or changed & {"day", "time_text", "duration", "role"} or not play.slots.exists():
            build_slots(play, days, order)
        counts["new" if is_new else "updated"] += bool(is_new or changed)
    if delete_missing:
        for play in existing.values():
            if not _started(play):
                play.delete()
                counts["deleted"] += 1
    audit("roleplay_import", json.dumps(counts), user)
    return counts


def redate(days, user=""):
    """New dates for the days (conf rp_days): every pending slot moves to its day's new date."""
    conf.put("rp_days", json.dumps(days))
    n = 0
    tz = ZoneInfo(settings.TIME_ZONE)
    for s in RolePlaySlot.objects.filter(status="pending").select_related("play"):
        if not s.start:
            continue
        local = s.start.astimezone(tz)
        new = at(s.play.day, local.hour, local.minute, days)
        if new and new != s.start:
            s.start = new
            s.save(update_fields=["start"])
            n += 1
    audit("roleplay_days", json.dumps(days), user)
    return n


# --- running the schedule --------------------------------------------------------------------------

def slots_for(person):
    """A person's slots: every slot of the role plays they play; a team leader's only where their
    team is (or all teams are) in it."""
    qs = (RolePlaySlot.objects.filter(play__players=person).select_related("play")
          .prefetch_related("teams", "play__players").order_by("start", "id"))
    if person.role == "tl":
        return [s for s in qs if not s.teams.all() or any(t.id == person.team_id for t in s.teams.all())]
    return list(qs)


def players_of(slot):
    """Who plays a slot (as slots_for)."""
    teams = {t.id for t in slot.teams.all()}
    return [p for p in slot.play.players.all() if p.active and (p.role != "tl" or not teams or p.team_id in teams)]


def has_role_plays(person):
    return bool(person) and RolePlay.objects.filter(players=person).exists()


def set_status(slot, status, by=""):
    if status not in ("pending", "ready", "done", "skipped"):
        raise ValueError(status)
    RolePlaySlot.objects.filter(pk=slot.pk).update(status=status, status_at=timezone.now(), status_by=by[:200])
    slot.status = status
    bump()


def slip(after, minutes, day=None, only=None, user=""):
    """Move every pending slot starting at or after `after` (aware) on that day by `minutes`
    (negative: earlier). only: a RolePlay to limit it to. Returns the slots moved; their players
    are told (tick sends the change)."""
    tz = ZoneInfo(settings.TIME_ZONE)
    qs = RolePlaySlot.objects.filter(status="pending", start__gte=after)
    if day is None:
        local = after.astimezone(tz)
        end = datetime.datetime.combine(local.date() + datetime.timedelta(days=1), datetime.time(0), tzinfo=tz)
        qs = qs.filter(start__lt=end)
    if only is not None:
        qs = qs.filter(play=only)
    moved = []
    now = timezone.now()
    for s in qs.select_related("play"):
        s.was_start = s.start
        s.start = s.start + datetime.timedelta(minutes=minutes)
        s.changed_at, s.reminded = now, ""
        s.save(update_fields=["start", "was_start", "changed_at", "reminded"])
        moved.append(s)
    if moved:
        audit("roleplay_slip", f"{len(moved)} slot(s) {minutes:+d} min from {after.astimezone(tz):%a %H:%M}", user)
        bump()
    return moved


def bump():
    """Something on the board changed: open boards and phones reload (poll)."""
    conf.put("rp_v", uuid.uuid4().hex[:10])


def version():
    return conf.get("rp_v") or "0"


# --- checks ------------------------------------------------------------------------------------

def checks(slots=None):
    """What looks wrong on the schedule: [{"kind", "text", "play"}]."""
    if slots is None:
        slots = list(RolePlaySlot.objects.select_related("play").prefetch_related("teams", "play__players"))
    out = []
    seen_plays = set()
    for s in slots:
        p = s.play
        if p.id in seen_plays:
            continue
        seen_plays.add(p.id)
        if not p.players.all() and not p.optional:
            out.append({"kind": "unassigned", "play": p, "text": f"{p.serial} {p.title[:60]}: no role player"})
        if not p.location and p.method.lower().startswith("f2f"):
            out.append({"kind": "location", "play": p, "text": f"{p.serial} {p.title[:60]}: no location"})
    for s in slots:
        if not s.start:
            out.append({"kind": "time", "play": s.play, "text": f"{s.play.serial} {s.play.title[:60]}: no time "
                                                                f"(\"{s.play.time_text}\", {s.play.day or 'no day'})"})
    timed = [s for s in slots if s.start and s.status != "skipped"]
    by_person = defaultdict(list)
    by_team = defaultdict(list)
    for s in timed:
        for person in players_of(s):
            by_person[person].append(s)
        for t in s.teams.all():
            by_team[t].append(s)
    for person, ss in by_person.items():
        for a, b in _overlaps(ss):
            out.append({"kind": "double", "play": b.play, "text": f"{person.name} is in {a.play.serial} "
                        f"({_hhmm(a)}) and {b.play.serial} ({_hhmm(b)}) at once"})
    for team, ss in by_team.items():
        for a, b in _overlaps(ss):
            if a.play_id != b.play_id:
                out.append({"kind": "team", "play": b.play, "text": f"{team.name} is in {a.play.serial} "
                            f"({_hhmm(a)}) and {b.play.serial} ({_hhmm(b)}) at once"})
    return out


def _hhmm(s):
    return timezone.localtime(s.start).strftime("%a %H:%M") if s.start else "?"


def _overlaps(slots):
    ss = sorted(slots, key=lambda s: s.start)
    out = []
    for i, a in enumerate(ss):
        a_end = a.start + datetime.timedelta(minutes=a.minutes or 1)
        for b in ss[i + 1:]:
            if b.start >= a_end:
                break
            if a.id != b.id:
                out.append((a, b))
    return out


# --- reminders (scheduler) ---------------------------------------------------------------------------

def lead():
    try:
        return max(0, int(conf.get("rp_lead") or DEFAULT_LEAD))
    except ValueError:
        return DEFAULT_LEAD


def describe(slot):
    """One line for a slot: "14:40 · 1-25 Immigration · Team A · Lagash airport"."""
    teams = ", ".join(t.name for t in slot.teams.all()) or "all teams"
    bits = [timezone.localtime(slot.start).strftime("%H:%M") if slot.start else "no time",
            f"{slot.play.serial} {slot.play.title.splitlines()[0][:60] if slot.play.title else ''}".strip(), teams]
    if slot.play.location:
        bits.append(slot.play.location)
    return " · ".join(bits)


def tick(now=None):
    """Remind each pending slot's players lead() minutes before it and at its start, tell them
    of changed slots, and tell ExCon of slots that started with nobody in position. Returns how
    many reminders went out."""
    now = now or timezone.now()
    sent = 0
    soon = now + datetime.timedelta(minutes=lead())
    qs = (RolePlaySlot.objects.filter(status__in=("pending", "ready"), start__isnull=False,
                                      start__gte=now - datetime.timedelta(minutes=60), start__lte=soon)
          .select_related("play").prefetch_related("teams", "play__players"))
    for s in qs:
        stage = "start" if s.start <= now else "soon"
        if s.reminded == "start" or (stage == "soon" and s.reminded == "soon"):
            pass
        elif stage == "start" and (s.status == "ready" or s.start < now - datetime.timedelta(minutes=10)):
            RolePlaySlot.objects.filter(pk=s.pk).update(reminded="start")  # in position, or long past: no alert
        elif RolePlaySlot.objects.filter(pk=s.pk, reminded=s.reminded).update(reminded=stage):  # claimed
            when = "Now" if stage == "start" else f"In {max(1, round((s.start - now).total_seconds() / 60))} min"
            sent += remind(s, f"{when}: {s.play.serial} {s.play.role or s.play.title[:40]}".strip(), describe(s),
                           f"rp-{s.id}-{stage}")
        if s.status == "pending" and s.start <= now - datetime.timedelta(minutes=2):
            from . import notices
            who = ", ".join(p.name for p in players_of(s)) or "no role player assigned"
            notices.add(f"rp-late:{s.id}", "system", f"Role play {s.play.serial}: nobody in position",
                        describe(s), who, f"/roleplay/{s.play_id}/")
    changed = (RolePlaySlot.objects.filter(changed_at__isnull=False, status="pending")
               .select_related("play").prefetch_related("teams", "play__players"))
    for s in changed:
        if RolePlaySlot.objects.filter(pk=s.pk, changed_at=s.changed_at).update(changed_at=None):
            was = f" (was {timezone.localtime(s.was_start):%H:%M})" if s.was_start else ""
            sent += remind(s, f"Changed: {s.play.serial} now "
                              f"{timezone.localtime(s.start):%H:%M}{was}" if s.start else f"Changed: {s.play.serial}",
                           describe(s), f"rp-{s.id}-chg-{int(s.start.timestamp()) if s.start else 0}")
    return sent


def remind(slot, title, body, key, people=None):
    """Push title/body to the slot's players' devices; email those without one (conf rp_email)."""
    from . import push
    people = players_of(slot) if people is None else people
    if not people:
        return 0
    data = {"title": title[:120], "body": body[:200], "url": f"/inbox/rp/#slot-{slot.id}", "tag": f"rp-{slot.id}"}
    push.notify_people([p.id for p in people], data)
    if conf.flag("rp_email"):
        with_push = set(PushSubscription.objects.filter(person__in=people).values_list("person_id", flat=True))
        for p in people:
            if p.id not in with_push:
                email_reminder(p, title, body, f"{key}-{p.id}")
    return len(people)


def email_reminder(person, title, body, key):
    """A reminder by email (a Message of kind rp, written before the send)."""
    from . import logins, mailer, render
    from .models import Message
    addr = (person.personal_email or person.email or "").strip()
    if not addr or not render.EMAIL.match(addr) or render.PLACEHOLDER_DOMAIN.search(addr):
        return False
    link = (settings.MAIL_URL or settings.PUBLIC_URL).rstrip("/") + "/inbox/rp/"
    text = f"{body}\n\nYour role-play schedule: {link}\n"
    try:
        m = Message.objects.create(kind="rp", channel="email", target=f"{person.name} · role play", person=person,
                                   from_addr=logins.from_addr(), to=[addr], subject=title, text=text, idem_key=key[:200])
    except IntegrityError:
        return False  # already sent
    try:
        rid = mailer.get().send({"from": m.from_addr, "to": m.to, "subject": m.subject, "text": text}, m.idem_key)
    except Exception as e:
        Message.objects.filter(pk=m.pk).update(status="failed", error=str(e)[:500])
        return False
    Message.objects.filter(pk=m.pk).update(status="sent", sent_at=timezone.now(), resend_id=rid)
    return True


def reset():
    """Start afresh (ops/reset.py): every slot pending again, no reminders sent."""
    RolePlaySlot.objects.update(status="pending", status_at=None, status_by="", reminded="", changed_at=None)
    from .models import RolePlaySeen
    RolePlaySeen.objects.all().delete()
    bump()

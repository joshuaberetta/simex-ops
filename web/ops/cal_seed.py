"""Find meetings in the mail (ExCon only, Team inboxes → Meetings in the mail; plan step 3 in
docs/CALENDAR_PLAN.md). SIMEX Calendar arrives part way through the exercise: the meetings the
teams have already proposed or agreed are in the mail. SIMEX AI reads the participant-facing
mail (the team inboxes, members' and leaders' own mailboxes, the persona desk's), thread by
thread in capped chunks, in a thread of the web process (like InjectDraft; AiCall purpose
cal_seed), and lists candidate meetings (CalendarSeedItem) for ExCon to check, edit, tick and
apply. Applying makes the events and their attendee rows quietly: no invitation mail, no push.

It never reads control-side material: no role plays, no planned injects, no Library notes, no
ExCon notes; only Mail. An item may only put an event on the calendars of mailboxes its
evidence mail belongs to or is addressed to (CalendarSeedItem.allowed): teams never cross.
"""

import logging
import threading
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import addresses, ai, calendar, notices, render as rnd, simclock
from .models import (CalendarAttendee, CalendarEvent, CalendarSeed, CalendarSeedItem, Mail, Person, Sender, Team)

log = logging.getLogger(__name__)

MAX_THREADS = 400  # read per run
MAX_MAILS = 12  # the latest of each thread
MAIL_CHARS = 1200
THREAD_CHARS = 6000
CHUNK_CHARS = 30_000  # one call
STATUSES = ["proposed", "confirmed", "declined", "cancelled"]
RESPONSES = ["accepted", "tentative", "declined", "needs_action"]
DUPLICATE_MINUTES = 30
FMT = "%Y-%m-%dT%H:%M"

RULES = """You read email conversations from the exercise (between teams, their members, their team \
leaders and the scenario's personas) and list the meetings in them: meetings, calls and video \
conferences that someone proposed, agreed, declined or cancelled, with a date and time that can be \
worked out. Ignore deadlines, reports due, deliveries and anything that isn't people meeting or \
talking at a set time. Several messages about the same meeting are one meeting: use the latest \
state (a time moved, then confirmed, is one confirmed meeting at the new time).

Every message has its real date and, when the exercise ran a simulation clock, its scenario date. \
People write in scenario time: resolve "tomorrow at 10", "Thursday 14:00", "in two hours" against \
the scenario date of the message it is written in (else its real date), and give start and end as \
YYYY-MM-DDTHH:MM with time_base "scenario" (or "real" if the message has no scenario date), and \
anchor_mail the id of that message. No end given: one hour. No time at all: leave the meeting out.

organiser and guests are keys from the KEYS list (t: a team, m: a member, l: a team leader, p: a \
persona), only those taking part in the conversation; never invent one. status: proposed (asked, \
not yet agreed), confirmed, declined, cancelled. Each guest's answer as the mail shows it: \
accepted, tentative, declined or needs_action (no answer yet). confidence 0-100. sources: the ids of \
the messages it rests on, each with the line that shows it, quoted exactly and briefly. note: one \
line for ExCon, e.g. what is unsure."""

SCHEMA = {
    "type": "object",
    "properties": {
        "meetings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "thread": {"type": "string", "description": "the T id of its conversation"},
                "title": {"type": "string"},
                "start": {"type": "string"}, "end": {"type": "string"},
                "time_base": {"type": "string", "enum": ["scenario", "real"]},
                "anchor_mail": {"type": "integer"},
                "all_day": {"type": "boolean"},
                "location": {"type": "string"},
                "mode": {"type": "string", "enum": [m for m, _ in calendar.MODES]},
                "organiser": {"type": "string"},
                "guests": {"type": "array", "items": {"type": "object", "properties": {
                    "key": {"type": "string"}, "answer": {"type": "string", "enum": RESPONSES}},
                    "required": ["key", "answer"], "additionalProperties": False}},
                "status": {"type": "string", "enum": STATUSES},
                "confidence": {"type": "integer"},
                "sources": {"type": "array", "items": {"type": "object", "properties": {
                    "mail": {"type": "integer"}, "quote": {"type": "string"}},
                    "required": ["mail", "quote"], "additionalProperties": False}},
                "note": {"type": "string"},
            },
            "required": ["thread", "title", "start", "end", "time_base", "anchor_mail", "all_day", "location", "mode",
                         "organiser", "guests", "status", "confidence", "sources", "note"],
            "additionalProperties": False,
        }},
    },
    "required": ["meetings"],
    "additionalProperties": False,
}

RUN_INLINE = None  # tests: None follows ai.RUN_INLINE


def system():
    return ai.instructions("exercise") + "\n\n" + RULES


def available():
    return ai.enabled()


# --- whose mail: the keys of a mailbox and of a message ---------------------------------------------

def box_key(m):
    """The address-book key of the mailbox a Mail is in (the desk's: the persona on its side)."""
    if m.team_id is None:
        return f"p:{m.persona}" if m.persona else ""
    if m.owner_id is None:
        return f"t:{m.team_id}"
    return f"l:{m.owner_id}" if m.owner and m.owner.is_leader else f"m:{m.owner_id}"


def mail_keys(m, memo):
    """(from key, [to keys]) of a Mail."""
    from .views_inbox import _key_for_sender
    if m.team_id is None:  # the persona desk's copy
        team = f"t:{m.desk_team_id}" if m.desk_team_id else ""
        side = f"m:{m.author_id}" if m.folder == "inbox" and m.author_id else team
        return (side, [box_key(m)]) if m.folder == "inbox" else (box_key(m), [team])
    if m.folder == "sent":
        r = m.recipients or {}
        to = list(r.get("to", [])) + list(r.get("cc", []))
        if not to and m.persona:
            to = [f"p:{m.persona}"]
        return box_key(m), to
    return _key_for_sender(m, memo), [box_key(m)]


def key_name(key, memo):
    if key in memo:
        return memo[key]
    from .views_inbox import leader_name
    kind, _, val = key.partition(":")
    name = ""
    if kind == "t" and val.isdigit():
        t = Team.objects.filter(pk=int(val)).first()
        name = t.name if t else ""
    elif kind in ("m", "l") and val.isdigit():
        p = Person.objects.filter(pk=int(val)).select_related("team").first()
        if p and p.team:
            name = leader_name(p) if p.is_leader else addresses.member_name(p)
    elif kind == "p":
        s, ctx = rnd.sender_by_local(val)
        name = (rnd.fill(s.display_name, ctx) or val) if s else ""
    memo[key] = name
    return name


def key_exists(key):
    kind, _, val = key.partition(":")
    if kind == "t" and val.isdigit():
        return Team.objects.filter(pk=int(val), active=True).exists()
    if kind == "m" and val.isdigit():
        return Person.objects.participants().filter(pk=int(val), active=True).exists()
    if kind == "l" and val.isdigit():
        return Person.objects.filter(pk=int(val), role="tl", active=True, team__isnull=False).exists()
    if kind == "p" and rnd.LOCAL.match(val or ""):
        s, _ = rnd.sender_by_local(val)
        return bool(s and s.active)
    return False


# --- what is read ---------------------------------------------------------------------------------

def _teams(seed):
    qs = Team.objects.filter(active=True)
    return qs.filter(pk__in=seed.teams) if seed.teams else qs


def _seen_before(seed):
    seen = set()
    for s in CalendarSeed.objects.exclude(pk=seed.pk).filter(status="done"):
        seen.update(s.scanned or [])
    return seen


def conversations(seed):
    """{(team id, thread): [Mail...]} of the participant-facing mail of the run's teams (every
    mailbox of a team, and the desk's copies of its conversations), oldest first, without the
    invitations SIMEX Calendar sent itself, copies of the same message once."""
    teams = list(_teams(seed))
    qs = (Mail.objects.filter(Q(team__in=teams) | Q(team__isnull=True, desk_team__in=teams))
          .exclude(folder="drafts").filter(trashed_at__isnull=True, event__isnull=True)
          .select_related("owner"))
    if seed.since:
        qs = qs.filter(created_at__gte=seed.since)
    if seed.until:
        qs = qs.filter(created_at__lt=seed.until)
    skip = set() if seed.include_seeded else _seen_before(seed)
    groups = {}
    for m in qs.order_by("created_at", "id")[:20000]:
        tid = m.team_id or m.desk_team_id
        key = (tid, m.thread)
        if f"{tid}:{m.thread}" in skip:
            continue
        groups.setdefault(key, []).append(m)
    out = {}
    for key, ms in list(groups.items())[-MAX_THREADS:]:
        seen, uniq = set(), []
        for m in ms:
            sig = (m.subject, m.from_addr, m.created_at.replace(microsecond=0), (m.body or "")[:200])
            if sig not in seen:
                seen.add(sig)
                uniq.append(m)
        out[key] = uniq
    return out


def _stamp(m):
    real = timezone.localtime(m.created_at).strftime("%a %d %b %Y %H:%M")
    if m.sim_at:
        return f"{real} (scenario: {timezone.localtime(m.sim_at).strftime('%a %d %b %Y %H:%M')})"
    return real


def thread_text(label, team_name, ms, memo, names):
    lines = [f"=== THREAD {label} ({team_name}) ==="]
    for m in ms[-MAX_MAILS:]:
        frm, to = mail_keys(m, memo)
        who = lambda k: f"{key_name(k, names) or k} [{k}]" if k else "?"
        lines.append(f"[M{m.id}] {_stamp(m)}")
        lines.append(f"From: {who(frm)} -> To: {', '.join(who(k) for k in to if k) or '?'}")
        lines.append(f"Subject: {m.subject}")
        lines.append((m.body or "").strip()[:MAIL_CHARS])
        lines.append("")
    return "\n".join(lines)[:THREAD_CHARS]


def chunks(groups):
    """[(text, {label: (team id, thread, [Mail])})], each text under CHUNK_CHARS."""
    memo, names = {}, {}
    team_names = dict(Team.objects.values_list("id", "name"))
    out, cur, size, labels = [], [], 0, {}
    for i, ((tid, thread), ms) in enumerate(groups.items(), 1):
        label = f"T{i}"
        text = thread_text(label, team_names.get(tid, "?"), ms, memo, names)
        if cur and size + len(text) > CHUNK_CHARS:
            out.append(("\n".join(cur), labels))
            cur, size, labels = [], 0, {}
        cur.append(text)
        size += len(text)
        labels[label] = (tid, thread, ms)
    if cur:
        out.append(("\n".join(cur), labels))
    return out, names


def keys_block(labels, names):
    memo = {}
    keys = set()
    for _, _, ms in labels.values():
        for m in ms:
            frm, to = mail_keys(m, memo)
            keys.update(k for k in [frm, box_key(m), *to] if k)
    return "KEYS\n" + "\n".join(f"{k}: {key_name(k, names) or k}" for k in sorted(keys))


# --- turning an answer into items --------------------------------------------------------------------

def _real(text, base, anchor):
    """A start or end the model read -> the real aware time. Scenario times are placed through the
    message they were read in: its real time plus the scenario time since it (at the clock's speed)."""
    try:
        naive = datetime.strptime((text or "")[:16], FMT)
    except ValueError:
        return None
    aware = timezone.make_aware(naive, timezone.get_current_timezone())
    if base == "scenario" and anchor is not None and anchor.sim_at:
        return anchor.created_at + (aware - anchor.sim_at) / (simclock.rate() or 1.0)
    if base == "scenario":
        return simclock.real_at(aware)
    return aware


def allowed_keys(ms, memo=None):
    """The keys the evidence mails belong to or are addressed to: the only calendars an item may
    put an event on."""
    memo = {} if memo is None else memo
    out = set()
    for m in ms:
        frm, to = mail_keys(m, memo)
        out.update(k for k in [frm, box_key(m), *to] if k)
    return sorted(out)


def check(organiser, guests, allowed):
    """(organiser, [{"key", "response"}], problems): only existing keys among allowed, once each."""
    problems = []
    ok = lambda k: k in allowed and key_exists(k)
    if not ok(organiser or ""):
        if organiser:
            problems.append(f"organiser {organiser} left out (not in these conversations)")
        organiser = ""
    out, seen = [], {organiser}
    for g in guests:
        k = (g.get("key") or "").strip()
        if not k or k in seen:
            continue
        if not ok(k):
            problems.append(f"guest {k} left out (not in these conversations)")
            continue
        seen.add(k)
        resp = g.get("response") or g.get("answer") or "needs_action"
        out.append({"key": k, "response": resp if resp in RESPONSES else "needs_action"})
    return organiser, out, problems


def duplicate_of(item):
    """(event, why) an existing event this item repeats: one made from the same conversation, or
    with the same guests within 30 minutes; else (None, "")."""
    if not item.starts_at:
        return None, ""
    same = CalendarEvent.objects.filter(status="confirmed", source_mail__thread=item.thread)
    same = same.filter(Q(team=item.team) | Q(source_mail__team=item.team) | Q(source_mail__desk_team=item.team))
    ev = same.first() if item.thread else None
    if ev:
        return ev, "made from the same conversation"
    keys = {item.organiser_key} | {g["key"] for g in item.guests}
    near = CalendarEvent.objects.filter(status="confirmed", starts_at__gte=item.starts_at - timedelta(minutes=DUPLICATE_MINUTES),
                                        starts_at__lte=item.starts_at + timedelta(minutes=DUPLICATE_MINUTES))
    for ev in near.prefetch_related("attendees"):
        if {a.key for a in ev.attendees.all()} == keys:
            return ev, f"same guests within {DUPLICATE_MINUTES} minutes"
    return None, ""


def _item(seed, labels, mails, raw):
    label = str(raw.get("thread") or "")
    if label not in labels:
        return None
    tid, thread, ms = labels[label]
    by_id = {m.id: m for m in ms}
    sources = []
    for s in raw.get("sources") or []:
        try:
            mid = int(s.get("mail"))
        except (TypeError, ValueError):
            continue
        if mid in by_id:
            sources.append({"mail": mid, "quote": str(s.get("quote") or "")[:300]})
    if not sources:
        return None  # nothing it rests on in what it was given
    evidence = [by_id[s["mail"]] for s in sources]
    allowed = allowed_keys(evidence)
    try:
        anchor = by_id.get(int(raw.get("anchor_mail") or 0)) or evidence[-1]
    except (TypeError, ValueError):
        anchor = evidence[-1]
    start = _real(raw.get("start"), raw.get("time_base"), anchor)
    end = _real(raw.get("end"), raw.get("time_base"), anchor)
    problems = []
    if start and (not end or end <= start):
        end = start + timedelta(hours=1)
        problems.append("no end: one hour")
    organiser, guests, more = check(raw.get("organiser"), raw.get("guests") or [], allowed)
    problems += more
    if not organiser:
        organiser = box_key(evidence[0])
        if organiser not in allowed or not key_exists(organiser):
            return None
        problems.append("organiser unclear: the mailbox of the first message")
    if not start:
        problems.append("no time could be read")
    status = raw.get("status") if raw.get("status") in STATUSES else "proposed"
    mode = raw.get("mode") if raw.get("mode") in dict(calendar.MODES) else "in_person"
    item = CalendarSeedItem(
        seed=seed, team_id=tid, thread=thread, title=str(raw.get("title") or "")[:200] or "(No title)",
        starts_at=start, ends_at=end, all_day=bool(raw.get("all_day")), location=str(raw.get("location") or "")[:200],
        mode=mode, organiser_key=organiser, guests=guests, status=status,
        confidence=max(0, min(100, int(raw.get("confidence") or 0))), sources=sources, allowed=allowed,
        note=str(raw.get("note") or "")[:500], problems=problems)
    item.duplicate, item.duplicate_why = duplicate_of(item)
    item.ticked = bool(start and not item.duplicate and status in ("confirmed", "proposed") and item.confidence >= 60)
    return item


# --- the run ----------------------------------------------------------------------------------------

def start(user, team_ids=(), since=None, until=None, include_seeded=False):
    if not available():
        raise ai.AiError("SIMEX AI is off (AI_PROVIDER, Settings → AI analysis).")
    seed = CalendarSeed.objects.create(user=str(user or "")[:150], teams=[int(t) for t in team_ids], since=since,
                                       until=until, include_seeded=include_seeded)
    inline = ai.RUN_INLINE if RUN_INLINE is None else RUN_INLINE
    if inline:
        return run(seed.id)
    threading.Thread(target=_thread, args=(seed.id,), daemon=True).start()
    return seed


def _thread(seed_id):
    from django.db import connection
    try:
        run(seed_id)
    finally:
        connection.close()


def run(seed_id):
    seed = CalendarSeed.objects.get(pk=seed_id)
    try:
        groups = conversations(seed)
        parts, names = chunks(groups)
        seed.threads, seed.total = len(groups), len(parts)
        seed.save(update_fields=["threads", "total"])
        scanned = []
        for i, (text, labels) in enumerate(parts):
            content = [{"type": "text", "text": keys_block(labels, names) + "\n\n" + text}]
            out, used = ai.call("cal_seed", f"cs{seed.id}-{i}", "deep", system(), content, SCHEMA, "medium", 8000)
            seed.model = used
            mails = {m.id: m for _, _, ms in labels.values() for m in ms}
            items = [x for x in (_item(seed, labels, mails, raw) for raw in out.get("meetings") or []) if x]
            CalendarSeedItem.objects.bulk_create(items)
            scanned += [f"{tid}:{thread}" for tid, thread, _ in labels.values()]
            seed.done = i + 1
            seed.scanned = scanned
            seed.save(update_fields=["done", "scanned", "model"])
        seed.status = "done"
    except ai.AiError as e:
        seed.status, seed.error = "failed", str(e)
    except Exception as e:  # a background thread: never lose the reason
        log.exception("finding meetings failed")
        seed.status, seed.error = "failed", f"{type(e).__name__}: {e}"
    seed.finished_at = timezone.now()
    seed.save()
    n = seed.items.count()
    if seed.status == "done":
        notices.add(f"cal_seed:{seed.id}", "system", "Meetings found in the mail",
                    f"{n} meeting{'s' if n != 1 else ''} in {seed.threads} conversation{'s' if seed.threads != 1 else ''}: check and apply them.",
                    url=f"/meetings/{seed.id}/")
    else:
        notices.add(f"cal_seed:{seed.id}", "system", "Finding meetings in the mail failed", seed.error, url=f"/meetings/{seed.id}/")
    return seed


# --- applying ---------------------------------------------------------------------------------------

DEFAULT_RESPONSE = {"confirmed": "accepted", "proposed": "needs_action", "declined": "declined", "cancelled": "needs_action"}


def apply(seed, user=""):
    """Make the ticked items' events, quietly (no mail, no push): the organiser's mailbox's,
    with a row per guest on their calendars, answers as the mail shows them (a confirmed
    meeting: accepted). Returns (made, problems)."""
    made, problems = 0, []
    names = {}
    for item in seed.items.filter(ticked=True, event__isnull=True).select_related("team"):
        organiser, guests, more = check(item.organiser_key, item.guests, set(item.allowed))
        if not organiser or not item.starts_at or not item.ends_at or item.ends_at <= item.starts_at:
            problems.append(f"{item.title}: needs a time and an organiser from its conversation")
            continue
        org_box = calendar._guest_box({"key": organiser})
        if not org_box:
            problems.append(f"{item.title}: its organiser has no calendar")
            continue
        src = Mail.objects.filter(pk__in=[s["mail"] for s in item.sources]).order_by("id").first()
        with transaction.atomic():
            ev = CalendarEvent.objects.create(
                team=org_box["team"], owner=org_box["owner"], organiser_key=organiser,
                organiser_name=key_name(organiser, names) or organiser, title=item.title, location=item.location,
                description="", mode=item.mode, starts_at=item.starts_at, ends_at=item.ends_at, all_day=item.all_day,
                status="cancelled" if item.status == "cancelled" else "confirmed", source_mail=src, seeded=seed,
                is_test=bool(item.team and item.team.is_test))
            CalendarAttendee.objects.create(event=ev, key=organiser, team=org_box["team"], owner=org_box["owner"],
                                            persona=org_box["persona"], person=org_box["person"],
                                            name=ev.organiser_name, role="organiser", response="accepted",
                                            responded_at=timezone.now())
            boxes = {(calendar._tid(org_box["team"]), calendar._tid(org_box["owner"]), org_box["persona"])}
            for g in guests:
                box = calendar._guest_box({"key": g["key"]})
                if not box:
                    continue
                b3 = (calendar._tid(box["team"]), calendar._tid(box["owner"]), box["persona"])
                if b3 in boxes:
                    continue
                boxes.add(b3)
                resp = g["response"] if g["response"] != "needs_action" else DEFAULT_RESPONSE[item.status]
                CalendarAttendee.objects.create(event=ev, key=g["key"], team=box["team"], owner=box["owner"],
                                                persona=box["persona"], person=box["person"],
                                                name=key_name(g["key"], names) or g["key"], role="required",
                                                response=resp, responded_at=timezone.now() if resp != "needs_action" else None)
            item.event = ev
            item.save(update_fields=["event"])
        made += 1
    seed.applied_at, seed.applied_by = timezone.now(), str(user or "")[:150]
    seed.save(update_fields=["applied_at", "applied_by"])
    from .models import audit
    audit("cal_seed_apply", f"run {seed.id}: {made} events", user)
    return made, problems


def edit(item, data):
    """ExCon's changes to an item from the review table (real times, as ExCon sees them)."""
    item.title = (data.get("title") or item.title).strip()[:200]
    for f in ("start", "end"):
        v = data.get(f)
        if v is not None:
            t = calendar.parse_shown(v, "real")
            setattr(item, "starts_at" if f == "start" else "ends_at", t)
    item.all_day = data.get("all_day") in ("1", "on", "true")
    item.location = (data.get("location") or "").strip()[:200]
    if data.get("mode") in dict(calendar.MODES):
        item.mode = data.get("mode")
    if data.get("status") in STATUSES:
        item.status = data.get("status")
    before = item.organiser_key
    if data.get("organiser") is not None:
        item.organiser_key = data.get("organiser").strip()
    if data.get("guests") is not None:
        old = {g["key"]: g["response"] for g in item.guests}
        keys = [k.strip() for k in data.get("guests").replace(";", ",").split(",") if k.strip()]
        item.guests = [{"key": k, "response": old.get(k, "needs_action")} for k in dict.fromkeys(keys)]
    item.ticked = data.get("ticked") in ("1", "on", "true")
    organiser, guests, problems = check(item.organiser_key, item.guests, set(item.allowed))
    item.problems = [p for p in item.problems if "left out" not in p] + problems
    item.organiser_key, item.guests = organiser or before, guests  # never one from outside its conversations
    item.duplicate, item.duplicate_why = duplicate_of(item)
    item.save()
    return item


def describe(item, names=None):
    """What the review table shows of an item: names of the keys, the evidence and its link."""
    names = {} if names is None else names
    mails = {m.id: m for m in Mail.objects.filter(pk__in=[s["mail"] for s in item.sources])}
    out = []
    for s in item.sources:
        m = mails.get(s["mail"])
        if not m:
            continue
        if m.team_id is None:
            url = f"/inbox/desk/t/{m.thread}/"
        elif m.owner_id is None:
            url = f"/inbox/as/{m.team_id}/t/{m.thread}/"
        elif m.owner and m.owner.is_leader:
            url = f"/inbox/as/{m.team_id}/leader/t/{m.thread}/"
        else:
            url = f"/inbox/as/{m.team_id}/member/{m.owner_id}/t/{m.thread}/"
        out.append({"quote": s["quote"], "subject": m.subject, "url": url, "when": m.created_at})
    return {"organiser": key_name(item.organiser_key, names), "guests": [
        {**g, "name": key_name(g["key"], names) or g["key"]} for g in item.guests],
        "allowed": [{"key": k, "name": key_name(k, names) or k} for k in item.allowed], "sources": out,
        "sim_start": calendar.shown(item.starts_at, "sim") if simclock.enabled() and item.starts_at else ""}


def clear(team=None):
    """Reset (ops/reset.py): every run (team None), or the items of a team's conversations."""
    if team is None:
        CalendarSeed.objects.all().delete()
    else:
        CalendarSeedItem.objects.filter(team=team).delete()

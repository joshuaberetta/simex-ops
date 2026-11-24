"""ExCon's Role play tab (ops/roleplay.py): the board of every sitting by time (who plays it,
where, for which team, in position or done), the slip, the checks, each role play's page
(script, outcomes, players, sittings, files), the import, the role players, their briefing
and FAQ, and printable run sheets. Control-side material: ExCon only (every view
@excon_required)."""

import datetime
import json
import re
from collections import defaultdict
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Prefetch, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_POST

from . import conf, roleplay, signin
from .access import excon_required
from .models import (RP_STATUSES, Person, PushSubscription, RolePlay, RolePlayFile, RolePlaySeen, RolePlaySlot, Team,
                     audit)

MAX_FILE_MB = 20


def _u(request):
    return request.user.get_username()


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _tz():
    return ZoneInfo(settings.TIME_ZONE)


def _slots():
    return (RolePlaySlot.objects.select_related("play")
            .prefetch_related("teams", Prefetch("play__players", queryset=Person.objects.select_related("team"))))


def _days():
    """The dates with sittings, in order (local dates)."""
    dates = sorted({timezone.localtime(s).date() for s in
                    RolePlaySlot.objects.exclude(start=None).values_list("start", flat=True)})
    return dates


def _decorate(slots, now):
    """What the board shows of each slot: its players (and whether each has their alerts on and
    has seen the schedule since it changed), and whether it is now, next or late."""
    subs = set(PushSubscription.objects.values_list("person_id", flat=True))
    seen = dict(RolePlaySeen.objects.values_list("person_id", "at"))
    for s in slots:
        s.players = roleplay.players_of(s)
        for p in s.players:
            p.alerts = p.id in subs
            p.seen = seen.get(p.id)
        s.team_list = list(s.teams.all())
        s.local = timezone.localtime(s.start) if s.start else None
        s.late = s.status == "pending" and s.start and s.start <= now
        s.now = s.status in ("pending", "ready") and s.start and s.start <= now <= s.end() if s.minutes else False
    return slots


@excon_required
def board(request):
    now = timezone.now()
    days = _days()
    today = timezone.localtime(now).date()
    G = request.GET
    day = G.get("day") or (today.isoformat() if today in days else days[0].isoformat() if days else "")
    qs = _slots()
    if day and day != "all":
        try:
            d = datetime.date.fromisoformat(day)
        except ValueError:
            d = today
        start = datetime.datetime.combine(d, datetime.time(0), tzinfo=_tz())
        qs = qs.filter(start__gte=start, start__lt=start + datetime.timedelta(days=1))
    player, team, place, status = _int(G.get("player")), _int(G.get("team")), G.get("location", ""), G.get("status", "")
    if player:
        qs = qs.filter(play__players__id=player)
    if team:
        qs = qs.filter(Q(teams__id=team) | Q(teams=None))
    if place:
        qs = qs.filter(play__location=place)
    if status:
        qs = qs.filter(status=status)
    slots = _decorate(list(qs.distinct()), now)
    if player:  # a team leader only plays their own team's sittings
        slots = [s for s in slots if any(p.id == player for p in s.players)]
    untimed = _decorate(list(_slots().filter(start=None)), now) if day != "all" else []
    nxt = next((s for s in slots if s.start and s.start > now and s.status == "pending"), None)
    all_slots = list(_slots())
    checks = roleplay.grouped_checks(all_slots)
    counts = {k: sum(1 for s in slots if s.status == k) for k in RP_STATUSES}
    return render(request, "ops/roleplay/board.html", {
        "tab": "roleplay", "slots": slots, "untimed": untimed, "days": days, "day": day, "now": now, "next": nxt,
        "checks": checks, "counts": counts, "statuses": RP_STATUSES, "n_plays": RolePlay.objects.count(),
        "players": Person.objects.filter(role_plays__isnull=False).distinct().order_by("name"),
        "teams": Team.objects.filter(active=True), "locations": sorted(set(RolePlay.objects.exclude(location="")
                                                                           .values_list("location", flat=True))),
        "f": {"player": player, "team": team, "location": place, "status": status}, "v": roleplay.version(),
        "lead": roleplay.lead(), "days_map": roleplay.days_map(),
    })


@excon_required
def poll(request):
    v = roleplay.version()
    return JsonResponse({"v": v, "changed": request.GET.get("v") != v})


@excon_required
@require_POST
def slot_status(request, sid):
    s = get_object_or_404(RolePlaySlot, pk=sid)
    status = request.POST.get("status")
    if status not in RP_STATUSES:
        raise Http404
    roleplay.set_status(s, status, f"ExCon ({_u(request)})")
    if request.headers.get("x-requested-with") == "fetch":
        return JsonResponse({"ok": True, "status": status})
    return redirect(request.POST.get("back") or "/roleplay/")


@excon_required
@require_POST
def slip(request):
    P = request.POST
    try:
        d = datetime.date.fromisoformat(P.get("day") or "")
        t = datetime.time.fromisoformat(P.get("from") or "")
        minutes = int(P.get("minutes") or 0)
    except ValueError:
        messages.error(request, "Give the day, the time to slip from and the minutes (negative: earlier).")
        return redirect(P.get("back") or "/roleplay/")
    if not minutes or abs(minutes) > 600:
        messages.error(request, "Slip by 1 to 600 minutes (negative: earlier).")
        return redirect(P.get("back") or "/roleplay/")
    after = datetime.datetime.combine(d, t, tzinfo=_tz())
    moved = roleplay.slip(after, minutes, user=_u(request))
    messages.success(request, f"{len(moved)} pending sitting(s) from {t:%H:%M} moved {minutes:+d} min. "
                              "Their role players get a notification.")
    return redirect(P.get("back") or "/roleplay/")


# --- one role play ---------------------------------------------------------------------------------

EDIT_FIELDS = ["serial", "day", "time_text", "duration", "title", "role", "location", "method", "script", "outcomes",
               "documents", "notes"]


@excon_required
def play_page(request, pid):
    play = get_object_or_404(RolePlay, pk=pid)
    if request.method == "POST":
        P = request.POST
        before = {p.id for p in play.players.all()}
        for f in EDIT_FIELDS:
            if f in P:
                v = P.get(f, "").replace("\r\n", "\n")
                setattr(play, f, v.strip() if f not in ("script", "outcomes", "notes") else v)
        play.optional = P.get("optional") == "1"
        play.save()
        ids = [i for i in map(_int, P.getlist("players")) if i]
        people = list(Person.objects.filter(pk__in=ids, role__in=("rp", "tl", "desk")))
        play.players.set(people)
        if {p.id for p in people} != before:  # tell the role players their schedule changed
            play.slots.filter(status="pending").update(changed_at=timezone.now(), was_start=None)
        roleplay.bump()
        audit("roleplay_edit", play.key, _u(request))
        messages.success(request, "Saved.")
        return redirect(f"/roleplay/{play.id}/")
    now = timezone.now()
    slots = _decorate(list(_slots().filter(play=play)), now)
    chosen = {p.id for p in play.players.all()}
    cands = roleplay.candidates()
    for p in cands:
        p.chosen = p.id in chosen
    return render(request, "ops/roleplay/play.html", {
        "tab": "roleplay", "play": play, "slots": slots, "candidates": cands, "teams": Team.objects.filter(active=True),
        "statuses": RP_STATUSES, "files": play.files.all(), "max_mb": MAX_FILE_MB,
        "checks": [{**c, "label": roleplay.CHECKS[c["kind"]]} for c in roleplay.checks(list(_slots().filter(play=play)))
                   if c["play"].id == play.id],
    })


@excon_required
@require_POST
def play_new(request):
    n = RolePlay.objects.count() + 1
    key = f"RP-{n}"
    while RolePlay.objects.filter(key=key).exists():
        n += 1
        key = f"RP-{n}"
    play = RolePlay.objects.create(key=key, serial=key, title=(request.POST.get("title") or "New role play")[:300],
                                   seq=(RolePlay.objects.order_by("-seq").values_list("seq", flat=True).first() or 0) + 10)
    audit("roleplay_new", key, _u(request))
    return redirect(f"/roleplay/{play.id}/")


@excon_required
@require_POST
def play_delete(request, pid):
    play = get_object_or_404(RolePlay, pk=pid)
    audit("roleplay_delete", play.key, _u(request))
    play.delete()
    roleplay.bump()
    messages.success(request, f"Role play {play.serial} deleted.")
    return redirect("/roleplay/")


@excon_required
@require_POST
def slots_save(request, pid):
    """Edit the sittings: each one's time, length and teams, delete some, add one, or rebuild
    them all from the time text (only while none has started)."""
    play = get_object_or_404(RolePlay, pk=pid)
    P = request.POST
    if P.get("rebuild"):
        if roleplay.build_slots(play):
            messages.success(request, f"Sittings rebuilt from \"{play.time_text}\".")
        else:
            messages.error(request, "A sitting has started: edit them one by one instead.")
        roleplay.bump()
        return redirect(f"/roleplay/{play.id}/#slots")
    teams = {t.id: t for t in Team.objects.all()}
    now = timezone.now()
    for s in play.slots.all():
        if P.get(f"delete-{s.id}"):
            s.delete()
            continue
        start = _local_dt(P.get(f"start-{s.id}"))
        new_teams = [teams[i] for i in map(_int, P.getlist(f"teams-{s.id}")) if i in teams]
        minutes = _int(P.get(f"minutes-{s.id}"))
        moved = start != s.start
        if moved and s.status == "pending":
            s.was_start, s.changed_at, s.reminded = s.start, now, ""
        s.start, s.minutes = start, minutes
        s.save()
        if {t.id for t in new_teams} != {t.id for t in s.teams.all()}:
            s.teams.set(new_teams)
            if s.status == "pending":
                RolePlaySlot.objects.filter(pk=s.pk).update(changed_at=now)
    start = _local_dt(P.get("start-new"))
    if start:
        s = RolePlaySlot.objects.create(play=play, start=start, minutes=_int(P.get("minutes-new")))
        s.teams.set([teams[i] for i in map(_int, P.getlist("teams-new")) if i in teams])
    roleplay.bump()
    audit("roleplay_slots", play.key, _u(request))
    messages.success(request, "Sittings saved. Role players whose sittings moved get a notification.")
    return redirect(f"/roleplay/{play.id}/#slots")


def _local_dt(v):
    """A datetime-local field (exercise time) -> aware datetime, or None."""
    try:
        return datetime.datetime.fromisoformat(v).replace(tzinfo=_tz()) if v else None
    except ValueError:
        return None


@excon_required
@require_POST
def files_upload(request, pid):
    play = get_object_or_404(RolePlay, pk=pid)
    n = 0
    for f in request.FILES.getlist("files"):
        if f.size > MAX_FILE_MB * 1024 * 1024:
            messages.error(request, f"{f.name}: larger than {MAX_FILE_MB} MB.")
            continue
        RolePlayFile.objects.create(play=play, filename=f.name[:300], size=f.size, data=f.read(),
                                    content_type=(f.content_type or "application/octet-stream")[:120])
        n += 1
    if n:
        audit("roleplay_files", f"{play.key}: {n}", _u(request))
        messages.success(request, f"{n} file(s) added.")
    return redirect(f"/roleplay/{play.id}/#files")


@excon_required
@require_POST
def file_delete(request, fid):
    f = get_object_or_404(RolePlayFile, pk=fid)
    pid = f.play_id
    f.delete()
    return redirect(f"/roleplay/{pid}/#files")


def file_response(f):
    resp = HttpResponse(bytes(f.data), content_type="application/octet-stream")  # download only
    resp["Content-Disposition"] = content_disposition_header(True, re.sub(r"[\r\n]+", " ", f.filename))
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


@excon_required
def file_get(request, fid):
    return file_response(get_object_or_404(RolePlayFile, pk=fid))


# --- import ------------------------------------------------------------------------------------------

@excon_required
def import_page(request):
    """Upload a MEL-MIL workbook or a role-player schedule: the plan, then apply it."""
    if request.method == "POST" and request.FILES.get("file"):
        up = request.FILES["file"]
        data = up.read()
        try:
            if up.name.lower().endswith((".docx", ".md", ".txt", ".markdown")):
                sources = roleplay.read_scripts(data, up.name)  # the role players' scripts document
            else:
                sources = roleplay.read_workbook(data)
        except roleplay.RolePlayImportError as e:
            messages.error(request, f"{up.name}: {e}")
            return redirect("/roleplay/import/")
        days = {**roleplay.days_map(), **roleplay.guess_days(data)}
        request.session["rp_import"] = {"name": up.name, "sources": sources, "days": days,
                                        "sheets": sorted(roleplay.default_sheets(sources))}
        return redirect("/roleplay/import/")
    state = request.session.get("rp_import")
    if not state:
        return render(request, "ops/roleplay/import.html", {"tab": "roleplay", "state": None})
    if request.GET.get("sheets") is not None or request.GET.get("cancel"):
        if request.GET.get("cancel"):
            request.session.pop("rp_import", None)
            return redirect("/roleplay/")
        state["sheets"] = request.GET.getlist("sheet")
        request.session["rp_import"] = state
        return redirect("/roleplay/import/")
    recs = roleplay.records(state["sources"], set(state["sheets"]))
    rows, missing, new_names = roleplay.plan(recs)
    if all(s["kind"] == "scripts" for s in state["sources"]):
        missing = []  # a scripts document isn't the whole schedule
    counts = {a: sum(1 for r in rows if r["action"] == a) for a in ("new", "update", "conflict", "unchanged")}
    days_used = sorted({roleplay.weekday(r.get("day", "")) for r in recs if roleplay.weekday(r.get("day", ""))},
                       key=roleplay.WEEKDAYS.index)
    order = roleplay.team_order()
    for r in rows:
        rec, play = r["rec"], r.setdefault("play", None)
        val = lambda f: rec[f] if f in rec else getattr(play, f, "") if play else ""  # noqa: E731
        r["slots"] = roleplay.parse_slots(val("time_text"), val("duration"), val("role"), order)
        r["show"] = {f: val(f) for f in ("day", "title", "time_text")}  # a scripts document leaves these as they are
    return render(request, "ops/roleplay/import.html", {
        "tab": "roleplay", "state": state, "rows": rows, "missing": missing, "new_names": new_names, "counts": counts,
        "days": [(d, state["days"].get(d, "")) for d in days_used],
        "sources": [{"sheet": s["sheet"], "kind": s["kind"], "n": len(s["rows"]), "on": s["sheet"] in state["sheets"]}
                    for s in state["sources"]],
    })


@excon_required
@require_POST
def import_apply(request):
    state = request.session.pop("rp_import", None)
    if not state:
        messages.error(request, "Nothing to import: upload the file again.")
        return redirect("/roleplay/import/")
    P = request.POST
    days = dict(roleplay.days_map())
    for k in roleplay.WEEKDAYS:
        v = (P.get(f"day-{k}") or "").strip()
        if v:
            try:
                datetime.date.fromisoformat(v)
                days[k] = v
            except ValueError:
                pass
    recs = roleplay.records(state["sources"], set(state["sheets"]))
    scripts_only = all(s["kind"] == "scripts" for s in state["sources"])
    counts = roleplay.apply(recs, overwrite_local=P.get("overwrite") == "1",
                            delete_missing=P.get("delete") == "1" and not scripts_only,
                            days=days, user=_u(request))
    roleplay.bump()
    messages.success(request, f"Role plays imported from {state['name']}: {counts['new']} new, {counts['updated']} "
                              f"updated, {counts['players_added']} role player(s) added"
                              + (f", {counts['kept_local']} edit(s) of yours kept" if counts["kept_local"] else "")
                              + (f", {counts['deleted']} deleted" if counts["deleted"] else "") + ".")
    return redirect("/roleplay/")


@excon_required
@require_POST
def days_save(request):
    days = {}
    for k in roleplay.WEEKDAYS:
        v = (request.POST.get(f"day-{k}") or "").strip()
        if v:
            try:
                datetime.date.fromisoformat(v)
                days[k] = v
            except ValueError:
                messages.error(request, f"{k.capitalize()}: not a date.")
                return redirect("/roleplay/players/#days")
    n = roleplay.redate(days, _u(request))
    roleplay.bump()
    messages.success(request, f"Exercise days saved; {n} pending sitting(s) moved to their day's new date.")
    return redirect("/roleplay/players/#days")


# --- role players, briefing, print ---------------------------------------------------------------

@excon_required
def players_page(request):
    people = list(Person.objects.filter(Q(role="rp") | Q(role_plays__isnull=False)).distinct()
                  .select_related("team", "user").annotate(n_subs=Count("push_subscriptions", distinct=True)))
    seen = dict(RolePlaySeen.objects.values_list("person_id", "at"))
    for p in people:
        p.slots = roleplay.slots_for(p)
        p.n_plays = len({s.play_id for s in p.slots})
        p.next = next((s for s in p.slots if s.start and s.start > timezone.now() and s.status == "pending"), None)
        p.seen = seen.get(p.id)
        p.can_qr = bool(p.user and signin.can_use(p.user))
    people.sort(key=lambda p: (p.role != "rp", p.name.lower()))
    used = sorted({roleplay.weekday(d) for d in RolePlay.objects.values_list("day", flat=True) if roleplay.weekday(d)}
                  | set(roleplay.days_map()), key=roleplay.WEEKDAYS.index)
    return render(request, "ops/roleplay/players.html", {
        "tab": "roleplay", "people": people, "briefing": conf.get("rp_briefing"), "faq": conf.get("rp_faq"),
        "lead": roleplay.lead(), "rp_email": conf.flag("rp_email"),
        "days": [(d, roleplay.days_map().get(d, "")) for d in used],
    })


@excon_required
@require_POST
def briefing_save(request):
    P = request.POST
    for k in ("rp_briefing", "rp_faq"):
        if k in P:
            conf.put(k, P.get(k, "").replace("\r\n", "\n"))
    if "rp_lead" in P:
        lead = _int(P.get("rp_lead"))
        conf.put("rp_lead", str(max(0, min(lead, 120))) if lead is not None else str(roleplay.DEFAULT_LEAD))
        conf.put("rp_email", "1" if P.get("rp_email") == "1" else "0")
    audit("roleplay_briefing", "", _u(request))
    messages.success(request, "Saved.")
    return redirect(request.POST.get("back") or "/roleplay/players/")


@excon_required
def print_page(request):
    """A run sheet: one role player's sittings (?player=), one location's (?location=), or all,
    for a day (?day=), with each role play's script and documents."""
    G = request.GET
    qs = _slots()
    title = "Role-play run sheet"
    if G.get("day") and G["day"] != "all":
        try:
            d = datetime.date.fromisoformat(G["day"])
            start = datetime.datetime.combine(d, datetime.time(0), tzinfo=_tz())
            qs = qs.filter(start__gte=start, start__lt=start + datetime.timedelta(days=1))
            title += f" · {d:%A %d %B}"
        except ValueError:
            pass
    person = None
    if _int(G.get("player")):
        person = get_object_or_404(Person, pk=_int(G["player"]))
        ids = set(qs.values_list("id", flat=True))
        slots = [s for s in roleplay.slots_for(person) if s.id in ids]
        title = f"{person.name} · " + title
    else:
        if G.get("location"):
            qs = qs.filter(play__location=G["location"])
            title = f"{G['location']} · " + title
        slots = list(qs)
    slots = _decorate(slots, timezone.now())
    plays, seen = [], set()
    for s in slots:
        if s.play_id not in seen:
            seen.add(s.play_id)
            plays.append(s.play)
    return render(request, "ops/roleplay/print.html", {"title": title, "slots": slots, "plays": plays,
                                                       "person": person, "scripts": G.get("scripts", "1") == "1",
                                                       "briefing": conf.get("rp_briefing") if person else ""})


@excon_required
@require_POST
def from_event(request, eid):
    """Meetings, the desk's calendar → Make a role play: a persona's meeting on the Role play
    board (roleplay.from_event), its sitting following the event."""
    from .models import CalendarEvent
    ev = get_object_or_404(CalendarEvent, pk=eid)
    if not ev.attendees.filter(persona__gt="").exists() and not ev.organiser_key.startswith("p:"):
        msg = "Only a meeting with a persona becomes a role play."
        if request.headers.get("x-requested-with") == "fetch":
            return JsonResponse({"ok": False, "error": msg}, status=400)
        messages.error(request, msg)
    else:
        rp = roleplay.from_event(ev, _u(request))
        if request.headers.get("x-requested-with") == "fetch":
            return JsonResponse({"ok": True, "key": rp.key, "url": f"/roleplay/{rp.id}/"})
        messages.success(request, f"Role play {rp.key} made: its sitting follows the meeting.")
    nxt = request.POST.get("next") or "/meetings/"
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/meetings/")

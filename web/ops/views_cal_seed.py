"""Team inboxes → Meetings in the mail (ExCon only): SIMEX AI finds the meetings already arranged
in the teams' mail (ops/cal_seed.py); ExCon checks them and puts them on the calendars."""

from datetime import datetime

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import ai, cal_seed, calendar
from .access import excon_required
from .models import CalendarSeed, Team


def _day(text, end=False):
    try:
        d = datetime.strptime((text or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None
    if end:
        d = d.replace(hour=23, minute=59)
    return timezone.make_aware(d, timezone.get_current_timezone())


@excon_required
def meetings(request):
    if request.method == "POST":
        teams = [int(t) for t in request.POST.getlist("team") if t.isdigit()]
        try:
            seed = cal_seed.start(request.user.get_username(), teams, _day(request.POST.get("since")),
                                  _day(request.POST.get("until"), end=True), request.POST.get("include_seeded") == "1")
        except ai.AiError as e:
            messages.error(request, str(e))
            return redirect("/meetings/find/")
        return redirect(f"/meetings/{seed.id}/")
    return render(request, "ops/meetings.html", {
        "tab": "inboxes", "runs": CalendarSeed.objects.all()[:30], "teams": Team.objects.filter(active=True),
        "ai_ok": cal_seed.available()})


@excon_required
def run(request, sid):
    seed = get_object_or_404(CalendarSeed, pk=sid)
    if request.method == "POST":
        for item in seed.items.filter(event__isnull=True):
            p = f"i{item.id}-"
            data = {k[len(p):]: v for k, v in request.POST.items() if k.startswith(p)}
            if data:
                cal_seed.edit(item, data)
        if request.POST.get("action") == "apply":
            made, problems = cal_seed.apply(seed, request.user.get_username())
            messages.success(request, f"{made} event{'s' if made != 1 else ''} put on the calendars, without any mail.")
            for p in problems:
                messages.error(request, p)
        else:
            messages.success(request, "Saved.")
        return redirect(f"/meetings/{seed.id}/")
    names = {}
    items = list(seed.items.select_related("team", "duplicate", "event"))
    for it in items:
        it.d = cal_seed.describe(it, names)
        it.team_name = it.team.name if it.team else ""
        it.start_text = calendar.shown(it.starts_at, "real") if it.starts_at else ""
        it.end_text = calendar.shown(it.ends_at, "real") if it.ends_at else ""
    return render(request, "ops/meeting_run.html", {
        "tab": "inboxes", "seed": seed, "items": items, "modes": calendar.MODES, "statuses": cal_seed.STATUSES,
        "teams": dict(Team.objects.values_list("id", "name"))})

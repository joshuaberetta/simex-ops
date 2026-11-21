"""ExCon views for the end-of-exercise reports (ops/report.py): Dashboard → Reports, each
report with its editable sections, and the print views. Every view is excon_required."""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import ai, report
from .access import excon_required
from .models import ExerciseReport, Report, audit
from .views_excon import _u


def _ai():
    return {"ai_available": ai.available(), "ai_enabled": ai.enabled(), "ai_spent": ai.spent_today(), "ai_cap": ai.cap()}


def _parts(rep):
    """The sections to show: (key, title, Markdown, edited)."""
    return [(k, title, rep.sections.get(k, ""), k in rep.edited) for k, title in report.section_list(rep)]


def _figures(rep, team_view):
    return report.for_team(rep.figures, rep.exercise.include_ai_written) if team_view and rep.team_id else rep.figures


@excon_required
def reports(request):
    report.expire()
    kind = "test" if request.GET.get("kind") == "test" else "live"
    return render(request, "ops/reports.html", {
        "tab": "dashboard", "kind": kind, "compiles": ExerciseReport.objects.filter(kind=kind).prefetch_related("reports")[:20],
        "teams": report.teams_of(kind), **_ai()})


@excon_required
@require_POST
def reports_compile(request):
    kind = "test" if request.POST.get("kind") == "test" else "live"
    ex = report.start(kind, include_ai_written=request.POST.get("include_ai_written") == "1", user=_u(request))
    audit("reports_compile", f"#{ex.id} ({kind})", _u(request))
    return redirect(f"/reports/{ex.id}/")


@excon_required
def exercise_report(request, eid):
    report.expire()
    ex = get_object_or_404(ExerciseReport, pk=eid)
    reps = list(ex.reports.select_related("team"))
    return render(request, "ops/reports_exercise.html", {
        "tab": "dashboard", "ex": ex, "reps": reps, "busy": any(r.status in ("queued", "running") for r in reps), **_ai()})


@excon_required
def report_page(request, rid):
    report.expire()
    rep = get_object_or_404(Report.objects.select_related("exercise", "team"), pk=rid)
    return render(request, "ops/report.html", {
        "tab": "dashboard", "rep": rep, "ex": rep.exercise, "parts": _parts(rep), "f": rep.figures,
        "team_view": False, "busy": rep.status in ("queued", "running"), **_ai()})


@excon_required
@require_POST
def report_section(request, rid):
    rep = get_object_or_404(Report, pk=rid)
    key = request.POST.get("key") or ""
    ok = (report.reset_section(rep, key) if request.POST.get("action") == "reset"
          else report.save_section(rep, key, request.POST.get("text") or ""))
    if ok:
        audit("report_edit", f"#{rep.id} {key}", _u(request))
    else:
        messages.error(request, "Nothing to change.")
    return redirect(f"/reports/part/{rep.id}/#s-{key}")


@excon_required
@require_POST
def report_rewrite(request, rid):
    rep = get_object_or_404(Report, pk=rid)
    if not ai.enabled():
        messages.error(request, "AI analysis is off (Settings).")
    elif rep.status in ("queued", "running"):
        messages.error(request, "It is being written already.")
    else:
        report.rewrite(rep)
        audit("report_rewrite", f"#{rep.id}", _u(request))
    return redirect(f"/reports/part/{rep.id}/")


@excon_required
def report_print(request, rid):
    rep = get_object_or_404(Report.objects.select_related("exercise", "team"), pk=rid)
    return render(request, "ops/report_print.html", {
        "ex": rep.exercise, "items": [(rep, _parts(rep), _figures(rep, True))]})


@excon_required
def exercise_print(request, eid):
    ex = get_object_or_404(ExerciseReport, pk=eid)
    reps = list(ex.reports.select_related("team"))
    reps.sort(key=lambda r: r.team_id is None)  # the teams', then ExCon's
    if request.GET.get("only") == "teams":
        reps = [r for r in reps if r.team_id]
    return render(request, "ops/report_print.html", {"ex": ex, "items": [(r, _parts(r), _figures(r, True)) for r in reps]})

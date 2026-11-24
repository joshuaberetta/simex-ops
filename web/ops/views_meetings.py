"""Team inboxes → Meetings (ExCon only, ops/meetings.py): every event on SIMEX Calendar, filtered,
exported, refreshed by version. Meetings in the mail (SIMEX AI) is its Find tab, /meetings/find/."""

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.http import content_disposition_header

from . import meetings
from .access import excon_required
from .models import Team

FILTERS = ("kind", "team", "persona", "status", "when", "hours", "since", "until", "needs")


def _filters(request):
    return {k: (request.GET.get(k) or "").strip() for k in FILTERS}


@excon_required
def overview(request):
    f = _filters(request)
    return render(request, "ops/meetings_overview.html", {
        "tab": "inboxes", "f": f, "rows": meetings.rows(f), "v": meetings.version(),
        "teams": Team.objects.filter(active=True, is_test=(f["kind"] == "test")), "personas": meetings.personas(),
        "query": request.GET.urlencode()})


@excon_required
def version(request):
    return JsonResponse({"v": meetings.version()})


@excon_required
def export(request, fmt):
    f = _filters(request)
    if fmt == "xlsx":
        data, ctype = meetings.xlsx_bytes(f), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        data, ctype = meetings.csv_bytes(f), "text/csv; charset=utf-8"
    r = HttpResponse(data, content_type=ctype)
    r["Content-Disposition"] = content_disposition_header(True, f"simex-meetings.{'xlsx' if fmt == 'xlsx' else 'csv'}")
    return r

"""SIMEX Calendar's pages (ops/calendar.py), mounted on each mailbox's base like the mail client:
/inbox/calendar/ (a member: the team's and their own; a team leader: their own, the team's and
their members', read-only), /inbox/me/calendar/, /inbox/desk/calendar/ (one layer per persona),
and read-only at /inbox/team/calendar/, /inbox/members/<pid>/calendar/ and
/inbox/as/<team>/.../calendar/. Every view is @mailbox_view, so the Viewer decides the
mailboxes; changes are POSTs, which a read-only view refuses."""

from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from . import calendar as cal
from .models import CalendarEvent
from .views_inbox import _ctx, mailbox_view


def _check(v):
    if v.peer:  # another team leader's mailbox: read-only mail, no calendar
        raise Http404


@mailbox_view
def page(request, v):
    _check(v)
    return render(request, "ops/mail/calendar/page.html", _ctx(v, folder="", cal=cal.page_data(v)))


@mailbox_view
def events(request, v):
    _check(v)
    only = set(request.GET.getlist("layer")) or None
    try:
        rows = cal.events(v, request.GET.get("from"), request.GET.get("to"), only)
    except cal.CalendarError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    now, rate = cal.now_shown(cal.clock(v))
    return JsonResponse({"ok": True, "events": rows, "now": now, "rate": rate})


@mailbox_view
@require_POST
def save(request, v):
    _check(v)
    try:
        eid = int(request.POST.get("id") or 0) or None
    except ValueError:
        eid = None
    try:
        ev, lay = cal.save(v, request.POST, eid)
    except cal.CalendarError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    except CalendarEvent.DoesNotExist:
        raise Http404
    clk = cal.clock(v)
    return JsonResponse({"ok": True, "event": cal.as_json(ev, lay, clk, cal.shows_both(v))})


@mailbox_view
@require_POST
def delete(request, v, eid):
    _check(v)
    try:
        cal.delete(v, eid)
    except CalendarEvent.DoesNotExist:
        raise Http404
    return JsonResponse({"ok": True})

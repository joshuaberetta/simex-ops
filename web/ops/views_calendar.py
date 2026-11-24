"""SIMEX Calendar's pages (ops/calendar.py), mounted on each mailbox's base like the mail client:
/inbox/calendar/ (a member: the team's and their own; a team leader: their own, the team's and
their members', read-only), /inbox/me/calendar/, /inbox/desk/calendar/ (one layer per persona),
and read-only at /inbox/team/calendar/, /inbox/members/<pid>/calendar/ and
/inbox/as/<team>/.../calendar/. Every view is @mailbox_view, so the Viewer decides the
mailboxes; changes are POSTs, which a read-only view refuses."""

from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
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
    pre = cal.prefill(v, request.GET.get("from", "")[:40]) if request.GET.get("from") and not v.readonly else None
    return render(request, "ops/mail/calendar/page.html", _ctx(v, folder="", cal=cal.page_data(v, pre)))


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
        ev, lay, warnings = cal.save(v, request.POST, eid, request)
    except cal.CalendarError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    except CalendarEvent.DoesNotExist:
        raise Http404
    clk = cal.clock(v)
    return JsonResponse({"ok": True, "event": cal.as_json(ev, lay, clk, cal.shows_both(v)), "warning": "; ".join(warnings)})


@mailbox_view
@require_POST
def delete(request, v, eid):
    _check(v)
    try:
        warnings = cal.delete(v, eid, request)
    except CalendarEvent.DoesNotExist:
        raise Http404
    except cal.CalendarError as e:  # the cancellations couldn't go (e.g. live sending not armed)
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return JsonResponse({"ok": True, "warning": "; ".join(warnings)})


@mailbox_view
@require_POST
def respond(request, v):
    """Yes / Maybe / No to an invitation, from the calendar's popover (fetch) or the invitation
    card in a conversation (a form, back to it)."""
    _check(v)
    P = request.POST
    ajax = request.headers.get("x-requested-with") == "fetch"
    nxt = P.get("next") or ""
    nxt = nxt if nxt.startswith(v.base + "/") and "//" not in nxt[1:] else f"{v.base}/"
    try:
        eid = int(P.get("event") or 0)
    except ValueError:
        raise Http404
    try:
        ev, warnings = cal.respond(v, eid, P.get("layer") or "", P.get("answer") or "", P.get("note") or "", request)
    except CalendarEvent.DoesNotExist:
        raise Http404
    except cal.CalendarError as e:
        if ajax:
            return JsonResponse({"ok": False, "error": str(e)}, status=400)
        from django.contrib import messages
        messages.error(request, str(e))
        return redirect(nxt)
    if ajax:
        return JsonResponse({"ok": True, "warning": "; ".join(warnings)})
    from django.contrib import messages
    messages.success(request, f"{cal.ANSWERS[P.get('answer')]}: {ev.title}" + (f" ({'; '.join(warnings)})" if warnings else ""))
    return redirect(nxt)

"""Dashboard → Network: ExCon's live map of who writes to whom (ops/network.py,
static/ops/network.js). The page, its data and the poll that keeps it moving; every view is
excon_required. With ?screen=1 the page is the wall-screen version (no menus, dark). The persona
desk has the same map as a tab of its own (/inbox/desk/network/, desk members and ExCon): the desk
already reads every team's mailboxes, and the map shows no more than who wrote to whom."""

from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render

from . import network, simclock
from .access import desk_of, excon_required

CACHE_S = 4  # several screens open on the map read the same figures


def _kind(request):
    return "test" if request.GET.get("kind") == "test" else "live"


def _cfg(prefix, kind, screen):
    return {"data": f"{prefix}data/?kind={kind}", "poll": f"{prefix}poll/?kind={kind}", "kind": kind, "screen": screen}


@excon_required
def network_page(request):
    kind = _kind(request)
    screen = request.GET.get("screen") == "1"
    ctx = {"tab": "dashboard", "kind": kind, "screen": screen, "back_url": f"/dashboard/network/?kind={kind}",
           "back_label": "Back to the Dashboard", "cfg_js": _cfg("/dashboard/network/", kind, screen)}
    return render(request, "ops/network_screen.html" if screen else "ops/network.html", ctx)


@excon_required
def network_data(request):
    return _data(request)


@excon_required
def network_poll(request):
    return _poll(request)


def _data(request):
    kind = _kind(request)
    key = f"network-graph-{kind}"
    data = cache.get(key)
    if data is None:
        data = network.graph(kind)
        cache.set(key, data, CACHE_S)
    return JsonResponse({**data, "clock": simclock.js()})


def _poll(request):
    """What was sent since the cursor (t, s, r of the last answer): the page adds it to the map
    and sends a dot along each new line. A node it doesn't know makes it reload the map."""
    kind = _kind(request)
    cursor = {k: request.GET.get(k) for k in ("t", "s", "r")}
    sends, cursor = network.sends(kind, cursor)
    return JsonResponse({"sends": sends, "cursor": cursor, "clock": simclock.js()})


# --- the persona desk's Network tab ------------------------------------------------------------------

def _desk_page(view):
    from .views_inbox import _desk_page as wrap  # views_inbox is the desk's home; imported late
    return wrap(view)


@_desk_page
def desk_network_page(request):
    from .views_inbox import DESK, box_theme, desk_tabs
    kind = _kind(request)
    screen = request.GET.get("screen") == "1"
    cfg = _cfg("/inbox/desk/network/", kind, screen)
    if screen:
        return render(request, "ops/network_screen.html", {
            "kind": kind, "screen": True, "back_url": f"/inbox/desk/network/?kind={kind}",
            "back_label": "Back to the persona desk", "cfg_js": cfg})
    person = desk_of(request.user)
    return render(request, "ops/mail/desk_network.html", {
        "team": DESK, "me": person, "base": "/inbox/desk", "readonly": True, "v": None, "back_url": "/inbox/desk/",
        "desk_tabs": desk_tabs(person, "network", request.user), "kind": kind, "screen": False, "cfg_js": cfg,
        "box": box_theme(kind="desk", title="Persona desk", sub="Network"),
        "simex_js": {"base": "/inbox/desk", "readonly": True, "book": [], "team": DESK["name"], "leader": False,
                     "folder": "", "unread": 0, "vapid": "", "pushBase": "/inbox/desk", "labels": []}})


@_desk_page
def desk_network_data(request):
    return _data(request)


@_desk_page
def desk_network_poll(request):
    return _poll(request)

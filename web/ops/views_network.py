"""Dashboard → Network: ExCon's live map of who writes to whom (ops/network.py,
static/ops/network.js). The page, its data and the poll that keeps it moving; every view is
excon_required. With ?screen=1 the page is the wall-screen version (no menus, dark)."""

from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render

from . import network, simclock
from .access import excon_required

CACHE_S = 4  # several screens open on the map read the same figures


def _kind(request):
    return "test" if request.GET.get("kind") == "test" else "live"


@excon_required
def network_page(request):
    kind = _kind(request)
    screen = request.GET.get("screen") == "1"
    ctx = {"tab": "dashboard", "kind": kind, "screen": screen,
           "cfg_js": {"data": f"/dashboard/network/data/?kind={kind}", "poll": f"/dashboard/network/poll/?kind={kind}",
                      "kind": kind, "screen": screen}}
    return render(request, "ops/network_screen.html" if screen else "ops/network.html", ctx)


@excon_required
def network_data(request):
    kind = _kind(request)
    key = f"network-graph-{kind}"
    data = cache.get(key)
    if data is None:
        data = network.graph(kind)
        cache.set(key, data, CACHE_S)
    return JsonResponse({**data, "clock": simclock.js()})


@excon_required
def network_poll(request):
    """What was sent since the cursor (t, s, r of the last answer): the page adds it to the map
    and sends a dot along each new line. A node it doesn't know makes it reload the map."""
    kind = _kind(request)
    cursor = {k: request.GET.get(k) for k in ("t", "s", "r")}
    sends, cursor = network.sends(kind, cursor)
    return JsonResponse({"sends": sends, "cursor": cursor, "clock": simclock.js()})

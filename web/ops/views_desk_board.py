"""The persona desk's Assign tab (ops/desk_board.py): the board of who has which conversations,
moved by dragging (it posts to /inbox/desk/mark/, desk.mark), and the default assignments of
personas to the people on the desk, by team. Desk members and ExCon (_desk_page)."""

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import desk, desk_board, desk_stats
from .access import desk_of
from .views_inbox import DESK, _desk_page, box_theme, desk_tabs


def _name(request):
    person = desk_of(request.user)
    return person.name if person else (request.user.get_full_name() or request.user.get_username())


def _int(x):
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def _args(request):
    G = request.GET
    have = desk_stats.kinds()
    kind = G.get("kind") or ("test" if have == {"test"} else "live")
    kind = kind if kind in ("live", "test") else "live"
    recent = _int(G.get("recent"))
    return {"kind": kind, "recent": recent if recent in desk_board.RECENT else 0,
            "team_id": _int(G.get("team")) or None, "sender_id": _int(G.get("persona")) or None}, have


def _page(request, current, sub, **ctx):
    person = desk_of(request.user)
    return {"team": DESK, "me": person, "base": "/inbox/desk", "readonly": True, "v": None, "back_url": "/inbox/desk/",
            "desk_tabs": desk_tabs(person, "assign", request.user), "assign_tab": current,
            "box": box_theme(kind="desk", title="Persona desk", sub=sub),
            "simex_js": {"base": "/inbox/desk", "readonly": True, "book": [], "team": DESK["name"], "leader": False,
                         "folder": "", "unread": 0, "vapid": "", "pushBase": "/inbox/desk", "labels": []}, **ctx}


@_desk_page
def board(request):
    """The board: a column for nobody and for each person on the desk; drag a conversation onto
    someone to give it to them."""
    args, have = _args(request)
    data = desk_board.board(request.user, **args)
    return render(request, "ops/mail/desk_assign.html", _page(
        request, "board", "Assign", data=data, v_board=desk_board.version(args["kind"]), args=args, have=have,
        filters=desk_board.filters(), recent_choices=desk_board.RECENT))


@_desk_page
def board_json(request):
    """The board for desk_board.js: {"v"} and, when it changed since ?v=, {"board"}."""
    args, _ = _args(request)
    v = desk_board.version(args["kind"])
    if request.GET.get("v") == v:
        return JsonResponse({"v": v})
    return JsonResponse({"v": v, "board": desk_board.board(request.user, **args)})


@_desk_page
def defaults(request):
    """Default assignments: who has each persona's conversations, for all teams or one."""
    return render(request, "ops/mail/desk_defaults.html", _page(request, "defaults", "Default assignments",
                                                                g=desk_board.grid()))


@_desk_page
@require_POST
def defaults_set(request):
    """A cell (sender, team: "" = all teams, user: "" = nobody), a whole row (act row) or a row
    cleared (act clear)."""
    P = request.POST
    fetch = request.headers.get("x-requested-with") == "fetch"
    try:
        act = P.get("act") or "set"
        if act == "set":
            desk_board.set_rule(_int(P.get("sender")), _int(P.get("team")) or None, _int(P.get("user")) or None, _name(request))
        elif act == "row":
            desk_board.set_row(_int(P.get("sender")), _int(P.get("user")) or None, _name(request))
        elif act == "clear":
            desk_board.set_row(_int(P.get("sender")), None, _name(request))
        else:
            raise desk_board.Invalid("Unknown action.")
    except desk_board.Invalid as e:
        if fetch:
            return JsonResponse({"ok": False, "error": str(e)}, status=400)
        messages.error(request, str(e))
        return redirect("/inbox/desk/assign/defaults/")
    if fetch:
        return JsonResponse({"ok": True, "unassigned": len(desk.unassigned_open())})
    return redirect("/inbox/desk/assign/defaults/")


@_desk_page
@require_POST
def defaults_apply(request):
    """Apply now: the open conversations nobody has go to their default owners."""
    n = desk.apply_defaults(request.user, _name(request))
    messages.success(request, f"{n} conversation{'' if n == 1 else 's'} given by the default assignments."
                     if n else "No open conversation without an owner has a default assignment.")
    nxt = request.POST.get("next") or ""
    return redirect(nxt if nxt.startswith("/inbox/desk/assign/") else "/inbox/desk/assign/defaults/")

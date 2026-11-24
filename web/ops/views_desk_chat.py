"""ChatSIMEX on the persona desk (ops/desk_chat.py): a desk member's (or ExCon staff's, on the
desk) own chats with SIMEX AI. Every chat is its owner's alone: another login's is a 404. The
answers can quote role-play scripts, planned injects, ExCon's notes and teams' internal mail,
so only these views serve them."""

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from . import ai, desk_chat
from .models import DeskChat, Team, audit
from .views_suggest import _who, desk_or_excon


def _scope(src):
    """{team, persona, thread} from a request, checked."""
    out = {}
    tid = src.get("team")
    if tid and str(tid).isdigit() and Team.objects.filter(pk=int(tid)).exists():
        out["team"] = int(tid)
    persona = (src.get("persona") or "").strip()[:120]
    if persona:
        out["persona"] = persona
    thread = (src.get("thread") or "").strip()[:40]
    if thread:
        if not desk_chat.desk_mails().filter(thread=thread).exists():
            raise Http404
        out["thread"] = thread
        team, local = desk_chat.team_of_thread(thread)
        if team:
            out.setdefault("team", team)
        if local:
            out.setdefault("persona", local)
    return {k: v for k, v in out.items() if v}


def _mine(request, cid):
    return get_object_or_404(DeskChat, pk=cid, user=request.user)


def _status():
    if not ai.enabled():
        return "AI analysis is off (Settings → AI analysis)."
    if not desk_chat.enabled():
        return "ChatSIMEX is off (Settings → AI analysis)."
    if not ai.under_cap():
        return "Today's AI spending cap is reached (Settings → AI analysis)."
    return ""


@desk_or_excon
def chats(request):
    """GET: your chats, newest first; POST: a new (empty) chat with a scope."""
    desk_chat.expire()
    if request.method == "POST":
        sc = _scope(request.POST)
        c = DeskChat.objects.create(user=request.user, team_id=sc.get("team"), persona=sc.get("persona", ""),
                                    thread=sc.get("thread", ""))
        return JsonResponse({"chat": desk_chat.chat_json(c)})
    rows = DeskChat.objects.filter(user=request.user)[:50]
    teams = [{"id": t.id, "name": t.name, "test": t.is_test} for t in Team.objects.filter(active=True).order_by("sort", "id")]
    return JsonResponse({"chats": [desk_chat.chat_json(c, turns=False) for c in rows], "teams": teams,
                         "off": _status() if not desk_chat.enabled() else "", "name": desk_chat.NAME})


@desk_or_excon
def chat(request, cid):
    desk_chat.expire()
    return JsonResponse({"chat": desk_chat.chat_json(_mine(request, cid))})


@desk_or_excon
@require_POST
def ask(request, cid):
    """A question in one of your chats (with its scope): starts a turn."""
    c = _mine(request, cid)
    question = (request.POST.get("question") or "").strip()
    if not question:
        return JsonResponse({"error": "Ask something."}, status=400)
    err = _status()
    if err:
        return JsonResponse({"error": err}, status=400)
    desk_chat.expire()
    if desk_chat.running(request.user):
        return JsonResponse({"error": "SIMEX AI is still answering your last question."}, status=409)
    sc = _scope(request.POST)
    c.team_id, c.persona, c.thread = sc.get("team"), sc.get("persona", ""), sc.get("thread", "")
    c.save(update_fields=["team", "persona", "thread"])
    t = desk_chat.start(c, question, sc)
    audit("chat", f"#{c.id} turn {t.id}", _who(request))
    return JsonResponse({"turn": desk_chat.turn_json(t)})


@desk_or_excon
def turn(request, tid):
    from .models import DeskChatTurn
    t = get_object_or_404(DeskChatTurn, pk=tid, chat__user=request.user)
    desk_chat.expire()
    t.refresh_from_db()
    return JsonResponse({"turn": desk_chat.turn_json(t)})


@desk_or_excon
@require_POST
def delete(request, cid):
    _mine(request, cid).delete()
    return JsonResponse({"ok": True})

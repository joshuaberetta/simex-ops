"""SIMEX Messenger's pages (ops/messenger.py): its own home-screen app at /chat/ (manifest,
service worker and push subscriptions of its own, so on a phone it is an app beside SIMEX Mail),
a team leader's read-only view of their team's chats (/chat/team/) and ExCon's (/chats/).

The app is one page (templates/ops/chat/app.html, static/ops/messenger.js) over JSON: the room
list, a room's posts, posting, and /chat/poll/ (a 204 when nothing changed)."""

import re
from functools import wraps

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_POST

from . import conf, push, richtext, signin
from . import messenger as mx
from . import messenger_desk
from .access import desk_of, excon_required, is_excon, leader_of, leader_required, login_required_any, participant_of, rp_of
from .models import Post, PostAttachment, PushSubscription, Room, Team
from .views_inbox import PUSH_HOSTS


def _fetch(request):
    return request.headers.get("x-requested-with") == "fetch"


def chat_view(mode=""):
    """A Messenger view for a member or leader ("") or a leader's read-only team view ("team")."""
    def deco(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not mx.enabled():
                raise Http404("SIMEX Messenger is off.")
            if mode == "" and not (participant_of(request.user) or leader_of(request.user)):
                if is_excon(request.user):
                    return redirect("/chats/")
                if desk_of(request.user):
                    return redirect("/inbox/desk/")
                if rp_of(request.user):
                    return redirect("/inbox/rp/")
                raise PermissionDenied("SIMEX Messenger is for the teams and their leaders.")
            if mode == "desk" and not (desk_of(request.user) or is_excon(request.user)):
                raise PermissionDenied("Persona desk only.")
            v = mx.ChatViewer(request.user, mode)
            if request.method == "POST" and v.readonly:
                raise PermissionDenied("Read-only view.")
            return view(request, v, *args, **kwargs)
        wrapper = login_required_any(wrapper)
        return leader_required(wrapper) if mode == "team" else wrapper
    return deco


def _err(e, status=400):
    return JsonResponse({"error": str(e)}, status=status)


def _room(v, rid):
    room = mx.get_room(v, rid)
    if room is None:
        raise Http404
    return room


# --- the app ------------------------------------------------------------------------------------------

def _config(request, v, room_id=None):
    me = mx.Names(v).name(v.key) if v.key else "ExCon"
    if v.desk:
        me = (v.person.name if v.person else "ExCon") + " · persona desk"
    return {"base": v.base, "readonly": v.readonly, "me": {"key": v.key, "name": me}, "room": room_id, "desk": v.desk,
            "simOnly": v.sim_only, "emoji": mx.EMOJI, "editMinutes": int(conf.get("chat_edit_minutes") or 15),
            "maxMb": int(conf.get("max_upload_mb") or 10), "vapid": push.public_key() if not (v.readonly or v.desk) else "",
            "team": v.team.name if v.team else "", "canGroup": bool(not v.readonly and not v.desk and
                                                                    (v.leader or conf.flag("chat_groups"))),
            "mailUrl": "/inbox/desk/" if v.desk else "/inbox/", "sw": "/chat/sw.js",
            "sections": dict(messenger_desk.SECTIONS) if v.desk else {}}


def _page(request, v, room_id=None):
    if room_id is not None and mx.get_room(v, room_id) is None:
        raise Http404
    if not v.readonly and not v.desk:
        mx.team_room(v.team)
        mx.forget(v)
    cfg = _config(request, v, room_id)
    return render(request, "ops/chat/app.html", {"cfg": cfg, "cv": v, "me_name": cfg["me"]["name"], "team": v.team})


@chat_view()
def app(request, v):
    return _page(request, v)


@chat_view()
def app_room(request, v, rid):
    return _page(request, v, rid)


@chat_view("team")
def team_app(request, v):
    return _page(request, v)


@chat_view("team")
def team_app_room(request, v, rid):
    return _page(request, v, rid)


def _rooms(request, v):
    rows = mx._lister(v)(v)
    return JsonResponse({"rooms": rows, "c": mx.head(), "unread": mx.unread_total(v)})


def _posts(request, v, rid):
    room = _room(v, rid)
    before = int(request.GET["before"]) if (request.GET.get("before") or "").isdigit() else None
    names = mx.Names(v)
    posts, more = mx.page(v, room, before, names)
    mems = mx.members(room, names, current=not v.readonly)
    out = {"room": {"id": room.id, "kind": room.kind, "name": mx.title_of(v, room, names), "frozen": room.frozen,
                    "members": mems, "canPost": mx.can_post(v, room),
                    "admin": any(m["key"] == v.key and m["admin"] and not m["left"] for m in mems),
                    "muted": False, "pinned": False}, "posts": posts, "more": more}
    if v.person and not v.readonly and not v.desk:
        st = room.states.filter(person=v.person).first()
        out["room"].update(muted=bool(st and st.muted), pinned=bool(st and st.pinned))
    if v.desk:  # the personas it can post as, and the desk's state of the room
        from . import messenger_desk as md
        st = md.states([room.id], request.user).get(room.id, {})
        out["room"].update(personas=md.persona_names(room), section=st.get("section"), owner=st.get("owner", ""),
                           mine=st.get("mine", False), npc=st.get("npc"), admin=False,
                           addressed=_addressed(room))
    return JsonResponse(out)


def _addressed(room):
    """The persona the room's latest team turn was for (the composer's default), or ""."""
    from .models import Response
    r = (Response.objects.filter(channel="chat", headers__room=room.id).exclude(persona__isnull=True)
         .exclude(persona="").order_by("-id").first())
    return r.persona if r else ""


def _poll(request, v):
    c = int(request.GET["c"]) if (request.GET.get("c") or "").isdigit() else 0
    rid = int(request.GET["r"]) if (request.GET.get("r") or "").isdigit() else None
    room = mx.get_room(v, rid) if rid else None
    out = mx.poll(v, c, room)
    if out is None:
        return HttpResponse(status=204)
    return JsonResponse(out)


def _file(request, v, fid):
    a = get_object_or_404(PostAttachment.objects.select_related("post", "post__room", "attachment"), pk=fid)
    if mx.get_room(v, a.post.room_id) is None or (a.post.deleted_at and not v.sees_history):
        raise Http404
    start = mx.joined_from(v, a.post.room)
    if start is None or a.post_id <= start:
        raise Http404
    data = a.content()
    if data is None:
        raise Http404
    shown = richtext.image_type(data) if request.GET.get("inline") else None
    if request.GET.get("inline") and not shown:
        raise Http404
    resp = HttpResponse(data, content_type=shown or "application/octet-stream")
    resp["Content-Disposition"] = content_disposition_header(not shown, re.sub(r"[\r\n]+", " ", a.filename))
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Content-Security-Policy"] = "default-src 'none'; sandbox"
    resp["Cache-Control"] = "private, max-age=31536000, immutable"  # a posted file never changes
    if not shown and not v.readonly and not v.desk:
        mx.track(v, "chat_download", a.post.room, file=a.filename[:120])
    return resp


@chat_view()
def rooms(request, v):
    return _rooms(request, v)


@chat_view()
def posts(request, v, rid):
    return _posts(request, v, rid)


@chat_view()
def poll(request, v):
    return _poll(request, v)


@chat_view()
def file_get(request, v, fid):
    return _file(request, v, fid)


@chat_view("team")
def team_rooms(request, v):
    return _rooms(request, v)


@chat_view("team")
def team_posts(request, v, rid):
    return _posts(request, v, rid)


@chat_view("team")
def team_poll(request, v):
    return _poll(request, v)


@chat_view("team")
def team_file(request, v, fid):
    return _file(request, v, fid)


@chat_view()
def book(request, v):
    return JsonResponse({"book": mx.book(v)})


@chat_view()
@require_POST
def new_room(request, v):
    P = request.POST
    keys = [k for k in P.getlist("keys") if k]
    try:
        if len(keys) == 1 and not (P.get("title") or "").strip():
            room, _ = mx.direct(v, keys[0])
        else:
            room = mx.create_group(v, P.get("title"), keys)
    except mx.ChatError as e:
        return _err(e)
    return JsonResponse({"id": room.id})


@chat_view()
@require_POST
def send(request, v, rid):
    room = _room(v, rid)
    P = request.POST
    files = [(f.name, f.content_type, f.read()) for f in request.FILES.getlist("files")][:10]
    reply = int(P["reply_to"]) if (P.get("reply_to") or "").isdigit() else None
    try:
        p, _ = mx.post(v, room, P.get("body"), files, reply, P.getlist("mentions"), P.get("client_key", ""))
    except mx.ChatError as e:
        return _err(e)
    names = mx.Names(v)
    return JsonResponse({"post": mx.post_json(v, p, names, {})})


@chat_view()
@require_POST
def read(request, v, rid):
    room = _room(v, rid)
    mx.mark_read(v, room, int(request.POST["upto"]) if (request.POST.get("upto") or "").isdigit() else 0)
    return JsonResponse({"ok": True})


@chat_view()
@require_POST
def typing(request, v, rid):
    mx.typing(v, _room(v, rid))
    return JsonResponse({"ok": True})


@chat_view()
@require_POST
def room_settings(request, v, rid):
    room = _room(v, rid)
    P = request.POST
    try:
        mx.settings_action(v, room, P.get("act", ""), [k for k in P.getlist("keys") if k], P.get("title", ""))
    except mx.ChatError as e:
        return _err(e)
    return JsonResponse({"ok": True})


def _own_post(v, pid):
    p = get_object_or_404(Post.objects.select_related("room"), pk=pid)
    if mx.get_room(v, p.room_id) is None:
        raise Http404
    start = mx.joined_from(v, p.room)
    if start is None or p.id <= start:
        raise Http404
    return p


@chat_view()
@require_POST
def post_action(request, v, pid, act):
    p = _own_post(v, pid)
    try:
        if act == "edit":
            mx.edit(v, p, request.POST.get("body"))
        elif act == "delete":
            mx.delete(v, p)
        elif act == "react":
            mx.react(v, p, request.POST.get("emoji", ""))
        else:
            raise Http404
    except mx.ChatError as e:
        return _err(e)
    p = Post.objects.get(pk=p.pk)
    return JsonResponse({"post": mx.post_json(v, p, mx.Names(v), mx.receipts(v, p.room))})


@chat_view()
@require_POST
def push_subscribe(request, v):
    P = request.POST
    endpoint = (P.get("endpoint") or "").strip()
    host = endpoint.split("://", 1)[-1].split("/")[0].lower()
    if not endpoint.startswith("https://") or not any(host == h.lstrip(".") or host.endswith(h) for h in PUSH_HOSTS):
        return _err("Unknown push service.")
    if P.get("off"):
        PushSubscription.objects.filter(endpoint=endpoint, person=v.person).delete()
        return JsonResponse({"ok": True})
    if not (P.get("p256dh") and P.get("auth")):
        return _err("Incomplete subscription.")
    PushSubscription.objects.update_or_create(endpoint=endpoint, defaults={
        "person": v.person, "p256dh": P["p256dh"][:200], "auth": P["auth"][:100], "app": "chat",
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:400]})
    return JsonResponse({"ok": True})


def service_worker(request):
    """Messenger's service worker (scope /chat/): pushes, and the app's own files kept on the
    device (cache first: they are versioned by name), so it opens at once."""
    from django.templatetags.static import static
    assets = [static(f) for f in ("ops/messenger.css", "ops/messenger.js", "ops/chat-192.png", "ops/apps.css",
                                  "ops/whatsnew.css", "ops/whatsnew.js", "ops/apps.js")]
    import hashlib
    version = hashlib.sha1("|".join(assets).encode()).hexdigest()[:12]  # hashed names: a new release, a new cache
    resp = render(request, "ops/chat/sw.js", {"icon": static("ops/chat-192.png"), "assets": assets, "version": version},
                  content_type="application/javascript")
    resp["Cache-Control"] = "no-cache"
    resp["Service-Worker-Allowed"] = "/chat/"
    return resp


def manifest(request):
    """Messenger's own web app manifest: added to the home screen, it is an app of its own."""
    from django.templatetags.static import static
    icons = [{"src": static(f"ops/chat-{n}.png"), "sizes": f"{n}x{n}", "type": "image/png", "purpose": "any"}
             for n in (192, 512)]
    icons.append({"src": static("ops/chat-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"})
    start = signin.path(request.user) + "?app=chat" if signin.can_use(request.user) else "/chat/"
    resp = JsonResponse({"name": "SIMEX Messenger", "short_name": "Messenger", "id": "/chat/", "start_url": start,
                         "scope": "/", "display": "standalone", "background_color": "#ffffff",
                         "theme_color": "#0f7b5f", "icons": icons}, content_type="application/manifest+json")
    resp["Cache-Control"] = "private, no-cache"
    return resp


# --- the persona desk (mode "desk": every room with a persona, posting as one) ------------------------

@chat_view("desk")
def desk_app(request, v):
    return _page(request, v)


@chat_view("desk")
def desk_app_room(request, v, rid):
    return _page(request, v, rid)


@chat_view("desk")
def desk_rooms(request, v):
    return _rooms(request, v)


@chat_view("desk")
def desk_posts(request, v, rid):
    return _posts(request, v, rid)


@chat_view("desk")
def desk_poll(request, v):
    return _poll(request, v)


@chat_view("desk")
def desk_file(request, v, fid):
    return _file(request, v, fid)


@chat_view("desk")
def desk_book(request, v):
    """Who the desk can open a chat with (teams, members, team leaders) and as which persona."""
    from . import desk
    from .views_inbox import desk_leaders, desk_members
    teams = list(Team.objects.filter(active=True))
    book = [{"key": f"t:{t.id}", "name": t.name, "kind": "team"} for t in teams]
    book += [{"key": e["key"], "name": e["name"], "kind": "member"} for e in desk_members(teams)]
    book += [{"key": e["key"], "name": e["name"], "kind": "tl"} for e in desk_leaders(teams)]
    personas = [{"local": s.local_part, "name": s.display_name or s.local_part, "org": s.organisation,
                 "colour": desk.colour(s)[0]} for s in desk.personas() if "{" not in s.local_part and not s.team_leader]
    return JsonResponse({"book": book, "personas": personas})


@chat_view("desk")
@require_POST
def desk_new(request, v):
    from . import messenger_desk as md
    try:
        room = md.open_room(v, (request.POST.get("persona") or "").strip().lower(), request.POST.get("key", ""),
                            user=request.user)
    except md.PersonaPostError as e:
        return _err(e)
    mx.forget(v)
    return JsonResponse({"id": room.id})


@chat_view("desk")
@require_POST
def desk_send(request, v, rid):
    from . import desk
    from . import messenger_desk as md
    room = _room(v, rid)
    P = request.POST
    files = [(f.name, f.content_type, f.read()) for f in request.FILES.getlist("files")][:10]
    reply = int(P["reply_to"]) if (P.get("reply_to") or "").isdigit() else None
    if not room.is_test and not conf.flag("live_armed") and not _team_wrote(room):
        return _err("Live sending isn't armed (Settings): only the Test team can be written to first.")
    try:
        p = md.post_as(room, (P.get("persona") or "").strip().lower(), P.get("body"), files, reply,
                       P.get("client_key", ""), user=request.user, person=v.person)
    except md.PersonaPostError as e:
        return _err(e)
    desk.mark([md.thread(room)], "take", request.user, desk.user_name(request.user)) if P.get("take") == "1" else None
    return JsonResponse({"post": mx.post_json(v, p, mx.Names(v), {})})


def _team_wrote(room):
    return Post.objects.filter(room=room, author__isnull=False).exists()


@chat_view("desk")
@require_POST
def desk_mark(request, v, rid):
    """take | release | done | reopen | watch | unwatch, through desk.mark (DeskConversation chat:<id>)."""
    from . import desk
    from . import messenger_desk as md
    room = _room(v, rid)
    act = request.POST.get("act", "")
    if act not in ("take", "release", "done", "reopen", "watch", "unwatch", "assign"):
        return _err("Unknown action.")
    try:
        desk.mark([md.thread(room)], act, request.user, desk.user_name(request.user), assignee=request.POST.get("to"))
    except desk.DeskError as e:
        return _err(e)
    mx.change(room, "room")
    return JsonResponse({"ok": True})


# --- ExCon -------------------------------------------------------------------------------------------

@excon_required
def excon_list(request):
    """Team inboxes → Messenger: every room, filtered by team and kind, searched."""
    v = mx.ChatViewer(request.user, "excon")
    teams = list(Team.objects.order_by("name"))
    G = request.GET
    team = next((t for t in teams if str(t.id) == G.get("team", "")), None)
    kind = G.get("kind") if G.get("kind") in ("team", "direct", "group") else ""
    q = (G.get("q") or "").strip()[:100]
    qs = Room.objects.all()
    if team:
        qs = qs.filter(members__team=team).distinct()
    if kind:
        qs = qs.filter(kind=kind)
    if G.get("persona") == "1":
        qs = qs.filter(has_persona=True)
    if G.get("test") != "1":
        qs = qs.filter(is_test=False)
    if q:
        qs = qs.filter(posts__body__icontains=q).distinct()
    ids = set(qs.values_list("id", flat=True)[:500])
    names = mx.Names(v)
    rows = mx.room_list(v, names, only=ids) if ids else []
    tnames = {t.id: t.name for t in teams}
    for r in rows:
        r["team_names"] = ", ".join(tnames.get(t, "") for t in r["teams"])
    return render(request, "ops/chats.html", {"tab": "inboxes", "rows": rows, "teams": teams, "team": team, "kind": kind,
                                              "q": q, "test": G.get("test") == "1", "persona": G.get("persona") == "1",
                                              "on": mx.enabled()})


@excon_required
def excon_room(request, rid):
    """One room, read-only: every post since it began, edits and deleted text, members (who left)."""
    v = mx.ChatViewer(request.user, "excon")
    room = get_object_or_404(Room, pk=rid)
    names = mx.Names(v)
    posts = list(mx._with(Post.objects.filter(room=room)).order_by("id"))
    names.load({mx.post_key(p) for p in posts if mx.post_key(p)})
    rows = [mx.post_json(v, p, names) for p in posts]
    if request.GET.get("format") == "md":
        return _md(room, rows, names, v)
    return render(request, "ops/chat_room.html", {"tab": "inboxes", "room": room, "posts": rows,
                                                  "title": mx.title_of(v, room, names),
                                                  "members": mx.members(room, names, current=False)})


def _md(room, rows, names, v):
    lines = [f"# {mx.title_of(v, room, names)}", ""]
    for r in rows:
        when = f"{r['day']} {r['t']}" + (f" (scenario {r['sim']})" if r.get("sim") else "")
        if r["kind"] == "system":
            lines.append(f"*{when} · {r['body']}*")
            continue
        flag = " [deleted by " + r.get("deleted_by", "") + "]" if r["deleted"] else ""
        flag += " [edited]" if r["edited"] else ""
        lines.append(f"**{r['name']}** · {when}{flag}  ")
        lines.append(r["body"] or "")
        for f in r.get("files", []):
            lines.append(f"📎 {f['name']}")
        for e in r.get("edits", []):
            lines.append(f"> before the edit at {e['at']}: {e['body']}")
        lines.append("")
    resp = HttpResponse("\n".join(lines), content_type="text/markdown; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="chat-{room.id}.md"'
    return resp


@excon_required
@require_POST
def excon_freeze(request, rid):
    room = get_object_or_404(Room, pk=rid)
    room.frozen = request.POST.get("on") == "1"
    room.save(update_fields=["frozen"])
    mx.system(room, "ExCon froze this chat: nobody can write in it" if room.frozen else "ExCon opened this chat again")
    mx.change(room, "room")
    messages.success(request, "Frozen." if room.frozen else "Open again.")
    return redirect(f"/chats/{room.id}/")


@excon_required
@require_POST
def excon_delete(request, pid):
    p = get_object_or_404(Post, pk=pid)
    mx.delete(None, p, by_excon=request.user.get_username())
    messages.success(request, "Deleted for everyone (the text stays here).")
    return redirect(f"/chats/{p.room_id}/#p{p.id}")


@excon_required
def excon_file(request, fid):
    a = get_object_or_404(PostAttachment, pk=fid)
    data = a.content()
    if data is None:
        raise Http404
    shown = richtext.image_type(data) if request.GET.get("inline") else None
    resp = HttpResponse(data, content_type=shown or "application/octet-stream")
    resp["Content-Disposition"] = content_disposition_header(not shown, re.sub(r"[\r\n]+", " ", a.filename))
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Content-Security-Policy"] = "default-src 'none'; sandbox"
    resp["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


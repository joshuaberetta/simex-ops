"""Files (ops/file_index.py): every file of the exercise in one list, for the persona desk
(/inbox/desk/files/), a team leader (/inbox/files/, their team only) and ExCon (/files/).
Downloading from here never records an Event: it is a monitoring view, not a team's mailbox."""

import re

from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import render
from django.utils.http import content_disposition_header

from . import file_index as fx
from . import richtext
from .access import desk_of, excon_required, leader_of, leader_required
from .models import Inject, Team
from .views_inbox import DESK, _desk_page, box_theme, desk_tabs, leader_tabs


def _context(request, who, team=None):
    teams = [team] if team else list(Team.objects.order_by("name"))
    f = fx.filters(request.GET, teams if not team else [])
    found = fx.rows(who, f, team=team)
    rows, more = fx.page(found, f)
    injects = sorted({(r["inject_id"], r["inject"]) for r in found if r["inject_id"]}, key=lambda x: x[1])
    return f, found, {
        "f": f, "groups": fx.grouped(rows, f.group), "total": len(found), "more": more,
        "total_size": sum(r["size"] or 0 for r in found), "teams": [] if team else teams, "who": who,
        "senders": fx.senders(found), "injects": injects, "DIRECTIONS": fx.DIRECTIONS, "SOURCES": fx.SOURCES,
        "TYPES": fx.TYPES, "GROUPS": fx.GROUPS, "AI_STATES": fx.AI_STATES, "sees_ai": who != "desk",
        "file_base": {"desk": "/inbox/desk/files", "leader": "/inbox/files", "excon": "/files/get"}[who],
    }


def _csv(found, who, name):
    resp = HttpResponse(fx.export_csv(found, who), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    return resp


def _js(base, team="", leader=False):
    return {"base": base, "readonly": True, "book": [], "team": team, "leader": leader, "folder": "", "unread": 0,
            "vapid": "", "pushBase": base, "labels": []}


@_desk_page
def desk_files(request):
    person = desk_of(request.user)
    f, found, ctx = _context(request, "desk")
    if request.GET.get("format") == "csv":
        return _csv(found, "desk", "files")
    js = _js("/inbox/desk")
    js["team"] = DESK["name"]
    return render(request, "ops/mail/files.html", {
        **ctx, "team": DESK, "me": person, "base": "/inbox/desk", "readonly": True, "v": None,
        "desk_tabs": desk_tabs(person, "files", request.user),
        "box": box_theme(kind="desk", title="Persona desk", sub="Files"), "simex_js": js})


@leader_required
def leader_files(request):
    lead = leader_of(request.user)
    f, found, ctx = _context(request, "leader", team=lead.team)
    if request.GET.get("format") == "csv":
        return _csv(found, "leader", f"{lead.team.code}-files")
    return render(request, "ops/mail/files.html", {
        **ctx, "team": lead.team, "me": lead, "base": "/inbox", "readonly": True,
        "leader_tabs": leader_tabs(lead, "files"),
        "box": box_theme(kind="leader", title="Team leader", sub=f"{lead.name} · {lead.team.name} · Files"),
        "simex_js": _js("/inbox", lead.team.name, True)})


@excon_required
def excon_files(request):
    f, found, ctx = _context(request, "excon")
    fmt = request.GET.get("format")
    if fmt == "csv":
        return _csv(found, "excon", "files")
    if fmt == "zip":
        resp = StreamingHttpResponse(fx.zip_stream(found), content_type="application/zip")
        resp["Content-Disposition"] = 'attachment; filename="files.zip"'
        return resp
    return render(request, "ops/files.html", {**ctx, "tab": "inboxes",
                                              "inject_names": dict(Inject.objects.values_list("id", "mel_id"))})


def _serve(request, who, team, src, fid):
    a = fx.visible(who, team, src, fid)
    data = fx.content(src, a) if a else None
    if data is None:
        raise Http404
    name = re.sub(r"[\r\n]+", " ", a.filename or "file")
    shown = richtext.image_type(data) if request.GET.get("inline") else None
    if request.GET.get("inline") and not shown:
        raise Http404  # only real pictures are ever shown in the page
    resp = HttpResponse(data, content_type=shown or "application/octet-stream")
    resp["Content-Disposition"] = content_disposition_header(not shown, name)
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Content-Security-Policy"] = "default-src 'none'; sandbox"
    resp["Cache-Control"] = "private, max-age=86400"  # a stored file never changes
    return resp


@_desk_page
def desk_file(request, src, fid):
    return _serve(request, "desk", None, src, fid)


@leader_required
def leader_file(request, src, fid):
    return _serve(request, "leader", leader_of(request.user).team, src, fid)


@excon_required
def excon_file(request, src, fid):
    return _serve(request, "excon", None, src, fid)

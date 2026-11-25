"""Notes on mail (ops/mail_notes.py): the side panel of a conversation (its JSON and the
notes written there, also in the read-only mailboxes, as a note changes no one's mail) and the
lists of notes: the persona desk's Notes tab, a team leader's, and ExCon's (from Team inboxes).
Only the desk, team leaders and ExCon reach any of it (mail_notes.access)."""

import csv

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils import timezone

from . import mail_notes
from .access import desk_of, excon_required, leader_of, leader_required, login_required_any
from .models import MailNote, Team
from .views_inbox import DESK, Viewer, _desk_page, box_theme, desk_tabs, leader_tabs

LIST_MAX = 500  # rows on a list page (the CSV has them all)


def notes_view(view):
    """A conversation's notes in a mailbox: the Viewer as for the mailbox, but a note may be
    written in the read-only ones (the team inbox and members' mailboxes for their leader,
    a team's for ExCon), and only by those mail_notes.access lets in."""
    @login_required_any
    def wrapper(request, *args, as_team=None, box="", pid=None, **kwargs):
        v = Viewer(request, as_team, box, pid)
        acc = mail_notes.access(v)
        if acc is None:
            raise PermissionDenied("Notes are for the persona desk, team leaders and ExCon.")
        return view(request, v, acc, *args, **kwargs)
    wrapper.__name__ = view.__name__
    return wrapper


def _visible(v, thread):
    return mail_notes.box_mails(v.team.id if v.team else None, v.owner.id if v.owner else None, thread)


def panel_html(request, v, acc, thread, mails):
    p = mail_notes.panel(v, acc, thread, mails)
    return render_to_string("ops/mail/_notes.html", {"np": p, "base": v.base, "thread": thread}, request=request), p


def _answer(request, v, acc, thread, error=""):
    mails = _visible(v, thread)
    if not mails:
        raise Http404
    html, p = panel_html(request, v, acc, thread, mails)
    return JsonResponse({"html": html, "pending": p["pending"], "n": p["n_notes"] + p["n_ai"], "error": error},
                        status=400 if error else 200)


@notes_view
def thread_notes(request, v, acc, thread):
    """GET: the panel (polled while SIMEX AI reads). POST act: add (mail, body), edit (note,
    body), delete (note), review (read the conversation again)."""
    if request.method != "POST":
        if request.GET.get("format") == "md":
            mails = _visible(v, thread)
            if not mails:
                raise Http404
            return _md(mail_notes.thread_markdown(v, acc, thread, mails), f"conversation-{thread[:8]}.md")
        return _answer(request, v, acc, thread)
    P, act = request.POST, request.POST.get("act", "")
    team_id, owner_id = (v.team.id if v.team else None), (v.owner.id if v.owner else None)
    if act == "add":
        mail = get_object_or_404(v.mails().exclude(folder="drafts"), pk=P.get("mail") or 0, thread=thread)
        body = (P.get("body") or "").replace("\r\n", "\n").strip()
        if not body:
            return _answer(request, v, acc, thread, "Write the note first.")
        MailNote.objects.create(scope=acc.write, team_id=team_id, owner_id=owner_id, thread=thread, mail=mail,
                                about_team_id=mail.desk_team_id if team_id is None else team_id, user=request.user,
                                author_name=mail_notes.author_name(request.user), body=body[:mail_notes.NOTE_MAX])
    elif act in ("edit", "delete"):
        note = get_object_or_404(MailNote, pk=P.get("note") or 0, scope__in=acc.read, team_id=team_id, owner_id=owner_id,
                                 thread=thread)
        if act == "delete":
            if not acc.can_delete(note):
                raise PermissionDenied("Only who wrote a note (or ExCon) can delete it.")
            note.delete()
        else:
            if not acc.can_edit(note):
                raise PermissionDenied("Only who wrote a note can change it.")
            body = (P.get("body") or "").replace("\r\n", "\n").strip()
            if not body:
                return _answer(request, v, acc, thread, "A note can't be empty: delete it instead.")
            note.body, note.edited_at = body[:mail_notes.NOTE_MAX], timezone.now()
            note.save(update_fields=["body", "edited_at"])
    elif act == "review":
        if not mail_notes.enabled():
            return _answer(request, v, acc, thread, "SIMEX AI's reading of conversations is off (Settings → AI analysis).")
        mail_notes.ensure(team_id, owner_id, thread, _visible(v, thread), force=True)
    else:
        return _answer(request, v, acc, thread, "Unknown action.")
    return _answer(request, v, acc, thread)


# --- the lists ---------------------------------------------------------------------------------------

def _filters(request, teams):
    G = request.GET
    team = next((t for t in teams if str(t.id) == G.get("team", "")), None)
    what = G.get("what") if G.get("what") in ("notes", "ai") else "all"
    verdict = G.get("verdict") if G.get("verdict") in mail_notes.VERDICTS else ""
    return {"team": team, "what": what, "verdict": verdict, "q": (G.get("q") or "").strip()[:100],
            "mine": G.get("by") == "me", "scope": G.get("scope") if G.get("scope") in mail_notes.SCOPES else ""}


def _md(text, name):
    resp = HttpResponse(text, content_type="text/markdown; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


def _export(request, rows, name, title):
    """?format=csv | md: the whole list (filters kept) as a file."""
    fmt = request.GET.get("format")
    if fmt == "csv":
        return _csv(rows, name + ".csv")
    if fmt == "md":
        return _md(mail_notes.list_markdown(rows, title), name + ".md")
    return None


def _csv(rows, name):
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    w = csv.writer(resp)
    w.writerow(["When", "Kind", "By", "Team", "Mailbox", "Conversation", "Email from", "Email sent", "Verdict", "Issues",
                "Text"])
    for e in rows:
        w.writerow([timezone.localtime(e["at"]).strftime("%Y-%m-%d %H:%M"), "Note" if e["kind"] == "note" else "SIMEX AI",
                    e["by"], e["team"], e["box"], e["subject"], e["from"],
                    timezone.localtime(e["mail_at"]).strftime("%Y-%m-%d %H:%M"), e.get("verdict_name", ""),
                    "; ".join(e["issues"]), e["text"]])
    return resp


def _rows(request, who, f, team=None):
    rows = mail_notes.entries(who, team=team if who == "leader" else f["team"],
                              scopes=[f["scope"]] if f["scope"] else None, what=f["what"], verdict=f["verdict"], q=f["q"],
                              mine=request.user if f["mine"] else None)
    return rows


def _mail_js(base):
    return {"base": base, "readonly": True, "book": [], "team": "", "leader": False, "folder": "", "unread": 0,
            "vapid": "", "pushBase": base, "labels": []}


@_desk_page
def desk_notes(request):
    """The persona desk's Notes tab: every note written on the desk, with SIMEX AI's
    assessment of each email the teams wrote to the personas."""
    person = desk_of(request.user)
    teams = list(Team.objects.filter(active=True))
    f = _filters(request, teams)
    rows = _rows(request, "desk", f)
    out = _export(request, rows, "desk-notes", "Persona desk: notes on the mail")
    if out:
        return out
    js = _mail_js("/inbox/desk")
    js["team"] = DESK["name"]
    return render(request, "ops/mail/notes.html", {
        "team": DESK, "me": person, "base": "/inbox/desk", "readonly": True, "v": None, "back_url": "/inbox/desk/",
        "desk_tabs": desk_tabs(person, "notes", request.user),
        "box": box_theme(kind="desk", title="Persona desk", sub="Notes on the mail"),
        "rows": rows[:LIST_MAX], "total": len(rows), "f": f, "teams": teams, "who": "desk", "ai_on": mail_notes.enabled(),
        "simex_js": js})


@leader_required
def leader_notes(request):
    """A team leader's Notes tab: their notes on their team's mail (their own mailbox, the
    team inbox, their members'), with SIMEX AI's assessment of each email the team wrote."""
    lead = leader_of(request.user)
    team = lead.team
    f = _filters(request, [])
    rows = _rows(request, "leader", f, team=team)
    out = _export(request, rows, f"{team.code}-notes", f"{team.name}: notes on the mail")
    if out:
        return out
    js = _mail_js("/inbox")
    js.update(team=team.name, leader=True)
    tabs = leader_tabs(lead, "notes")
    return render(request, "ops/mail/notes.html", {
        "team": team, "me": lead, "base": "/inbox", "readonly": True, "leader_tabs": tabs,
        "box": box_theme(kind="leader", title="Team leader", sub=f"{lead.name} · {team.name} · Notes"),
        "rows": rows[:LIST_MAX], "total": len(rows), "f": f, "teams": [], "who": "leader", "ai_on": mail_notes.enabled(),
        "simex_js": js})


@excon_required
def excon_notes(request):
    """ExCon's list (from Team inboxes): every note (the desk's, the team leaders', ExCon's)
    and every SIMEX AI assessment, filtered by team, kind and author."""
    teams = list(Team.objects.all())
    f = _filters(request, teams)
    rows = _rows(request, "excon", f)
    out = _export(request, rows, "notes", "Notes on the mail" + (f": {f['team'].name}" if f["team"] else ""))
    if out:
        return out
    return render(request, "ops/notes.html", {
        "tab": "inboxes", "rows": rows[:LIST_MAX], "total": len(rows), "f": f, "teams": teams,
        "scopes": mail_notes.SCOPES, "verdicts": mail_notes.VERDICTS, "ai_on": mail_notes.enabled()})


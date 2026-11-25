"""The persona desk's Injects tab (/inbox/desk/injects/): the injects, read-only, so whoever plays
the personas knows what is coming, what each says and who gets it. Desk members and ExCon
(_desk_page); Settings desk_injects turns it off. Never ExCon's checks, AI reviews, errors,
rubrics or grades, and never the recipients' email addresses."""

from datetime import timedelta

from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from . import conf, desk
from . import render as rnd
from .access import desk_of
from .models import DELIVERY_MODES, Attachment, Inject, Mail, Message, Team
from .views_excon import _download, local_day
from .views_inbox import DESK, _desk_page, box_theme, desk_tabs

UPCOMING = ("scheduled", "sending", "overdue", "failed")  # not gone yet
SENT = ("sent", "partial")
SHOWS = {"upcoming": "Coming up", "sent": "Sent", "all": "All"}
STATUS_LABELS = {"draft": "Not scheduled", "scheduled": "Scheduled", "sending": "Sending", "sent": "Sent",
                 "partial": "Partly sent", "failed": "Not sent (ExCon is on it)", "overdue": "Overdue (held by ExCon)",
                 "cancelled": "Cancelled"}
KINDS = {"live": "Inject", "followup": "Follow-up", "test": "Test", "oneoff": "Test"}
WHO = {"each_team": "each team's inbox", "each_person": "each participant", "all_bcc": "everyone",
       "custom": "the addresses listed"}


def enabled():
    return conf.flag("desk_injects")


def _page(request, sub, **ctx):
    if not enabled():
        raise Http404
    person = desk_of(request.user)
    return {"team": DESK, "me": person, "base": "/inbox/desk", "readonly": True, "v": None, "back_url": "/inbox/desk/",
            "desk_tabs": desk_tabs(person, "injects", request.user),
            "box": box_theme(kind="desk", title="Persona desk", sub=sub),
            "simex_js": {"base": "/inbox/desk", "readonly": True, "book": [], "team": DESK["name"], "leader": False,
                         "folder": "", "unread": 0, "vapid": "", "pushBase": "/inbox/desk", "labels": []}, **ctx}


def _when(dt, now):
    """"in 25 min" / "3 h ago" for a send time."""
    if not dt:
        return ""
    mins = round((dt - now).total_seconds() / 60)
    n, unit = (abs(mins), "min") if abs(mins) < 120 else (round(abs(mins) / 60), "h")
    return "now" if mins == 0 else f"in {n} {unit}" if mins > 0 else f"{n} {unit} ago"


def recipients(inj, teams=None):
    """Who an inject goes to, in words: its teams (all, or those named), how, and the leaders."""
    teams = rnd.scope_teams(inj) if teams is None else teams
    every = not inj.team_ids
    out = f"{'All teams' if every else ', '.join(t.name for t in teams) or 'No team'}: {WHO.get(inj.delivery, inj.delivery)}"
    if inj.to_leaders:
        out += ", and the team leaders"
    return out


def _annotate(i, now, memo, teams, files):
    i.chip = desk.describe(i.sender_local, None, memo) if i.sender_local else None
    i.status_label = STATUS_LABELS.get(i.status, i.status)
    i.when = _when(i.send_at, now) if i.status in UPCOMING else ""
    i.to_words = recipients(i, [t for t in teams if not i.team_ids or t.id in i.team_ids])
    i.files = files.get(i.id, 0)
    return i


@_desk_page
def injects(request):
    """Every inject by send time, grouped by day: Coming up (not gone yet), Sent, or All (with
    those not scheduled and cancelled); filters by persona, team and words."""
    G = request.GET
    show = G.get("show") if G.get("show") in SHOWS else "upcoming"
    q, persona = (G.get("q") or "").strip(), (G.get("persona") or "").strip()
    try:
        team_id = int(G.get("team") or 0)
    except ValueError:
        team_id = 0
    now = timezone.now()
    rows = list(Inject.objects.all())
    counts = {"upcoming": sum(1 for i in rows if i.status in UPCOMING), "sent": sum(1 for i in rows if i.status in SENT),
              "all": len(rows)}
    personas = sorted({i.sender_local for i in rows if i.sender_local})
    if show == "upcoming":
        rows = [i for i in rows if i.status in UPCOMING]
    elif show == "sent":
        rows = [i for i in rows if i.status in SENT]
    if persona:
        rows = [i for i in rows if i.sender_local == persona]
    if team_id:
        rows = [i for i in rows if not i.team_ids or team_id in i.team_ids]
    if q:
        ql = q.lower()
        rows = [i for i in rows if ql in " ".join(filter(None, [i.mel_id, i.subject, i.body, i.sender_local,
                                                                 i.from_name])).lower()]
    far = now + timedelta(days=36500)
    rows.sort(key=lambda i: (i.send_at is None, i.send_at or far, i.seq), reverse=show == "sent")
    teams = list(Team.objects.filter(active=True, is_test=False))
    files = {}
    for iid in Attachment.objects.filter(inject__in=rows).values_list("inject_id", flat=True):
        files[iid] = files.get(iid, 0) + 1
    memo, groups = {}, []
    nxt = next((i.id for i in sorted(rows, key=lambda i: (i.send_at or far))
                if i.status == "scheduled" and i.send_at and i.send_at >= now), None)
    for i in rows:
        _annotate(i, now, memo, teams, files)
        i.is_next = i.id == nxt
        key = local_day(i.send_at) or "none"
        if not groups or groups[-1]["key"] != key:
            groups.append({"key": key, "label": timezone.localtime(i.send_at).strftime("%A %d %B %Y")
                           if i.send_at else "No send time", "items": []})
        groups[-1]["items"].append(i)
    memo2 = {}
    return render(request, "ops/mail/desk_injects.html", _page(
        request, "Injects · read-only", groups=groups, show=show, shows=SHOWS, counts=counts, q=q,
        persona=persona, team_id=team_id, teams=teams, total=len(rows),
        personas=[desk.describe(p, None, memo2) for p in personas]))


@_desk_page
def inject(request, iid):
    """One inject as the teams get it: from, when, who gets it and the text of each version
    (placeholders filled per team), its files, and where it has gone so far (each delivery
    linked to its conversation on the desk)."""
    inj = get_object_or_404(Inject, pk=iid)
    now = timezone.now()
    teams = list(Team.objects.filter(active=True, is_test=False))
    files = {inj.id: inj.attachments.count()}
    _annotate(inj, now, {}, teams, files)
    drafts, _ = rnd.build(inj, conf.get_all())  # its warnings are ExCon's
    versions = []  # one per distinct text, with who gets it
    for d in drafts:
        key = (d.from_name, d.subject, d.body, d.footer)
        v = next((x for x in versions if x["key"] == key), None)
        if not v:
            v = {"key": key, "from": d.from_name, "subject": d.subject, "body": d.body, "footer": d.footer,
                 "targets": []}
            versions.append(v)
        who = d.target + (" · by email" if d.channel == "email" and inj.effective_channel == "inapp" else "")
        if who not in v["targets"]:
            v["targets"].append(who)
    msgs = list(Message.objects.filter(inject=inj, kind__in=KINDS).select_related("team").order_by("created_at", "id"))
    threads = dict(Mail.objects.filter(team=None, owner=None, folder="sent", message__in=msgs)
                   .values_list("message_id", "thread"))
    for m in msgs:
        m.kind_label = KINDS.get(m.kind, m.kind)
        m.thread = threads.get(m.id)
    return render(request, "ops/mail/desk_inject.html", _page(
        request, f"Inject {inj.mel_id} · read-only", inj=inj, versions=versions, msgs=msgs,
        atts=list(inj.attachments.all()), channel=inj.effective_channel,
        delivery_label=DELIVERY_MODES.get(inj.delivery, inj.delivery)))


@_desk_page
def inject_file(request, iid, aid):
    """One of an inject's files, to the desk."""
    if not enabled():
        raise Http404
    a = get_object_or_404(Attachment, pk=aid, inject_id=iid)
    return _download(a.data, a.filename, a.content_type)

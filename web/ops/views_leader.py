"""A team leader's Dashboard (the Dashboard tab above their SIMEX Mail): their team's replies
and Claude's reading of them. The figures come from views_ai.dashboard_data, as ExCon's
Dashboard, for this team only and without grades. Every view is leader_required; a leader
never sees another team's inbox (only, read-only, the other team leaders' mailboxes)."""

from collections import Counter
from datetime import timedelta

from django.shortcuts import redirect, render
from django.utils import timezone

from . import ai, push
from .access import leader_required, leader_of
from .views_ai import dashboard_data
from .views_inbox import ai_band, leader_tabs, peer_teams


@leader_required
def dashboard(request):
    lead = leader_of(request.user)
    team = lead.team
    data = dashboard_data("test" if team.is_test else "live", team=team)
    tile = data["tiles"][0] if data["tiles"] else None
    reps = data["reps"]
    for r in reps:
        r.band = ai_band(r.ai.ai_written) if r.ai else ""
        r.thread = r.mail.thread if r.mail_id and r.mail else ""
    for r in data["flagged"]:
        r.band = ai_band(r.ai.ai_written)
    by_member = Counter(r.person.name if r.person else (r.from_name or "?") for r in reps)
    # Replies per hour of the exercise, for a small bar strip.
    hours = []
    if reps:
        start = data["t0"].replace(minute=0, second=0, microsecond=0)
        n = int((max(r.received_at for r in reps) - start).total_seconds() // 3600) + 1
        counts = Counter(int((r.received_at - start).total_seconds() // 3600) for r in reps)
        top = max(counts.values())
        hours = [{"at": start + timedelta(hours=i), "n": counts.get(i, 0), "pct": round(100 * counts.get(i, 0) / top)}
                 for i in range(min(n, 72))]
    analysed = [r for r in reps if r.ai]
    likely = sum(1 for r in analysed if (r.ai.ai_written or 0) >= 70)
    possibly = sum(1 for r in analysed if 40 <= (r.ai.ai_written or 0) < 70)
    wpm = [r.ai.wpm for r in analysed if r.ai.wpm]
    ai_list = sorted([r for r in analysed if r.band], key=lambda r: -(r.ai.ai_written or 0))[:30]
    return render(request, "ops/mail/dashboard.html", {
        "team": team, "me": lead, "base": "/inbox", "readonly": True, "leader_tabs": leader_tabs(lead, "dashboard"),
        "tile": tile, "reps": reps, "members": by_member.most_common(), "hours": hours,
        "analysed": len(analysed), "likely": likely, "possibly": possibly,
        "mean_wpm": round(sum(wpm) / len(wpm)) if wpm else None, "ai_list": ai_list,
        "flagged": data["flagged"], "commitments": data["commitments"], "asks": data["asks"],
        "t0": data["t0"], "t1": data["t1"], "ai_enabled": ai.enabled(), "now": timezone.now(),
        "simex_js": {"base": "/inbox", "readonly": True, "book": [], "team": team.name, "leader": True,
                     "folder": "", "unread": 0, "vapid": push.public_key(), "pushBase": "/inbox", "labels": []}})


@leader_required
def peers(request):
    """The Team leaders tab: the first other team leader's mailbox (the others are in its side bar)."""
    first = peer_teams(leader_of(request.user)).first()
    return redirect(f"/inbox/leaders/{first.id}/" if first else "/inbox/")

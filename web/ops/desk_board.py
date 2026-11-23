"""The persona desk's Assign tab (views_desk_board.py, static/ops/desk_board.js): its
conversations as a board, a column for nobody and one for each person on the desk who can have
them (desk.assignees), moved by dragging (desk.mark assign / release, nothing of its own); and
the default assignments of the personas to them, by team (DeskDefault, applied by
desk.auto_assign and desk.apply_defaults). Computed from the desk's mailbox and
DeskConversation; nothing stored but the defaults."""

from datetime import timedelta

from django.db.models import Count, Max
from django.utils import timezone

from . import desk
from .models import DeskConversation, DeskDefault, Mail, Person, Team, audit

RECENT = (0, 1, 4, 24)  # hours of answered conversations the board may show too (0: open only)
ONLINE_MINUTES = 5


def _hm(dt):
    return timezone.localtime(dt).strftime("%H:%M") if dt else ""


def _ms(dt):
    return int(dt.timestamp() * 1000) if dt else None


def version(kind="live"):
    """What changes the board: the desk's mail, its conversations' state, the defaults, the
    minute (waits grow, presence comes and goes)."""
    mail = Mail.objects.filter(team=None, owner=None).aggregate(n=Max("id"))["n"] or 0
    conv = DeskConversation.objects.aggregate(at=Max("updated_at"), n=Count("id"))
    dflt = DeskDefault.objects.aggregate(at=Max("updated_at"), n=Count("id"))
    return (f"{kind}:{mail}:{conv['n']}:{_ms(conv['at']) or 0}:{dflt['n']}:{_ms(dflt['at']) or 0}"
            f":{int(timezone.now().timestamp() // 30)}")


def _defaults_by_user():
    """{user id: [{"name", "colour", "fg", "teams"}]}: the personas each person has by default
    ("all teams", or the teams by name)."""
    teams = dict(Team.objects.values_list("id", "name"))
    out = {}
    rows = {}
    for d in DeskDefault.objects.select_related("sender").order_by("sender__local_part", "team__name"):
        rows.setdefault((d.user_id, d.sender_id), [d.sender, []])[1].append(d.team_id)
    for (uid, _), (s, tids) in rows.items():
        bg, fg = desk.colour(s)
        names = ["all teams" if t is None else teams.get(t, "") for t in tids]
        out.setdefault(uid, []).append({"name": s.display_name or s.local_part, "colour": bg, "fg": fg,
                                        "teams": ", ".join(n for n in names if n)})
    return out


def board(user, kind="live", recent=0, team_id=None, sender_id=None, now=None):
    """{"columns": [...], "filters": {...}}: each column {uid (0: nobody), name, desk, me,
    online, gone (no longer on the desk), n, attention, longest, defaults, cards}; each card a
    desk conversation still needing something (and, with recent, those answered in the last
    recent hours), most urgent first (desk.sort_key)."""
    now = now or timezone.now()
    qs = Mail.objects.filter(team=None, owner=None, trashed_at__isnull=True).exclude(folder="drafts")
    test_threads = set(qs.filter(is_test=True).order_by().values_list("thread", flat=True).distinct())
    by_thread = {}
    for t, mid, folder, local, team, subj, at in (qs.order_by("id").values_list(
            "thread", "id", "folder", "persona", "desk_team_id", "subject", "created_at")):
        if (t in test_threads) != (kind == "test"):
            continue
        by_thread.setdefault(t, []).append((mid, folder, local, team, subj, at))
    if team_id:
        by_thread = {t: ms for t, ms in by_thread.items() if any(m[3] == team_id for m in ms)}
    if sender_id:
        locs = {k for k, s in desk.persona_senders().items() if s.id == sender_id}
        by_thread = {t: ms for t, ms in by_thread.items() if any((m[2] or "").lower() in locs for m in ms)}
    sts = desk.states(list(by_thread), user, now)
    since = now - timedelta(hours=recent) if recent else None
    keep = [t for t, st in sts.items()
            if st["section"] != desk.REST or (since and by_thread[t][-1][5] >= since)]
    here = desk.on_now(keep, user.id if user else None)
    teams = dict(Team.objects.values_list("id", "name"))
    memo = {}
    cards = []
    for t in keep:
        st, ms = sts[t], by_thread[t]
        first_in = next((m for m in ms if m[1] == "inbox"), None) or ms[0]
        latest = ms[-1][5]
        persona = desk.describe(first_in[2], memo=memo) if first_in[2] else None
        tri = st["triage"] or {}
        follow = st["follow"]
        cards.append({
            "thread": t, "subject": ms[0][4] or "(no subject)", "url": f"/inbox/desk/t/{t}/",
            "team": teams.get(first_in[3], ""),
            "persona": {"name": persona["name"], "colour": persona["colour"], "fg": persona["fg"]} if persona else None,
            "section": st["section"], "waiting": st["waiting"], "since": _ms(st["since"]),
            "urgent": tri.get("urgency") == "high", "ask": tri.get("ask", ""),
            "follow": {"due": follow["due"], "at": _hm(follow["at"]), "note": follow["note"]} if follow else None,
            "npc": (st["npc"] or {}).get("status", ""), "done_by": st["done_by"],
            "owner": st["owner_id"] or 0, "auto": st["auto"], "assigned_by": st["assigned_by"],
            "here": here.get(t, []), "latest": _hm(latest), "count": len(ms),
            "_key": desk.sort_key(st, latest)})
    cards.sort(key=lambda c: c.pop("_key"))
    people = desk.assignees(user)
    defaults = _defaults_by_user()
    seen = {p.user_id: p.last_seen_at for p in Person.objects.filter(user_id__in=[p["id"] for p in people])}
    online_after = now - timedelta(minutes=ONLINE_MINUTES)
    cols = [{"uid": 0, "name": "Not assigned", "desk": True, "me": False, "online": False, "gone": False, "defaults": []}]
    for p in people:
        cols.append({"uid": p["id"], "name": p["name"], "desk": p["desk"], "me": p["me"], "gone": False,
                     "online": bool(seen.get(p["id"]) and seen[p["id"]] >= online_after),
                     "defaults": defaults.get(p["id"], [])})
    known = {c["uid"] for c in cols}
    names = dict(DeskConversation.objects.filter(owner__isnull=False, thread__in=keep).values_list("owner_id", "owner_name"))
    for uid in dict.fromkeys(c["owner"] for c in cards):
        if uid not in known:  # someone no longer on the desk still has some
            cols.append({"uid": uid, "name": names.get(uid, "?"), "desk": True, "me": False, "online": False,
                         "gone": True, "defaults": []})
            known.add(uid)
    for col in cols:
        col["cards"] = [c for c in cards if c["owner"] == col["uid"]]
        col["n"] = len(col["cards"])
        col["attention"] = sum(1 for c in col["cards"] if c["section"] == desk.ATTENTION)
        waits = [c["since"] for c in col["cards"] if c["waiting"] and c["since"]]
        col["oldest"] = min(waits) if waits else None
    return {"columns": cols, "now": _ms(now), "kind": kind, "recent": recent}


def filters():
    """The board's choices: teams and personas."""
    return {"teams": [{"id": t.id, "name": t.name, "test": t.is_test} for t in Team.objects.filter(active=True)],
            "personas": [{"id": s.id, "name": s.display_name or s.local_part} for s in desk.personas()]}


# --- default assignments -------------------------------------------------------------------------

def grid():
    """The Default assignments grid: {"teams": [Team], "rows": [{sender, colour, fg, ai, all
    (user id or None), cells: [{team, user, inherited}]}], "people": desk.assignees(),
    "by_person": [{name, personas: [...]}], "gone": rules to someone no longer on the desk}."""
    from . import conf
    from .models import NpcProfile
    teams = sorted(Team.objects.filter(active=True), key=lambda t: (t.is_test, t.name.lower()))
    people = desk.assignees()
    ok = {p["id"] for p in people}
    rules = {(d.sender_id, d.team_id): d.user_id for d in DeskDefault.objects.all()}
    ai = (set(NpcProfile.objects.filter(enabled=True).values_list("sender_id", flat=True))
          if conf.flag("npc_enabled") else set())
    rows = []
    for s in desk.personas():
        bg, fg = desk.colour(s)
        every = rules.get((s.id, None))
        cells = [{"team": t.id, "name": t.name, "user": rules.get((s.id, t.id)), "inherited": every} for t in teams]
        rows.append({"sender": s, "colour": bg, "fg": fg, "ai": s.id in ai, "all": every, "cells": cells,
                     "team_persona": "{team}" in s.local_part})
    by = _defaults_by_user()
    by_person = [{"id": p["id"], "name": p["name"], "desk": p["desk"], "personas": by.get(p["id"], [])} for p in people]
    gone = sum(1 for uid in rules.values() if uid not in ok)
    return {"teams": teams, "rows": rows, "people": people, "by_person": by_person, "gone": gone,
            "unassigned": len(desk.unassigned_open())}


class Invalid(Exception):
    pass


def set_rule(sender_id, team_id, user_id, by):
    """One cell of the grid: the persona's conversations with the team (None: all teams) go to
    user_id by default (None or "": no rule)."""
    s = desk.personas().filter(pk=sender_id).first()
    if s is None:
        raise Invalid("Unknown persona.")
    team = None
    if team_id:
        team = Team.objects.filter(pk=team_id).first()
        if team is None:
            raise Invalid("Unknown team.")
    if user_id:
        to = next((u for u in desk.assignee_users() if u.id == int(user_id)), None)
        if to is None:
            raise Invalid("Choose someone on the desk.")
        DeskDefault.objects.update_or_create(sender=s, team=team, defaults={"user": to, "set_by": by[:150]})
        who = desk.user_name(to)
    else:
        DeskDefault.objects.filter(sender=s, team=team).delete()
        who = "nobody"
    audit("desk_default", f"{s.local_part} / {team.name if team else 'all teams'} -> {who}", by)


def set_row(sender_id, user_id, by):
    """A whole row: all teams to user_id (None: nobody), no team of its own."""
    s = desk.personas().filter(pk=sender_id).first()
    if s is None:
        raise Invalid("Unknown persona.")
    DeskDefault.objects.filter(sender=s, team__isnull=False).delete()
    set_rule(s.id, None, user_id, by)

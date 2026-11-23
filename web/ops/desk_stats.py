"""The persona desk's Dashboard (/inbox/desk/dashboard/): how the desk is keeping up with the
teams, computed from its own mailbox (what the teams wrote to the personas, what the personas
sent), nothing stored. Each team message is paired with the first persona message to that
team after it in the same conversation (desk.states' rule): its answer, by whom (a desk
member, ExCon, an AI persona, an inject, a follow-up) and how long it took. Operational
figures only: never grades, rubrics or Claude's reading of the teams' work (only the desk's
own reading of what needs an answer, ops/desk_ai.py)."""

from collections import Counter
from datetime import timedelta
from statistics import median

from django.utils import timezone

from . import conf, desk
from .models import DeskConversation, Mail, NpcReply, Person, ReplyAnalysis, Team

HOURS = 12  # the activity strip
ONLINE_SECONDS = 180

# Who wrote a persona message that answered a team (by the Mail's Message).
INJECT, FOLLOWUP, AI_PERSONA, EXCON = "Inject", "Follow-up (ExCon)", "AI persona", "ExCon"
NOT_TIMED = (INJECT, FOLLOWUP)  # went out on their own schedule: not the desk answering


def kinds():
    """Which traffic there is: ("live" | "test") present."""
    return set(("test" if t else "live") for t in Mail.objects.filter(team=None, owner=None).order_by()
               .values_list("is_test", flat=True).distinct())


def _mins(a, b):
    return (b - a).total_seconds() / 60


def _med(xs):
    return round(median(xs)) if xs else None


def _p90(xs):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(0.9 * len(xs)))])


def responder(m, names):
    msg_kind = m["message__kind"]
    if m["message__npc"]:
        return AI_PERSONA
    if m["author_id"]:
        return names.get(m["author_id"], "?")
    if msg_kind in ("live", "test", "oneoff"):
        return INJECT
    if msg_kind == "followup":
        return FOLLOWUP
    return EXCON


# Claude's reading of each reply (ai.analyse_reply), the parts the desk needs to play the
# personas: what the teams ask them, what they commit to, the figures they state, and the flags
# about the conversation itself. Never quality, tone or the AI-written estimate.
DESK_FLAGS = ("escalate", "safety", "wrong_recipient", "out_of_role")
LIST_MAX = 25


def readings(mails, pairs, teams, now):
    """From the teams' messages on the desk read by Claude: requests per persona (each marked
    open while its message has no answer), commitments, stated figures and the desk's flags."""
    from . import ai
    status = {}
    for x, a, closed in pairs:
        status[x["id"]] = "answered" if a is not None else "closed" if closed else "open"
    inbox = {m["id"]: m for m in mails if m["folder"] == "inbox"}
    rid_mail = dict(Mail.objects.filter(id__in=list(inbox), desk_response__isnull=False)
                    .values_list("desk_response_id", "id"))
    found = (ReplyAnalysis.objects.filter(response_id__in=list(rid_mail), status="done")
             .values("response_id", "summary", "asks", "commitments", "facts", "flags"))
    memo, by_persona, commitments, facts, flags = {}, {}, [], [], []
    for a in found:
        m = inbox[rid_mail[a["response_id"]]]
        base = {"at": m["created_at"], "team": teams.get(m["desk_team_id"]), "thread": m["thread"],
                "status": status.get(m["id"], "open"),
                "persona": desk.describe(m["persona"], memo=memo) if m["persona"] else None}
        if a["asks"]:
            p = by_persona.setdefault(m["persona"] or "?", {"persona": base["persona"], "asks": [], "open": 0})
            for ask in a["asks"]:
                p["asks"].append({**base, "text": str(ask)[:300]})
                p["open"] += base["status"] == "open"
        commitments += [{**base, "text": str(c)[:300]} for c in a["commitments"] or []]
        facts += [{**base, "text": str(f)[:300]} for f in a["facts"] or []]
        mine = [ai.FLAGS[f] for f in a["flags"] or [] if f in DESK_FLAGS]
        if mine:
            flags.append({**base, "flags": mine, "text": a["summary"][:400]})
    newest = lambda xs: sorted(xs, key=lambda x: x["at"], reverse=True)[:LIST_MAX]  # noqa: E731
    requests = sorted(by_persona.values(), key=lambda p: (-p["open"], -len(p["asks"])))
    for p in requests:
        p["n"] = len(p["asks"])
        p["asks"] = sorted(p["asks"], key=lambda x: (x["status"] != "open", -x["at"].timestamp()))[:8]
    return {"requests": requests, "commitments": newest(commitments), "facts": newest(facts), "flags": newest(flags),
            "read_n": len(found), "readable_n": len(rid_mail)}


CONV_MAX = 15  # conversations listed per person


def by_assignee(states, by_thread, teams, now, memo):
    """Who on the desk has which conversations (DeskConversation.owner), with nobody's last:
    [{"uid", "name", "desk", "n", "attention", "waiting", "longest", "follow_due", "follow_later",
    "later", "answered", "convs": [...] (those still open first)}]. Every desk member is listed,
    with none if so; nobody's row counts only what is still open."""
    order = [s for s, _ in desk.SECTIONS]
    users = {u["id"]: u for u in desk.assignees()}
    rows = {uid: [] for uid, u in users.items() if u["desk"]}
    for t, st in states.items():
        if st["owner_id"]:
            rows.setdefault(st["owner_id"], []).append((t, st))
    nobody = [(t, st) for t, st in states.items() if not st["owner_id"] and st["section"] != desk.REST]
    listed = {t for xs in list(rows.values()) + [nobody] for t, st in xs}
    subjects = {}
    for t, subj in (Mail.objects.filter(team=None, owner=None, thread__in=listed).exclude(folder="drafts")
                    .order_by("id").values_list("thread", "subject")):
        subjects.setdefault(t, subj)
    names = dict(DeskConversation.objects.filter(owner__isnull=False, thread__in=listed).values_list("owner_id", "owner_name"))

    def conv(t, st):
        ms = by_thread.get(t, [])
        first_in = next((m for m in ms if m["folder"] == "inbox"), None) or (ms[0] if ms else {})
        return {"thread": t, "subject": subjects.get(t, ""), "section": st["section"], "waiting": st["waiting"],
                "mins": round(_mins(st["since"], now)) if st["since"] else None, "follow": st["follow"],
                "team": teams.get(first_in.get("desk_team_id")),
                "persona": desk.describe(first_in["persona"], memo=memo) if first_in.get("persona") else None,
                "assigned_by": st["assigned_by"]}

    def row(uid, name, xs, is_desk):
        xs = sorted(xs, key=lambda x: (order.index(x[1]["section"]), x[1]["since"] or now))
        w = [st for _, st in xs if st["waiting"] and st["section"] == desk.ATTENTION]
        return {"uid": uid, "name": name, "desk": is_desk, "n": len(xs),
                "attention": sum(1 for _, st in xs if st["section"] == desk.ATTENTION), "waiting": len(w),
                "longest": round(max(_mins(st["since"], now) for st in w)) if w else None,
                "follow_due": sum(1 for _, st in xs if st["follow"] and st["follow"]["due"]),
                "follow_later": sum(1 for _, st in xs if st["follow"] and not st["follow"]["due"]),
                "later": sum(1 for _, st in xs if st["section"] == desk.LATER),
                "answered": sum(1 for _, st in xs if st["section"] == desk.REST),
                "convs": [conv(t, st) for t, st in xs[:CONV_MAX]], "more": max(0, len(xs) - CONV_MAX)}

    out = [row(uid, users[uid]["name"] if uid in users else names.get(uid, "?"), xs, users.get(uid, {}).get("desk", False))
           for uid, xs in rows.items()]
    out.sort(key=lambda r: (-r["attention"], -r["n"], r["name"].lower()))
    out.append(row(0, "Nobody", nobody, False))
    return out


def compute(kind="live", now=None):
    now = now or timezone.now()
    qs = Mail.objects.filter(team=None, owner=None, trashed_at__isnull=True).exclude(folder="drafts")
    qs = qs.filter(is_test=(kind == "test"))
    mails = list(qs.order_by("id").values("id", "thread", "folder", "desk_team_id", "persona", "created_at",
                                          "author_id", "message__kind", "message__npc"))
    names = dict(Person.objects.filter(id__in={m["author_id"] for m in mails if m["author_id"]})
                 .values_list("id", "name"))
    done = dict(DeskConversation.objects.filter(thread__in={m["thread"] for m in mails}).values_list("thread", "done_upto"))
    target = int(conf.get("desk_wait_minutes") or 0)

    # Pair each team message with the persona message that answered it.
    pairs, pending = [], {}  # (inbox mail, answering sent mail or None, closed)
    for m in mails:
        key = (m["thread"], m["desk_team_id"])
        if m["folder"] == "inbox":
            pending.setdefault(key, []).append(m)
        elif m["folder"] == "sent":
            for x in pending.pop(key, []):
                pairs.append((x, m, False))
    for (thread, _), xs in pending.items():
        for x in xs:
            pairs.append((x, None, x["id"] <= done.get(thread, 0)))

    answers = []  # (team message, answer, minutes, who)
    for x, a, _ in pairs:
        if a is not None:
            answers.append((x, a, _mins(x["created_at"], a["created_at"]), responder(a, names)))
    timed = [mins for _, _, mins, who in answers if who not in NOT_TIMED]
    hour_ago = now - timedelta(hours=1)
    timed_hour = [mins for x, a, mins, who in answers if who not in NOT_TIMED and a["created_at"] >= hour_ago]

    # Where it stands now (desk.states: the Inbox's sections).
    threads = list({m["thread"] for m in mails})
    states = desk.states(threads, now=now)
    waiting = [(t, st) for t, st in states.items() if st["section"] == desk.ATTENTION and st["waiting"]]
    sections = Counter(st["section"] for st in states.values())
    follow_due = sum(1 for st in states.values() if st["follow"] and st["follow"]["due"])
    follow_later = sum(1 for st in states.values() if st["follow"] and not st["follow"]["due"])
    longest = max((_mins(st["since"], now) for _, st in waiting), default=None)

    teams = {t.id: t for t in Team.objects.filter(is_test=(kind == "test"))}
    by_thread = {}
    for m in mails:
        by_thread.setdefault(m["thread"], []).append(m)

    def first_open(t, st):
        """The waiting conversation's first unanswered team message (for its team and persona)."""
        return next((m for m in by_thread.get(t, []) if m["id"] == st["open_id"]), None)

    subjects = dict(Mail.objects.filter(id__in=[st["open_id"] for _, st in waiting]).values_list("id", "subject"))
    memo = {}
    queue = []
    for t, st in sorted(waiting, key=lambda x: x[1]["since"]):
        m = first_open(t, st)
        queue.append({"thread": t, "since": st["since"], "mins": round(_mins(st["since"], now)),
                      "team": teams.get(m["desk_team_id"]) if m else None,
                      "persona": desk.describe(m["persona"], memo=memo) if m and m["persona"] else None,
                      "owner": st["owner"], "triage": st["triage"], "subject": subjects.get(st["open_id"], "")})

    # Per team: are any being left behind?
    team_rows = []
    for tid, t in teams.items():
        if not t.active and not any(x["desk_team_id"] == tid for x, _, _ in pairs):
            continue
        mine = [(x, a, c) for x, a, c in pairs if x["desk_team_id"] == tid]
        mins = [m for x, a, m, who in answers if x["desk_team_id"] == tid and who not in NOT_TIMED]
        w = [st for th, st in waiting if (first_open(th, st) or {}).get("desk_team_id") == tid]
        team_rows.append({
            "team": t, "received": len(mine), "answered": sum(1 for _, a, _ in mine if a is not None),
            "closed": sum(1 for _, a, c in mine if a is None and c), "waiting": len(w),
            "longest": round(max(_mins(st["since"], now) for st in w)) if w else None,
            "median": _med(mins), "persona_out": sum(1 for m in mails if m["folder"] == "sent" and m["desk_team_id"] == tid),
            "last_in": max((x["created_at"] for x, _, _ in mine), default=None)})
    team_rows.sort(key=lambda r: (-(r["longest"] or -1), r["team"].name))

    # Per persona: which ones carry the load.
    persona_rows = {}
    for x, a, c in pairs:
        p = persona_rows.setdefault(x["persona"] or "?", {"received": 0, "answered": 0, "closed": 0, "mins": [],
                                                          "last_in": None})
        p["received"] += 1
        p["answered"] += a is not None
        p["closed"] += a is None and c
        p["last_in"] = max(filter(None, [p["last_in"], x["created_at"]]))
    for x, a, mins, who in answers:
        if who not in NOT_TIMED:
            persona_rows[x["persona"] or "?"]["mins"].append(mins)
    wait_by_persona = Counter((first_open(t, st) or {}).get("persona") or "?" for t, st in waiting)
    sent_by_persona = Counter(m["persona"] for m in mails if m["folder"] == "sent")
    personas = []
    for local, p in persona_rows.items():
        personas.append({**desk.describe(local, memo=memo), "received": p["received"], "answered": p["answered"],
                         "closed": p["closed"], "waiting": wait_by_persona.get(local, 0), "median": _med(p["mins"]),
                         "sent": sent_by_persona.get(local, 0), "last_in": p["last_in"]})
    personas.sort(key=lambda r: (-r["waiting"], -r["received"]))

    # Who on the desk is answering.
    owners = Counter(st["owner"] for st in states.values() if st["owner"] and st["section"] != desk.REST)
    who_rows = {}
    for x, a, mins, who in answers:
        r = who_rows.setdefault(who, {"answers": 0, "mins": [], "last": None})
        r["answers"] += 1
        r["last"] = max(filter(None, [r["last"], a["created_at"]]))
        if who not in NOT_TIMED:
            r["mins"].append(mins)
    online_after = now - timedelta(seconds=ONLINE_SECONDS)
    desk_people = list(Person.objects.filter(role="desk", active=True))
    for p in desk_people:
        who_rows.setdefault(p.name, {"answers": 0, "mins": [], "last": None})
    seen = {p.name: p.last_seen_at for p in desk_people}
    people = [{"name": n, "answers": r["answers"], "median": _med(r["mins"]), "last": r["last"],
               "taken": owners.get(n, 0), "desk": n in seen, "seen": seen.get(n),
               "online": bool(seen.get(n) and seen[n] >= online_after),
               "machine": n in (INJECT, FOLLOWUP, AI_PERSONA)}
              for n, r in who_rows.items()]
    people.sort(key=lambda r: (r["machine"], -r["answers"], r["name"]))

    # Activity: team messages in, persona answers out, per hour.
    start = (now - timedelta(hours=HOURS - 1)).replace(minute=0, second=0, microsecond=0)
    ins = Counter(int((m["created_at"] - start).total_seconds() // 3600) for m in mails
                  if m["folder"] == "inbox" and m["created_at"] >= start)
    outs = Counter(int((a["created_at"] - start).total_seconds() // 3600) for _, a, _, who in answers
                   if who not in NOT_TIMED and a["created_at"] >= start)
    top = max(list(ins.values()) + list(outs.values()) + [1])
    hours = [{"at": start + timedelta(hours=i), "in": ins.get(i, 0), "out": outs.get(i, 0),
              "in_pct": round(100 * ins.get(i, 0) / top), "out_pct": round(100 * outs.get(i, 0) / top)}
             for i in range(HOURS)]

    requests = readings(mails, pairs, teams, now)
    assigned = by_assignee(states, by_thread, teams, now, memo)

    npc = Counter(NpcReply.objects.filter(status__in=["queued", "writing", "held", "failed"],
                                          team__is_test=(kind == "test")).values_list("status", flat=True))
    within = [m for m in timed if target and m <= target]
    received = sum(1 for m in mails if m["folder"] == "inbox")
    return {
        "kind": kind, "now": now, "target": target, "received": received,
        "received_hour": sum(1 for m in mails if m["folder"] == "inbox" and m["created_at"] >= hour_ago),
        "answered": len(answers), "closed": sum(1 for _, a, c in pairs if a is None and c),
        "attention": sections.get(desk.ATTENTION, 0), "quiet": sections.get(desk.QUIET, 0),
        "later": sections.get(desk.LATER, 0), "waiting": len(waiting), "longest": round(longest) if longest is not None else None,
        "over_target": sum(1 for _, st in waiting if target and _mins(st["since"], now) >= target),
        "untaken": sum(1 for _, st in waiting if not st["owner"]),
        "follow_due": follow_due, "follow_later": follow_later,
        "median": _med(timed), "p90": _p90(timed), "median_hour": _med(timed_hour),
        "within_pct": round(100 * len(within) / len(timed)) if timed and target else None,
        "oldest": queue[:10], "assigned": assigned, "teams": team_rows, "personas": personas, "people": people, "hours": hours,
        "asks": sorted([o for o in queue if o["triage"] and o["triage"]["ask"]],
                       key=lambda o: (desk.URGENCY.get(o["triage"]["urgency"], 1), o["since"]))[:20],
        **requests,
        "npc": {"queued": npc.get("queued", 0) + npc.get("writing", 0), "held": npc.get("held", 0),
                "failed": npc.get("failed", 0)},
    }

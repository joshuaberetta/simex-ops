"""The comms network (Dashboard → Network): who writes to whom, worked out from what the app
already keeps, for ExCon's map of the exercise's mail (views_network.py, static/ops/network.js).

A send is one act of writing: {"id", "at" (epoch ms), "f" (the node it came from), "to" (the
nodes it went to), "k" (inject | followup | answer | leader | participant), "s" (subject)}.
Nodes are "t:<team id>" (a team's inbox), "m:<person id>" (a member, folded into their team on
the map until it is opened up), "l:<person id>" (a team leader, and the team leader persona they
play), "p:<username>" (a persona) and "x" (ExCon, a message with no persona). Sends come from:

- Message, status sent (so never withdrawn, pending or failed): what the personas, the desk,
  ExCon and the leaders send. Its recipients are the mailboxes its Mail landed in (a team's
  inbox; a member's or leader's own when it went only there), or, by email or when the mailboxes
  were cleared, its team / person. An inject's copy for a team leader (Inject.to_leaders) is
  left out, as in the other figures; logins and role-play reminders aren't mail between players.
- A participant's Sent Mail (no Message): to personas, the team leader persona, teammates, their
  own team or another team, from Mail.recipients. Written from a member's own mailbox to a
  persona, the team inbox's Sent has a copy (same author, time and subject): counted once.
- Response with no Mail: a team's reply by email.

Nothing is stored: the figures follow withdrawals, Clear mailboxes and Start afresh by themselves.
The desk's copies (Mail team None) and anybody's reading are never sends.
"""

from datetime import datetime, timedelta, timezone as dt_tz

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from . import render
from .models import Mail, Message, NpcProfile, Person, Response, Team

LIVE_KINDS = ("live", "followup", "reply", "leader", "desk", "tl")
TEST_KINDS = ("test", "oneoff")
INJECT_KINDS = ("live", "test", "oneoff", "followup")  # a team leader's copy of these isn't counted
KIND = {"live": "inject", "test": "inject", "oneoff": "inject", "followup": "followup", "reply": "answer",
        "desk": "answer", "tl": "answer", "leader": "leader"}
OVERLAP = timedelta(seconds=30)  # polls look back this far (a Message is stamped sent a moment after it's made)


def _ms(dt):
    return int(dt.timestamp() * 1000) if dt else 0


class Ctx:
    """What resolving the nodes of many sends needs, read once."""

    def __init__(self, kind):
        self.test = kind == "test"
        self.teams = {t.id: t for t in Team.objects.all()}
        self.people = {p.id: p for p in Person.objects.all()}
        locals_ = render.leader_locals()  # {filled team leader username: team id}
        leaders = {p.team_id: p.id for p in self.people.values() if p.role == "tl" and p.active and p.team_id}
        self.leader_of_local = {local: leaders[tid] for local, tid in locals_.items() if tid in leaders}

    def team_is_test(self, tid):
        t = self.teams.get(tid)
        return bool(t and t.is_test)

    def persona(self, local):
        """A persona username's node: the team leader persona is the leader who plays it."""
        local = (local or "").lower()
        if not local:
            return None
        lid = self.leader_of_local.get(local)
        return f"l:{lid}" if lid else f"p:{local}"

    def person(self, pid, team_id=None):
        p = self.people.get(pid)
        if not p:
            return f"t:{team_id}" if team_id else None
        if p.role == "tl":
            return f"l:{p.id}"
        if p.role == "member":
            return f"m:{p.id}"
        return None


def _messages(ctx, since=None):
    kinds = TEST_KINDS + LIVE_KINDS if ctx.test else LIVE_KINDS
    qs = Message.objects.filter(status="sent", kind__in=kinds)
    if since:
        qs = qs.filter(sent_at__gte=since)
    rows = list(qs.values("id", "kind", "channel", "persona", "author_id", "team_id", "inbox_team_id",
                          "inbox_owner_id", "person_id", "subject", "sent_at", "created_at"))
    boxes = {}  # message id -> [(team id, owner id)] of the inboxes its Mail landed in
    for mid, tid, oid in (Mail.objects.filter(message__in=qs.values("id"), folder="inbox", team__isnull=False)
                          .values_list("message_id", "team_id", "owner_id")):
        boxes.setdefault(mid, []).append((tid, oid))
    out = []
    for r in rows:
        tid = r["inbox_team_id"] or r["team_id"]
        test = r["kind"] in TEST_KINDS or ctx.team_is_test(tid)
        if test != ctx.test:
            continue
        inject_kind = r["kind"] in INJECT_KINDS
        author = ctx.people.get(r["author_id"])
        if author and author.role == "tl":
            frm = f"l:{author.id}"
        else:
            frm = ctx.persona(r["persona"]) or "x"
        to = []
        landed = boxes.get(r["id"])
        if landed:
            team_copy = any(o is None for _, o in landed)
            for t, o in landed:
                if o is None:
                    node = f"t:{t}"
                elif team_copy and ctx.people.get(o) and ctx.people[o].role == "member":
                    continue  # a member's copy of the team inbox's (delivery.to_members)
                else:
                    node = ctx.person(o)
                    if node and node.startswith("l:") and inject_kind:
                        continue  # an inject's copy for the leader
                to.append(node)
        else:  # email, or its mailboxes were cleared
            owner = r["inbox_owner_id"] or r["person_id"]
            node = ctx.person(owner, tid) if owner else (f"t:{tid}" if tid else None)
            if not (node and node.startswith("l:") and inject_kind):
                to.append(node)
        to = sorted({n for n in to if n and n != frm})
        if not to:
            continue
        at = r["sent_at"] or r["created_at"]
        out.append({"id": f"g{r['id']}", "at": _ms(at), "f": frm, "to": to, "k": KIND.get(r["kind"], "answer"),
                    "s": (r["subject"] or "")[:120]})
    return out


def _recipients(ctx, mail):
    rec = mail["recipients"] or {}
    keys = [k for f in ("to", "cc", "bcc") for k in (rec.get(f) or [])]
    to = []
    for k in keys:
        kind, _, val = k.partition(":")
        if kind == "p":
            to.append(ctx.persona(val))
        elif kind == "t" and val.isdigit():
            to.append(f"t:{val}")
        elif kind in ("m", "l") and val.isdigit():
            to.append(ctx.person(int(val)))
    if not keys:  # older mail, before compose kept the keys
        if mail["persona"]:
            to.append(ctx.persona(mail["persona"]))
        elif mail["to_team_id"]:
            to.append(f"t:{mail['to_team_id']}")
    return to


def _participant_mail(ctx, after_id=None):
    qs = Mail.objects.filter(folder="sent", team__isnull=False, message__isnull=True, author__role="member")
    qs = qs.filter(Q(is_test=True) | Q(team__is_test=True)) if ctx.test else qs.filter(is_test=False, team__is_test=False)
    # Written from a member's own mailbox to a persona, the team inbox's Sent has a copy: not a second send.
    twin = Mail.objects.filter(folder="sent", team=OuterRef("team"), owner=OuterRef("author"),
                               author=OuterRef("author"), created_at=OuterRef("created_at"), subject=OuterRef("subject"))
    qs = qs.exclude(Q(owner__isnull=True) & Exists(twin))
    if after_id:
        qs = qs.filter(id__gt=after_id)
    out = []
    for m in qs.order_by("id").values("id", "author_id", "recipients", "persona", "to_team_id", "subject", "created_at"):
        frm = f"m:{m['author_id']}"
        to = sorted({n for n in _recipients(ctx, m) if n and n != frm})
        if not to:
            continue
        out.append({"id": f"s{m['id']}", "at": _ms(m["created_at"]), "f": frm, "to": to, "k": "participant",
                    "s": (m["subject"] or "")[:120]})
    return out


def _email_replies(ctx, after_id=None):
    qs = Response.objects.filter(mail__isnull=True, kind="test" if ctx.test else "live", team__isnull=False)
    if after_id:
        qs = qs.filter(id__gt=after_id)
    out = []
    for r in qs.values("id", "team_id", "person_id", "persona", "subject", "received_at"):
        frm = ctx.person(r["person_id"], r["team_id"]) if r["person_id"] else f"t:{r['team_id']}"
        to = ctx.persona(r["persona"])
        if not frm or not to or to == frm:
            continue
        out.append({"id": f"r{r['id']}", "at": _ms(r["received_at"]), "f": frm, "to": [to], "k": "participant",
                    "s": (r["subject"] or "")[:120]})
    return out


def sends(kind="live", cursor=None):
    """(sends, cursor) of the exercise ("live") or the Test team ("test"), oldest first; with a
    cursor from an earlier call, only what came since (and a few already given: the client
    keeps each id once)."""
    ctx = Ctx(kind)
    now = timezone.now()
    cursor = cursor or {}
    since = None
    if cursor.get("t"):
        try:
            since = datetime.fromtimestamp(int(cursor["t"]) / 1000, tz=dt_tz.utc) - OVERLAP
        except (TypeError, ValueError, OverflowError):
            since = None
    s_after, r_after = _int(cursor.get("s")), _int(cursor.get("r"))
    out = _messages(ctx, since) + _participant_mail(ctx, s_after) + _email_replies(ctx, r_after)
    out.sort(key=lambda e: (e["at"], e["id"]))
    new_cursor = {"t": _ms(now),
                  "s": max([s_after or 0] + list(Mail.objects.order_by("-id").values_list("id", flat=True)[:1])),
                  "r": max([r_after or 0] + list(Response.objects.order_by("-id").values_list("id", flat=True)[:1]))}
    return out, new_cursor


def _int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# --- the nodes ---------------------------------------------------------------------------------------

def nodes(kind, used):
    """Every node the map may show: the teams of this view (the exercise's or the Test team),
    their leaders and members (silent ones too: a member who writes nothing is worth seeing),
    and the personas and others the sends name (used: node ids)."""
    from . import desk
    test = kind == "test"
    teams = [t for t in Team.objects.filter(active=True) if t.is_test == test]
    out = []
    for i, t in enumerate(teams):
        out.append({"id": f"t:{t.id}", "type": "team", "name": t.name, "team": f"t:{t.id}", "slot": i})
    tids = {t.id for t in teams}
    want_people = {int(n[2:]) for n in used if n[:2] in ("m:", "l:") and n[2:].isdigit()}
    people = Person.objects.filter(Q(team_id__in=tids, active=True, role__in=("member", "tl")) | Q(pk__in=want_people))
    for p in people.select_related("team", "team_role"):
        if p.role not in ("member", "tl"):
            continue
        out.append({"id": f"{'l' if p.role == 'tl' else 'm'}:{p.id}", "type": "leader" if p.role == "tl" else "member",
                    "name": p.name, "team": f"t:{p.team_id}" if p.team_id else None,
                    "role": p.team_role.name if p.team_role_id else ("Team leader" if p.role == "tl" else "")})
    ai = set(NpcProfile.objects.filter(enabled=True).values_list("sender__local_part", flat=True))
    memo = {}
    for n in sorted(x for x in used if x.startswith("p:")):
        local = n[2:]
        d = desk.describe(local, memo=memo)
        s, _ = render.sender_by_local(local)
        out.append({"id": n, "type": "persona", "name": d["name"], "addr": d["addr"], "team": None,
                    "ai": bool(s and s.local_part in ai)})
    if "x" in used:
        out.append({"id": "x", "type": "excon", "name": "ExCon", "team": None})
    known = {n["id"] for n in out}
    for n in sorted(used - known):  # someone since gone (a team made inactive, a person deleted)
        out.append({"id": n, "type": {"t": "team", "m": "member", "l": "leader"}.get(n[0], "persona"),
                    "name": "(removed)", "team": None, "gone": True})
    return out


def graph(kind="live"):
    """The whole map: {"nodes", "sends", "cursor"}."""
    ss, cursor = sends(kind)
    used = {e["f"] for e in ss} | {n for e in ss for n in e["to"]}
    return {"nodes": nodes(kind, used), "sends": ss, "cursor": cursor}

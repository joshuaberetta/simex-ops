"""Filter views of a SIMEX Mail list (views_inbox.inbox): quick filters in every mailbox
(unread, starred, with files, not replied) and, on the persona desk, more (personas, teams,
where a conversation stands, who has it, how long a team has waited, live or test, recent).

Everything is in the URL (f=, and on the desk p= t= st= own= wait= kind= recent=), so a filtered
list can be bookmarked, paged and reopened; the thread view takes the same parameters for its
neighbours and back link. Nothing is stored. Teams never cross: every query here is narrowed
from the viewer's own mailbox (v.mails()), never widened.
"""
from datetime import timedelta
from urllib.parse import urlencode

from django.utils import timezone

from . import desk

# Every mailbox's quick filters: key, label, what a row needs (views_inbox.conversations' rows).
GENERAL = [("unread", "Unread"), ("starred", "Starred"), ("files", "Has files"), ("unanswered", "Not replied")]
GENERAL_KEYS = [k for k, _ in GENERAL]

# The persona desk's conversation states (desk.states), any of them (OR).
STATES = [("attention", "Needs attention"), ("waiting", "A team is waiting"), ("urgent", "Urgent (SIMEX AI)"),
          ("quiet", "Probably no reply needed"), ("follow", "Follow-up set"), ("npc", "AI persona answering"),
          ("done", "Marked no reply needed")]
STATE_KEYS = [k for k, _ in STATES]
WAITS = [(15, "15 minutes"), (30, "30 minutes"), (60, "1 hour"), (120, "2 hours")]
RECENTS = [(15, "15 minutes"), (60, "hour"), (180, "3 hours"), (720, "12 hours")]
KINDS = [("live", "Live teams only"), ("test", "Test traffic only")]

PARAMS = ("f", "p", "t", "st", "own", "wait", "kind", "recent")


def _ints(values):
    out = []
    for x in values:
        if str(x).isdigit() and int(x) not in out:
            out.append(int(x))
    return out


def _pick(value, choices):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n in [c for c, _ in choices] else None


class Filters:
    """The filters of one request, read from its query string (unknown values dropped)."""

    def __init__(self, v, GET, folder=""):
        self.v, self.folder = v, folder
        self.general = [k for k in GENERAL_KEYS if k in GET.getlist("f")]
        self.personas, self.teams, self.states = [], [], []
        self.own, self.wait, self.kind, self.recent = "", None, "", None
        if v.desk:
            self.personas = _ints(GET.getlist("p"))
            self.teams = _ints(GET.getlist("t"))
            self.states = [k for k in STATE_KEYS if k in GET.getlist("st")]
            own = GET.get("own") or ""
            self.own = own if own in ("me", "none", "any") or own.isdigit() else ""
            self.wait = _pick(GET.get("wait"), WAITS)
            self.kind = GET.get("kind") if GET.get("kind") in dict(KINDS) else ""
            self.recent = _pick(GET.get("recent"), RECENTS)

    # --- state ------------------------------------------------------------------------------
    def params(self, **change):
        """The filters as [(name, value)] for a URL, with change applied (name -> list or value)."""
        cur = {"f": self.general, "p": self.personas, "t": self.teams, "st": self.states, "own": self.own,
               "wait": self.wait, "kind": self.kind, "recent": self.recent}
        cur.update(change)
        out = []
        for name in PARAMS:
            val = cur[name]
            for x in (val if isinstance(val, list) else [val]):
                if x not in (None, ""):
                    out.append((name, x))
        return out

    @property
    def active(self):
        return bool(self.params())

    @property
    def desk_active(self):
        return bool(self.personas or self.teams or self.states or self.own or self.wait or self.kind or self.recent)

    @property
    def qs(self):
        """"&f=unread&..." to append to a "?folder=x" URL ("" with no filters)."""
        p = self.params()
        return "&" + urlencode(p) if p else ""

    def url(self, q="", **change):
        p = [("folder", self.folder)] + ([("q", q)] if q else []) + self.params(**change)
        return "?" + urlencode(p)

    # --- narrowing ----------------------------------------------------------------------------
    def narrow(self, qs):
        """Narrow a mailbox's Mail queryset to whole conversations matching the desk's persona,
        team and live/test filters (the row keeps its own latest message and counts)."""
        if not self.v.desk:
            return qs
        from .models import Sender
        from .views_inbox import persona_locals
        box = self.v.mails()
        if self.personas:
            locs = [loc for s in Sender.objects.filter(pk__in=self.personas) for loc in persona_locals(s)]
            qs = qs.filter(thread__in=box.filter(persona__in=locs).values("thread"))
        if self.teams:
            qs = qs.filter(thread__in=box.filter(desk_team_id__in=self.teams).values("thread"))
        if self.kind:
            qs = qs.filter(thread__in=box.filter(is_test=self.kind == "test").values("thread"))
        return qs

    def keep(self, r, now=None, skip=None):
        """Whether a conversation row passes the row-level filters (skip: a filter group left out,
        for the counts)."""
        now = now or timezone.now()
        for k in self.general:
            if k != skip and not _general(k, r):
                return False
        st = r.get("state")
        if self.states and skip != "st":
            if not st or not any(_state(k, st) for k in self.states):
                return False
        if self.own:
            if not st:
                return False
            if self.own == "me" and not st["mine"]:
                return False
            if self.own == "none" and st["owner_id"]:
                return False
            if self.own == "any" and not st["owner_id"]:
                return False
            if self.own.isdigit() and st["owner_id"] != int(self.own):
                return False
        if self.wait:
            if not (st and st["waiting"] and st["since"] and st["since"] <= now - timedelta(minutes=self.wait)):
                return False
        if self.recent and r["latest"].created_at < now - timedelta(minutes=self.recent):
            return False
        return True

    def apply(self, rows, keep_thread=None):
        """The rows that pass (keep_thread: a conversation kept whatever, the one being read)."""
        if not self.active:
            return rows
        now = timezone.now()
        return [r for r in rows if r["thread"] == keep_thread or self.keep(r, now)]

    # --- what the list shows -----------------------------------------------------------------
    def bar(self, rows, q=""):
        """The filter bar: quick filter chips (with how many would match) and, on the desk, the
        panel's choices and the active filters as removable chips. rows: the unfiltered rows."""
        now = timezone.now()
        chips = []
        for key, name in GENERAL:
            if key == "unanswered" and self.folder in ("sent", "drafts"):
                continue
            if key == "unread" and self.folder == "drafts":
                continue
            on = key in self.general
            others = [r for r in rows if self.keep(r, now, skip=key)] if on else None
            n = sum(1 for r in (others if on else rows) if _general(key, r) and (on or self.keep(r, now)))
            nxt = [k for k in self.general if k != key] if on else self.general + [key]
            chips.append({"key": key, "name": name, "on": on, "n": n, "href": self.url(q, f=nxt)})
        out = {"chips": chips, "active": self.active, "clear": self.url(q, **{k: [] for k in PARAMS}),
               "desk": None}
        if self.v.desk:
            out["desk"] = self._desk_bar(rows, now, q)
        return out

    def _desk_bar(self, rows, now, q):
        from .models import Sender, Team
        base = [r for r in rows if self.keep(r, now, skip="st")]
        states = [{"key": k, "name": name, "on": k in self.states,
                   "n": sum(1 for r in base if r.get("state") and _state(k, r["state"]))} for k, name in STATES]
        personas = [{"id": s.id, "name": s.display_name or s.local_part, "colour": desk.colour(s)[0],
                     "on": s.id in self.personas} for s in desk.personas().order_by("display_name", "local_part")]
        teams = [{"id": t.id, "name": t.name, "test": t.is_test, "on": t.id in self.teams}
                 for t in Team.objects.filter(active=True).order_by("name")]
        people = desk.assignees(self.v.user)
        tags = []  # the active desk filters, each removable
        names = {s.id: s.display_name or s.local_part for s in Sender.objects.filter(pk__in=self.personas)}
        for pid in self.personas:
            tags.append({"name": names.get(pid, "?"), "href": self.url(q, p=[x for x in self.personas if x != pid])})
        tnames = {t["id"]: t["name"] for t in teams}
        for tid in self.teams:
            tags.append({"name": tnames.get(tid, "?"), "href": self.url(q, t=[x for x in self.teams if x != tid])})
        for k in self.states:
            if k not in ("attention", "urgent"):  # those two are presets
                tags.append({"name": dict(STATES)[k], "href": self.url(q, st=[x for x in self.states if x != k])})
        if self.own and self.own not in ("me", "none"):
            who = {"me": "Assigned to me", "none": "Nobody has it", "any": "Someone has it"}.get(self.own)
            if not who:
                who = "Assigned to " + next((a["name"] for a in people if str(a["id"]) == self.own), "?")
            tags.append({"name": who, "href": self.url(q, own="")})
        if self.wait and self.wait != 30:
            tags.append({"name": f"Waiting over {dict(WAITS)[self.wait]}", "href": self.url(q, wait=None)})
        if self.kind:
            tags.append({"name": dict(KINDS)[self.kind], "href": self.url(q, kind="")})
        if self.recent:
            tags.append({"name": f"Active in the last {dict(RECENTS)[self.recent]}", "href": self.url(q, recent=None)})
        def preset(name, **change):
            key, val = next(iter(change.items()))
            cur = getattr(self, {"st": "states", "own": "own", "wait": "wait"}[key])
            if key == "st":
                on = val[0] in cur
                change = {"st": [x for x in cur if x not in val] if on else cur + val}
            else:
                on = cur == val
                change = {key: None if on else val}
            return {"name": name, "on": on, "href": self.url(q, **change)}
        # One click to the desk's most used views (each toggles).
        presets = [preset("Needs attention", st=["attention"]), preset("Urgent", st=["urgent"]),
                   preset("Mine", own="me"), preset("Nobody has it", own="none"),
                   preset("Waiting 30 min+", wait=30)]
        return {"presets": presets, "states": states, "personas": personas, "teams": teams, "people": people, "own": self.own,
                "wait": self.wait, "waits": WAITS, "kind": self.kind, "kinds": KINDS, "recent": self.recent,
                "recents": RECENTS, "tags": tags, "general": self.general, "on": self.desk_active,
                "n_on": len(self.personas) + len(self.teams) + len(self.states) + bool(self.own) + bool(self.wait)
                + bool(self.kind) + bool(self.recent)}

    def describe(self):
        """For MailAction detail: the filters in use."""
        out = {}
        for name, val in self.params():
            out.setdefault(name, []).append(str(val))
        return out


def _general(key, r):
    if key == "unread":
        return r["unread"]
    if key == "starred":
        return r["starred"]
    if key == "files":
        return r["has_files"]
    if key == "unanswered":
        return bool(r.get("awaiting"))
    return True


def _state(key, st):
    if key in (desk.ATTENTION, desk.QUIET):
        return st["section"] == key
    if key == "waiting":
        return st["waiting"]
    if key == "urgent":
        return bool(st["triage"] and st["triage"]["urgency"] == "high")
    if key == "follow":
        return bool(st["follow"])
    if key == "npc":
        return bool(st["npc"])
    if key == "done":
        return bool(st["done_by"])
    return False

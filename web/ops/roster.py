"""Roster import (People & logins → Import a roster): a spreadsheet (.xlsx) or CSV with a header
row (name, email, team, and optionally role) sets people's personal emails and teams, and
adds the people not in the app yet. Planned first (who is matched, what changes), applied after
ExCon checks the plan. The email column is the personal email (Person.personal_email)."""

import csv
import io
import re
import unicodedata

import openpyxl

from . import render
from .models import ROLES, Person, PushSubscription, Team

HEADERS = {
    "name": ("name", "full name", "participant", "person"),
    "email": ("personal email", "email", "e-mail", "email address", "mail"),
    "team": ("team", "team code", "group"),
    "role": ("role",),
}


def _clean(value):
    return " ".join(str(value).split()) if value is not None else ""


def key(name):
    """A name compared without case, accents, punctuation or extra spaces."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def team_code(cell):
    """"A", "a", "Team A" -> "a"."""
    s = re.sub(r"^team\s+", "", _clean(cell).lower())
    return re.sub(r"[^a-z0-9-]", "", s)


def read(name, data):
    """The rows of an uploaded roster: [{"line", "name", "email", "team", "role"}]. Raises
    ValueError when there's no header row with a name column."""
    if name.lower().endswith((".xlsx", ".xlsm")):
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        grid = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    else:
        grid = list(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))))
    cols, start = None, 0
    for i, row in enumerate(grid[:20]):
        cells = [_clean(c).lower() for c in row]
        found = {}
        for field, names in HEADERS.items():
            for n in names:  # first the most specific name ("personal email" before "email")
                if n in cells and field not in found:
                    found[field] = cells.index(n)
        if "name" in found:
            cols, start = found, i + 1
            break
    if not cols:
        raise ValueError("No header row with a Name column (and Email, Team) in the first 20 rows.")
    out = []
    for i, row in enumerate(grid[start:], start + 1):
        def cell(field):
            j = cols.get(field)
            return _clean(row[j]) if j is not None and j < len(row) else ""
        if not cell("name"):
            continue
        out.append({"line": i, "name": cell("name"), "email": cell("email").strip().lower(),
                    "team": team_code(cell("team")), "role": cell("role").lower()})
    return out


def _match(row, people, taken):
    """The Person a roster row is (same personal or channel email, else the same name, else
    a name whose words are all in the other's in the same team), or None."""
    free = [p for p in people if p.pk not in taken]
    if row["email"]:
        for p in free:
            if row["email"] in (p.personal_email.lower(), p.email.lower()):
                return p, "same email"
    k = key(row["name"])
    same = [p for p in free if key(p.name) == k]
    if len(same) == 1:
        return same[0], "same name"
    words = set(k.split())
    near = [p for p in free if (not row["team"] or (p.team and p.team.code == row["team"])) and len(words) > 1
            and (set(key(p.name).split()) <= words or words <= set(key(p.name).split()))]
    if len(near) == 1:
        return near[0], "similar name"
    return None, ""


def plan(rows):
    """What importing rows would do: (items, warnings). An item: the row, its Person (or None
    when it adds one), how it was matched and the changes ([(field, old, new)])."""
    people = list(Person.objects.exclude(role="desk").select_related("team"))
    teams = {t.code: t for t in Team.objects.all()}
    items, warnings, taken, seen = [], [], set(), set()
    for row in rows:
        problems = []
        if row["email"] and (not render.EMAIL.match(row["email"]) or render.PLACEHOLDER_DOMAIN.search(row["email"])):
            problems.append(f"'{row['email']}' is not a usable address")
        if row["email"] and row["email"] in seen:
            problems.append(f"{row['email']} is on another line too")
        seen.add(row["email"])
        p, how = _match(row, people, taken)
        if p:
            taken.add(p.pk)
        changes = []
        if row["email"] and (not p or p.personal_email.lower() != row["email"]):
            changes.append(("personal email", p.personal_email if p else "", row["email"]))
        if row["team"] and (not p or not p.team or p.team.code != row["team"]):
            changes.append(("team", p.team.name if p and p.team else "",
                            teams[row["team"]].name if row["team"] in teams else f"Team {row['team'].upper()} (new)"))
        if row["role"] in ("tl", "team leader", "leader") and (not p or p.role != "tl"):
            changes.append(("role", ROLES.get(p.role, p.role) if p else "", ROLES["tl"]))
        if not p and not row["team"]:
            problems.append("no team")
        items.append({"row": row, "person": p, "how": how, "changes": changes, "problems": problems,
                      "action": "skip" if problems else "add" if not p else "update" if changes else "same"})
    left = [p for p in people if p.pk not in taken and p.active and p.team and not p.team.is_test]
    if left:
        warnings.append(f"Not in the file, left as they are: {', '.join(p.name for p in left)}.")
    return items, warnings


def apply(items, make_logins, ensure_account):
    """Carry out a plan (from plan(), as rows: re-planned here so nothing stale is written).
    Returns (added, updated, new logins [[name, team, username, password]], errors)."""
    from .views_excon import _leader_problem
    teams = {t.code: t for t in Team.objects.all()}
    added = updated = 0
    creds, errors = [], []
    for it in items:
        if it["action"] == "skip" or (it["action"] == "same" and (it["person"].user or not make_logins)):
            continue
        row = it["row"]
        p = it["person"] or Person(name=row["name"], role="member")
        if it["action"] in ("add", "update"):
            if row["team"] and row["team"] not in teams:
                teams[row["team"]] = Team.objects.create(code=row["team"], name=f"Team {row['team'].upper()}",
                                                         sort=len(teams))
            if row["email"]:
                p.personal_email = row["email"]
            if row["team"]:
                p.team = teams[row["team"]]
            old_role = p.role if p.pk else None
            if row["role"] in ("tl", "team leader", "leader"):
                p.role, p.team_role = "tl", None
            problem = _leader_problem(p)
            if problem:
                errors.append(f"line {row['line']}: {problem}")
                continue
            new = not p.pk
            p.save()
            if old_role and old_role != p.role:  # a different mailbox from now on: its devices subscribe again
                PushSubscription.objects.filter(person=p).delete()
            added, updated = added + new, updated + (not new)
        if make_logins and not p.user:
            pw = ensure_account(p)
            creds.append([p.name, p.team.name if p.team else "", p.user.username, pw])
    return added, updated, creds, errors

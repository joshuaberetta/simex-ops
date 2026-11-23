"""Senders import (Senders → Import a list of senders): a spreadsheet (.xlsx) or CSV with a
header row, one persona per row: its SIMEX Mail address (Alias), optionally its username, the
name shown, ExCon's notes (Organisation / use), a category and Active. Planned first (which
sender each row is, what it adds and what it changes, flagging values that replace ones set
here), applied after ExCon checks the plan. Export (the same columns) gives the file back.

A row is matched to a sender by username (the Username column, else the address's username),
else, with no Username column, by its SIMEX Mail address. Empty cells leave a value as it is.
Usernames are never renamed here (that is the Senders tab, with rename in injects)."""

import csv
import io
import re

import openpyxl
from django.conf import settings
from django.db.models import Count

from . import addresses, render as rnd
from .models import Inject, Sender, Team

# Most specific names first: a column is claimed by the first field that names it.
HEADERS = {
    "username": ("username", "user name", "persona username"),
    "address": ("address in simex mail", "simex mail address", "alias", "sender alias", "address", "email",
                "email address", "e-mail"),
    "display_name": ("display name", "name shown", "name", "persona"),
    "notes": ("organisation / use", "organization / use", "organisation/use", "organization/use", "notes",
              "description", "organisation", "organization", "use"),
    "category": ("category", "type", "group"),
    "active": ("active",),
}
EXPORT = ["Username", "Address in SIMEX Mail", "Display name", "Organisation / use", "Category", "Active"]
FIELD_LABELS = {"display_name": "name shown", "address": "SIMEX Mail address", "notes": "organisation / use",
                "category": "category", "active": "active"}
YES, NO = ("yes", "y", "true", "1", "x", "active"), ("no", "n", "false", "0", "inactive")


def _clean(value):
    return " ".join(str(value).split()) if value is not None else ""


def read(name, data):
    """The rows of an uploaded list: [{"line", "username", "address", "display_name", "notes",
    "category", "active", "explicit"}] ("explicit": the file has a Username column). Raises
    ValueError when there's no header row with an Alias/Address or Username column."""
    if name.lower().endswith((".xlsx", ".xlsm")):
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        grid = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    else:
        grid = list(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))))
    cols, start = None, 0
    for i, row in enumerate(grid[:20]):
        cells = [_clean(c).lower() for c in row]
        found, used = {}, set()
        for field, names in HEADERS.items():
            for n in names:
                if n in cells and cells.index(n) not in used:
                    found[field] = cells.index(n)
                    used.add(found[field])
                    break
        if "address" in found or "username" in found:
            cols, start = found, i + 1
            break
    if not cols:
        raise ValueError("No header row with an Alias (the SIMEX Mail address) or Username column in the first 20 rows.")
    out = []
    for i, row in enumerate(grid[start:], start + 1):
        def cell(field):
            j = cols.get(field)
            return _clean(row[j]) if j is not None and j < len(row) else ""
        if not cell("address") and not cell("username"):
            continue
        out.append({"line": i, "username": re.sub(r"@.*$", "", cell("username").lower()).replace(" ", ""),
                    "address": cell("address").lower().replace(" ", ""), "display_name": cell("display_name"),
                    "notes": cell("notes"), "category": cell("category"), "active": cell("active").lower(),
                    "explicit": "username" in cols})
    return out


def _active(value):
    return True if value in YES else False if value in NO else None


def _split(address, username):
    """(inapp_local, domain) as stored: empty when it is the username / MAIL_DOMAIN."""
    local, _, domain = address.partition("@")
    return ("" if local == username else local), ("" if domain == settings.MAIL_DOMAIN.lower() else domain)


def plan(rows):
    """What importing rows would do: (items, missing). An item: the row, its Sender (None: added),
    how it was matched, the changes [{"field", "old", "new", "replaces"}] ("replaces": a value set
    here is overwritten, not just filled in), problems, and the action add | update | same | skip.
    missing: the active senders not in the file, with the injects using each."""
    senders = list(Sender.objects.all())
    by_local = {x.local_part: x for x in senders}
    by_addr = {x.inapp_address: x for x in senders}
    team_addrs = {addresses.team_address(t): t for t in Team.objects.all()}
    items, taken, seen_local, seen_addr = [], set(), {}, {}
    for row in rows:
        problems = []
        addr = row["address"]
        if addr and "@" not in addr:  # just a username in the Alias column
            addr = f"{addr}@{settings.MAIL_DOMAIN}".lower()
        local, _, domain = addr.partition("@")
        username = row["username"] or local
        x, how = by_local.get(username), "same username"
        if not x and addr and not row["explicit"] and addr in by_addr:
            x, how = by_addr[addr], "same address"
            username = x.local_part
        if not rnd.LOCAL.match(username.replace("{team}", "x")):
            problems.append(f"'{username}' is not a valid username (letters, digits, . _ + - and {{team}})")
        if addr and (not rnd.LOCAL.match(local.replace("{team}", "x")) or not rnd.DOMAIN.match(domain)
                     or domain.endswith(f"teams.{settings.MAIL_DOMAIN}")):
            problems.append(f"'{row['address']}' is not a valid address")
        elif addr:
            other = by_addr.get(addr)
            if other and other is not x:
                problems.append(f"{addr} is already the SIMEX Mail address of {other.local_part}")
            if addr in team_addrs:
                problems.append(f"{addr} is {team_addrs[addr].name}'s address in SIMEX Mail")
        for key, seen, what in ((username, seen_local, "username"), (addr, seen_addr, "address")):
            if key and key in seen:
                problems.append(f"the same {what} as line {seen[key]}")
            seen.setdefault(key, row["line"])
        if x and x.pk in taken:
            problems.append(f"{x.local_part} is on another line too")
        if x:
            taken.add(x.pk)
        changes = []

        def change(field, old, new, replaces=None):
            if new != old:
                changes.append({"field": FIELD_LABELS[field], "old": old, "new": new,
                                "replaces": bool(x and old) if replaces is None else replaces})

        if x:
            if addr:  # the default address (username@MAIL_DOMAIN) wasn't set here: not flagged
                change("address", x.inapp_address, addr, replaces=bool(x.inapp_local or x.domain))
            for f in ("display_name", "notes", "category"):
                if row[f]:
                    change(f, getattr(x, f), row[f])
            if _active(row["active"]) is not None and _active(row["active"]) != x.active:
                change("active", "yes" if x.active else "no", "yes" if _active(row["active"]) else "no")
        else:
            change("address", "", addr or f"{username}@{settings.MAIL_DOMAIN}".lower())
            for f in ("display_name", "notes", "category"):
                change(f, "", row[f])
            if _active(row["active"]) is False:
                change("active", "", "no")
        items.append({"row": row, "sender": x, "how": how if x else "", "username": username, "address": addr,
                      "changes": changes, "problems": problems,
                      "replaces": any(c["replaces"] for c in changes),
                      "action": "skip" if problems else "add" if not x else "update" if changes else "same"})
    usage = dict(Inject.objects.values_list("sender_local").annotate(n=Count("id")).values_list("sender_local", "n"))
    missing = [{"sender": x, "usage": usage.get(x.local_part, 0)}
               for x in senders if x.pk not in taken and x.active]
    return items, missing


def apply(items, lines, deactivate_missing=()):
    """Carry out the plan's add and update items on the given lines (re-planned by the caller, so
    nothing stale is written), and untick active on the senders in deactivate_missing (ids).
    Returns (added, updated, deactivated)."""
    added = updated = 0
    for it in items:
        if it["action"] not in ("add", "update") or it["row"]["line"] not in lines:
            continue
        row, x = it["row"], it["sender"] or Sender(local_part=it["username"])
        if it["address"] or not x.pk:
            x.inapp_local, x.domain = _split(it["address"] or f"{it['username']}@{settings.MAIL_DOMAIN}".lower(),
                                             x.local_part)
        for f in ("display_name", "notes", "category"):
            if row[f]:
                setattr(x, f, row[f])
        if _active(row["active"]) is not None:
            x.active = _active(row["active"])
        new = not x.pk
        x.save()
        added, updated = added + new, updated + (not new)
    deactivated = Sender.objects.filter(pk__in=list(deactivate_missing), active=True, team_leader=False).update(active=False)
    return added, updated, deactivated


def export():
    """The senders as an .xlsx in the import's columns."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Senders"
    ws.append(EXPORT)
    for x in Sender.objects.all():
        ws.append([x.local_part, x.inapp_address, x.display_name, x.notes, x.category, "yes" if x.active else "no"])
    for col, width in zip("ABCDEF", (24, 34, 34, 50, 16, 8)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

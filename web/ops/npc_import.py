"""AI persona profiles in bulk (AI personas → Import profiles): a spreadsheet (.xlsx) or CSV
with a header row, one persona per row, keyed by its username (or its SIMEX Mail address):
organisation, role, personality and voice, what they know and may share, boundaries, timing,
office hours, and optionally whether it answers and sends on its own. Planned first (which
persona each row is, whether it gives it a profile or changes one, flagging values that replace
ones set here), applied to the rows ExCon ticks. Export (the same columns) gives the file back.

Only personas already on the Senders tab (add them there, or with Senders → Import, first);
never the team leader persona or a {team} one. Empty cells leave a value as it is. New profiles
are off unless the file says Answers on its own: yes. What a file puts in a profile reaches the
teams through the persona's answers (and shapes Suggest a reply and ChatSIMEX), so it must hold
only what the teams may learn, never control-side material."""

import csv
import io
import re

import openpyxl

from . import npc
from .models import NpcProfile, Sender

TEXT_MAX = 20_000
# Most specific names first: a column is claimed by the first field that names it.
HEADERS = {
    "username": ("username", "user name", "persona username", "persona"),
    "address": ("address in simex mail", "simex mail address", "alias", "address", "email", "email address"),
    "display_name": ("name shown", "display name", "name"),  # for the reader only: Senders owns it
    "organisation": ("organisation", "organization"),
    "role": ("role", "title", "job title"),
    "personality": ("personality and voice", "personality", "voice"),
    "knowledge": ("what they know and may share", "what they know", "knowledge"),
    "boundaries": ("boundaries", "limits"),
    "timing": ("timing", "timing preset"),
    "min_minutes": ("custom: from (min)", "from (min)", "min minutes", "minimum minutes"),
    "max_minutes": ("custom: to (min)", "to (min)", "max minutes", "maximum minutes"),
    "office_from": ("office hours from", "office from"),
    "office_to": ("office hours to", "office to"),
    "auto_send": ("sends straight away", "sends its answers straight away", "auto send"),
    "enabled": ("answers on its own", "enabled", "ai answers"),
}
EXPORT = ["Username", "Name shown", "Organisation", "Role", "Personality and voice", "What they know and may share",
          "Boundaries", "Timing", "Custom: from (min)", "Custom: to (min)", "Office hours from", "Office hours to",
          "Sends straight away", "Answers on its own"]
TEXT = ("organisation", "role", "personality", "knowledge", "boundaries")
LABELS = {"organisation": "organisation", "role": "role", "personality": "personality and voice",
          "knowledge": "what they know", "boundaries": "boundaries", "timing": "timing",
          "office": "office hours", "auto_send": "sends straight away", "enabled": "answers on its own"}
YES, NO = ("yes", "y", "true", "1", "x", "on"), ("no", "n", "false", "0", "off")
TIMING_NAMES = {**{k: k for k in npc.TIMINGS}, **{t["label"].lower(): k for k, t in npc.TIMINGS.items()}}


def _line(value):
    return " ".join(str(value).split()) if value is not None else ""


def _text(value):
    """A long cell: line breaks kept (knowledge is one fact per line), trailing spaces dropped."""
    if value is None:
        return ""
    lines = [ln.rstrip() for ln in str(value).replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:TEXT_MAX]


def read(name, data):
    """The rows of an uploaded file: [{"line", "username", "address", field: text...}]. Raises
    ValueError when there's no header row with a Username or Address column."""
    if name.lower().endswith((".xlsx", ".xlsm")):
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        grid = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    else:
        grid = list(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))))
    cols, start = None, 0
    for i, row in enumerate(grid[:20]):
        cells = [_line(c).lower() for c in row]
        found, used = {}, set()
        for field, names in HEADERS.items():
            for n in names:
                if n in cells and cells.index(n) not in used:
                    found[field] = cells.index(n)
                    used.add(found[field])
                    break
        if "username" in found or "address" in found:
            cols, start = found, i + 1
            break
    if not cols:
        raise ValueError("No header row with a Username (or Address in SIMEX Mail) column in the first 20 rows.")
    out = []
    for i, row in enumerate(grid[start:], start + 1):
        def cell(field, long=False):
            j = cols.get(field)
            v = row[j] if j is not None and j < len(row) else None
            return _text(v) if long else _line(v)
        if not cell("username") and not cell("address"):
            continue
        user = cell("username").lower().replace(" ", "")
        rec = {"line": i, "username": re.sub(r"@.*$", "", user),  # an address in it: matched by it too
               "address": (cell("address") or (user if "@" in user else "")).lower().replace(" ", ""),
               "display_name": cell("display_name")}
        for f in TEXT:
            rec[f] = cell(f, long=f in ("personality", "knowledge", "boundaries"))
        for f in ("timing", "min_minutes", "max_minutes", "office_from", "office_to", "auto_send", "enabled"):
            rec[f] = cell(f).lower()
        out.append(rec)
    return out


def _yes(value):
    return True if value in YES else False if value in NO else None


def _num(value):
    try:
        return float(value.replace(",", "."))
    except (AttributeError, ValueError):
        return None


def _hour(value):
    """An hour from "8", "08", "8:00", "08:30" (minutes dropped); None if empty or not one."""
    m = re.fullmatch(r"(\d{1,2})(?:[:.h]\d{2})?", value or "")
    return int(m.group(1)) if m and int(m.group(1)) <= 23 else None


def _timing_text(t):
    return t["label"] if t["timing"] != "custom" else f"custom {t['min']:g}–{t['max']:g} min"


def _wanted_timing(row, p):
    """The row's timing as {"timing", "min", "max"}, None if the row leaves it; or a problem."""
    key = TIMING_NAMES.get(row["timing"]) if row["timing"] else None
    if row["timing"] and not key:
        return None, f"timing '{row['timing']}' is not one of {', '.join(k for k in npc.TIMINGS)}"
    lo, hi = _num(row["min_minutes"]), _num(row["max_minutes"])
    if (row["min_minutes"] and lo is None) or (row["max_minutes"] and hi is None):
        return None, "the custom minutes are not numbers"
    if key and key != "custom":
        t = npc.TIMINGS[key]
        return {"timing": key, "min": t["min"], "max": t["max"], "label": t["label"]}, None
    if key == "custom" or lo is not None or hi is not None:
        lo = max(0.0, lo if lo is not None else (p.min_minutes if p else 5))
        hi = max(lo, hi if hi is not None else (p.max_minutes if p else 20))
        return {"timing": "custom", "min": lo, "max": hi, "label": "Custom"}, None
    return None, None


def plan(rows):
    """What importing rows would do: items, each the row, its Sender, its profile (None: a new
    one), the changes [{"field", "old", "new", "replaces"}], problems, and the action
    add | update | same | skip."""
    senders = list(Sender.objects.select_related("npc"))
    by_local = {x.local_part: x for x in senders}
    by_addr = {x.inapp_address: x for x in senders}
    items, seen = [], {}
    for row in rows:
        problems, x = [], None
        if row["username"]:
            x = by_local.get(row["username"])
        if not x and row["address"]:
            x = by_addr.get(row["address"] if "@" in row["address"] else "") or by_local.get(row["address"].partition("@")[0])
        name = row["username"] or row["address"]
        if not x:
            problems.append(f"{name} is not on the Senders tab: add it there first")
        elif x.team_leader:
            problems.append(f"{x.local_part} is the team leader persona (the leaders play it)")
        elif "{" in x.local_part:
            problems.append(f"{x.local_part} is filled in per team ({{team}}): an AI persona needs one address")
        if x and x.pk in seen:
            problems.append(f"{x.local_part} is on line {seen[x.pk]} too")
        if x:
            seen.setdefault(x.pk, row["line"])
        p = getattr(x, "npc", None) if x else None
        timing, problem = _wanted_timing(row, p)
        if problem:
            problems.append(problem)
        changes = []

        def change(field, old, new, replaces):
            if new != old:
                changes.append({"field": LABELS[field], "old": old, "new": new, "replaces": replaces})

        for f in TEXT:
            if row[f]:
                old = getattr(p, f) if p else ""
                change(f, old, row[f][:200] if f in ("organisation", "role") else row[f], bool(old))
        if timing:
            old = {"timing": p.timing, "min": p.min_minutes, "max": p.max_minutes,
                   "label": (npc.TIMINGS.get(p.timing) or npc.TIMINGS["custom"])["label"]} if p else None
            if not old or (old["timing"], old["min"], old["max"]) != (timing["timing"], timing["min"], timing["max"]):
                change("timing", _timing_text(old) if old else "", _timing_text(timing),
                       bool(p) and p.timing != "responsive")
        if row["office_from"] or row["office_to"]:
            a, b = _hour(row["office_from"]), _hour(row["office_to"])
            if a is None or b is None:
                problems.append("office hours need both hours, 0 to 23")
            else:
                old = f"{p.office_from:02d}–{p.office_to:02d}" if p and p.office_from is not None and p.office_to is not None else ""
                change("office", old, f"{a:02d}–{b:02d}", bool(old))
        for f, default in (("auto_send", True), ("enabled", False)):
            want = _yes(row[f])
            if row[f] and want is None:
                problems.append(f"{LABELS[f]}: '{row[f]}' is not yes or no")
            elif want is not None:
                have = getattr(p, f) if p else default
                if want != have or (not p and want):
                    change(f, ("yes" if have else "no") if p else "", "yes" if want else "no", bool(p))
        items.append({"row": row, "sender": x, "profile": p, "changes": changes, "problems": problems,
                      "timing": timing, "replaces": any(c["replaces"] for c in changes),
                      "turns_on": _yes(row["enabled"]) is True and not (p and p.enabled),
                      "action": "skip" if problems else "add" if not p else "update" if changes else "same"})
    return items


def apply(items, lines):
    """Carry out the plan's add and update items on the given lines (re-planned by the caller, so
    nothing stale is written). Returns (added, updated)."""
    added = updated = 0
    for it in items:
        if it["action"] not in ("add", "update") or it["row"]["line"] not in lines:
            continue
        row, p = it["row"], it["profile"]
        new = p is None
        if new:
            p = NpcProfile(sender=it["sender"])
            npc.apply_timing(p, "responsive")
        for f in TEXT:
            if row[f]:
                setattr(p, f, row[f][:200] if f in ("organisation", "role") else row[f])
        t = it["timing"]
        if t:
            p.timing, p.min_minutes, p.max_minutes = t["timing"], t["min"], t["max"]
            if t["timing"] != "custom":
                npc.apply_timing(p, t["timing"])
        if row["office_from"] or row["office_to"]:
            p.office_from, p.office_to = _hour(row["office_from"]), _hour(row["office_to"])
        for f in ("auto_send", "enabled"):
            if _yes(row[f]) is not None:
                setattr(p, f, _yes(row[f]))
        if p.gen_status in ("done", "failed"):
            p.gen_status = ""  # written by ExCon's file now
        p.save()
        added, updated = added + new, updated + (not new)
    return added, updated


def export():
    """The profiles as an .xlsx in the import's columns, then the personas that could have one,
    with only their username and name (fill them in and import it back)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AI personas"
    ws.append(EXPORT)
    for p in NpcProfile.objects.select_related("sender"):
        s = p.sender
        ws.append([s.local_part, s.display_name, p.organisation, p.role, p.personality, p.knowledge, p.boundaries,
                   p.timing, p.min_minutes if p.timing == "custom" else None,
                   p.max_minutes if p.timing == "custom" else None, p.office_from, p.office_to,
                   "yes" if p.auto_send else "no", "yes" if p.enabled else "no"])
    for s in Sender.objects.filter(team_leader=False, npc__isnull=True).exclude(local_part__contains="{"):
        ws.append([s.local_part, s.display_name])
    for col, width in zip("ABCDEFGHIJKLMN", (20, 30, 28, 28, 60, 70, 50, 14, 10, 10, 10, 10, 10, 10)):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=2):
        for c in row[4:7]:
            c.alignment = openpyxl.styles.Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "B2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

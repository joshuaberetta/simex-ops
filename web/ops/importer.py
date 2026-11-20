"""Import email injects from the master events list (MEL-MIL.xlsx) or from an injects.json
export (undac-simex registers/injects.json, or the bundle from its export script).

Only participant-facing fields are read. Expected outcomes, story, theme, key figures,
relevance and the ExCon description are dropped here and never stored.
"""

import datetime
import io
import json
import re
from datetime import timezone

from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction

from .models import IMPORT_FIELDS, Inject, Sender, audit

# MEL column header -> raw field (the sheet's own spelling, newline included).
MEL_COLUMNS = {
    "Inject #": "inject_id", "Time": "time", "Inject \nmethod": "method", "Content": "content",
    "Supporting documents": "supporting_documents", "Send date": "send_date", "Send time": "send_time",
    "Mailbox": "mailbox", "Sender alias": "sender", "Recipients": "recipients", "Subject": "subject",
}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december"]
DAY = re.compile(r"(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\s+)?(\d{1,2})(?:\s*(?:&|and)\s*(\d{1,2}))?"
                 r"\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?", re.I)
HM = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)|(?<!\d)([01]\d|2[0-3])([0-5]\d)(?!\d)")
SCENARIO_YEAR = 2035


class ImportError_(ValueError):
    pass


def _text(v):
    if v is None:
        return None
    if isinstance(v, datetime.time):
        return v.strftime("%H:%M")
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    s = str(v).strip()
    return s or None


def _hm(s):
    m = HM.search(s or "")
    if not m:
        return None
    h, mi = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return f"{int(h):02d}:{mi}"


def _day_header(value):
    """Dates covered by a day-header row in the Time column, or None (same rules as
    undac-simex scripts/parse_mel.py)."""
    if isinstance(value, datetime.datetime) and value.year >= 2000 and value.time() == datetime.time(0, 0):
        return [value.date()], ""
    if not isinstance(value, str):
        return None
    m = DAY.search(value)
    if not m:
        return None
    year = int(m.group(4) or SCENARIO_YEAR)
    month = MONTHS.index(m.group(3).lower()) + 1
    days = [int(m.group(1))] + ([int(m.group(2))] if m.group(2) else [])
    return [datetime.date(year, month, d) for d in days], value[m.end():].strip()


def read_mel_xlsx(data: bytes):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    if "MEL-MIL" not in wb.sheetnames:
        raise ImportError_("No sheet called MEL-MIL in this workbook.")
    rows = wb["MEL-MIL"].iter_rows(values_only=True)
    header = None
    for row in rows:
        if row and "Inject #" in row:
            header = [_text(c) for c in row]
            break
    if not header:
        raise ImportError_("No header row with 'Inject #' found in the MEL-MIL sheet.")
    cols = {MEL_COLUMNS[h]: i for i, h in enumerate(header) if h in MEL_COLUMNS}
    if "inject_id" not in cols or "time" not in cols:
        raise ImportError_("The MEL-MIL sheet is missing the 'Inject #' or 'Time' column.")

    out, days, order = [], [], {}
    for row in rows:
        cells = {k: _text(row[i]) if i < len(row) else None for k, i in cols.items()}
        raw_time = row[cols["time"]] if cols["time"] < len(row) else None
        time_cell = cells.get("time") or ""
        if hdr := _day_header(raw_time):
            days, time_cell = hdr
            order = {}
            if not cells.get("content") and not cells.get("sender"):
                continue
        elif isinstance(raw_time, datetime.datetime) and raw_time.year < 1950:
            time_cell = raw_time.strftime("%H:%M")
        if not cells.get("inject_id"):
            continue
        group = cells["inject_id"].split("-")[0]
        day = None
        if days:
            order.setdefault(group, len(order))
            day = days[min(order[group], len(days) - 1)]
        t = _hm(time_cell)
        cells["scenario_dt"] = f"{day.isoformat()}T{t}" if day and t else None
        out.append(cells)
    return out


def read_json(data: bytes):
    try:
        items = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ImportError_(f"Not valid JSON: {e}") from e
    if isinstance(items, dict):
        items = items.get("injects", [])
    if not isinstance(items, list):
        raise ImportError_("Expected a list of injects.")
    return items


def _send_at(send_date, send_time):
    """MEL send date + time (exercise timezone) -> naive UTC ISO string."""
    if not send_date:
        return None
    try:
        d = datetime.date.fromisoformat(send_date[:10])
    except ValueError:
        return None
    t = _hm(send_time or "")
    if not t:  # a date without a readable time ("TBC", "after 1-15"): leave it to ExCon, never midnight
        return None
    local = datetime.datetime.combine(d, datetime.time.fromisoformat(t), tzinfo=ZoneInfo(settings.TIME_ZONE))
    return local.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="minutes")


def sender_domain(sender):
    """The domain of the MEL's sender address (in-app personas show it), if it is one."""
    domain = (sender or "").strip().lower().rpartition("@")[2] if "@" in (sender or "") else ""
    return domain if re.fullmatch(r"([a-z0-9-]+\.)+[a-z]{2,}", domain) else None


def sender_local(sender):
    if not sender:
        return None
    local = sender.split("@")[0].strip()
    local = re.sub(r"\s+", "", local).lower()
    return local or None


def delivery_for(label):
    l = (label or "").lower()
    if "each team" in l:
        return "each_team"
    if "all teams" in l:
        return "all_bcc"
    if "individual" in l:
        return "each_person"
    return "each_team"


def is_email(raw):
    return bool(raw.get("sender")) or "mail" in (raw.get("method") or "").lower()


def normalise(raw_items):
    """Raw MEL rows or injects.json records -> import records (participant-facing fields)."""
    out, seen = [], {}
    for seq, raw in enumerate(raw_items):
        if not raw.get("inject_id") or not is_email(raw):
            continue
        mel_id = re.sub(r"\s+", " ", raw["inject_id"]).strip()
        # A few ids repeat in the MEL; later ones get -b, -c...
        n = seen.get(mel_id, 0)
        seen[mel_id] = n + 1
        if n:
            mel_id = f"{mel_id}-{chr(ord('a') + n)}"
        docs = [d.strip() for d in (raw.get("supporting_documents") or "").splitlines()]
        docs = [d for d in docs if d and d.lower() not in ("none", "n/a", "-")]
        out.append({  # cut to the field sizes (Postgres rejects longer values)
            "mel_id": mel_id[:40],
            "seq": seq,
            "method": _cut(raw.get("method"), 200),
            "mailbox": _cut(raw.get("mailbox"), 60),
            "scenario_dt": _cut(raw.get("scenario_dt"), 20),
            "send_at": _send_at(raw.get("send_date"), raw.get("send_time")),
            "sender_local": _cut(sender_local(raw.get("sender")), 120),
            "sender_domain": _cut(sender_domain(raw.get("sender")), 200),
            "recipients_label": _cut(raw.get("recipients"), 200),
            "delivery": delivery_for(raw.get("recipients")),
            "subject": raw.get("subject"),
            "body": raw.get("content"),
            "supporting_documents": "\n".join(docs) or None,
        })
    _split_shared_usernames(out)
    return out


def _cut(v, n):
    return v[:n] if isinstance(v, str) else v


def _split_shared_usernames(records):
    """news@cnn.com and news@reuters.com in one MEL are two personas: their usernames become
    cnn-news and reuters-news, with news as the username shown in SIMEX Mail."""
    domains = {}
    for r in records:
        if r["sender_local"] and r["sender_domain"]:
            domains.setdefault(r["sender_local"], set()).add(r["sender_domain"])
    for r in records:
        lp, domain = r["sender_local"], r["sender_domain"]
        if lp and domain and len(domains[lp]) > 1:
            labels = [d.split(".")[0] for d in domains[lp]]
            label = domain.split(".")[0] if labels.count(domain.split(".")[0]) == 1 else domain.replace(".", "-")
            r["sender_shown"], r["sender_local"] = lp, f"{label}-{lp}"


def parse_upload(filename, data):
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        raw = read_mel_xlsx(data)
    elif name.endswith(".json"):
        raw = read_json(data)
    else:
        raise ImportError_("Upload MEL-MIL.xlsx or an injects .json file.")
    records = normalise(raw)
    if not records:
        raise ImportError_("No email injects found in this file.")
    return records


# --- comparing and applying ----------------------------------------------------------

def current_value(inj, field):
    v = getattr(inj, field)
    if field == "send_at" and v is not None:
        return v.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="minutes")
    return v


def set_value(inj, field, value):
    if field == "send_at":
        value = datetime.datetime.fromisoformat(value).replace(tzinfo=timezone.utc) if value else None
    setattr(inj, field, value)


LOCKED = {"sending", "sent", "partial"}  # never rewrite what has (partly) gone out
DROPPABLE = {"draft", "scheduled", "overdue"}  # not in the file any more -> may be cancelled


def status_after(status, old_send_at, new_send_at, now):
    """Status once an import has changed send_at (naive UTC ISO strings): a draft that only
    lacked a time gets scheduled, an overdue inject moved to a later time is scheduled again,
    and an inject whose time was removed goes back to draft."""
    if not new_send_at:
        return "draft" if status in ("scheduled", "overdue") else status
    future = datetime.datetime.fromisoformat(new_send_at).replace(tzinfo=timezone.utc) > now
    if future and (status == "overdue" or (status == "draft" and not old_send_at)):
        return "scheduled"
    return status


def _droppable(inj):
    """Came from an import (not created by hand) and has not gone out."""
    return inj.imported is not None and inj.status in DROPPABLE


def plan(records):
    """What an import would do: per inject, new / unchanged / update / conflict."""
    now = datetime.datetime.now(timezone.utc)
    existing = {i.mel_id: i for i in Inject.objects.all()}
    rows = []
    for r in records:
        inj = existing.pop(r["mel_id"], None)
        if not inj:
            rows.append({"mel_id": r["mel_id"], "action": "new", "subject": r["subject"], "changes": []})
            continue
        snap = inj.imported or {}
        changes = []
        for f in IMPORT_FIELDS:
            new, cur = r.get(f), current_value(inj, f)
            if f in snap and new == snap.get(f):
                continue  # unchanged in the MEL since the last import
            if new == cur:
                continue
            local_edit = f in snap and cur != snap.get(f)
            changes.append({"field": f, "current": cur, "new": new, "local_edit": local_edit})
        action = "unchanged" if not changes else ("conflict" if any(c["local_edit"] for c in changes) else "update")
        if changes and all(c["field"] == "seq" for c in changes):
            action = "unchanged"  # only moved within the sheet
        new_status = None
        sa = next((c for c in changes if c["field"] == "send_at"), None)
        if sa and inj.status not in LOCKED:
            after = status_after(inj.status, sa["current"], sa["new"], now)
            new_status = after if after != inj.status else None
        rows.append({"mel_id": r["mel_id"], "action": action, "subject": inj.subject, "changes": changes,
                     "status": inj.status, "new_status": new_status})
    missing = [{"mel_id": i.mel_id, "subject": i.subject, "status": i.status, "droppable": _droppable(i)}
               for i in existing.values()]
    return rows, missing


@transaction.atomic  # all or nothing: an error part-way must not leave half an import
def apply(records, overwrite_local=False, cancel_missing=False, sender_domains=False, user=""):
    now = datetime.datetime.now(timezone.utc)
    existing = {i.mel_id: i for i in Inject.objects.all()}
    counts = {"new": 0, "updated": 0, "kept_local": 0, "skipped_sent": 0, "rescheduled": 0, "cancelled": 0}
    for r in records:
        inj = existing.get(r["mel_id"])
        if not inj:
            inj = Inject(mel_id=r["mel_id"])
            for f in IMPORT_FIELDS:
                set_value(inj, f, r.get(f))
            # A new inject whose time has passed (added mid-exercise) waits for ExCon.
            inj.status = "scheduled" if inj.send_at and inj.send_at > now else "draft"
            inj.imported = {f: r.get(f) for f in IMPORT_FIELDS}
            inj.save()
            counts["new"] += 1
            continue
        if inj.status in LOCKED:
            counts["skipped_sent"] += 1
            continue
        snap = dict(inj.imported or {})
        changed = False
        for f in IMPORT_FIELDS:
            new, cur = r.get(f), current_value(inj, f)
            if f in snap and new == snap.get(f):
                continue  # unchanged in the MEL since the last import: keep what is here (as plan() shows)
            local_edit = f in snap and cur != snap.get(f)
            if new != cur and (not local_edit or overwrite_local):
                set_value(inj, f, new)
                changed = True
                if f == "send_at":
                    after = status_after(inj.status, cur, new, now)
                    if after != inj.status:
                        audit("import_status", f"{inj.mel_id}: {inj.status} -> {after}", user)
                        inj.status = after
                        counts["rescheduled"] += 1
            elif new != cur:
                counts["kept_local"] += 1
                continue  # the old snapshot stays, so the conflict shows again next time
            snap[f] = new
        inj.imported = snap
        inj.save()
        if changed:
            counts["updated"] += 1
    if cancel_missing:
        ids = {r["mel_id"] for r in records}
        for inj in existing.values():
            if inj.mel_id not in ids and _droppable(inj):
                inj.status = "cancelled"
                inj.save()
                audit("import_cancel", f"{inj.mel_id}: not in the file", user)
                counts["cancelled"] += 1
    ensure_senders(records, use_domains=sender_domains)
    audit("import", json.dumps(counts), user)
    return counts


def sender_domains(records):
    """{username: (username shown, domain)} from the MEL for the senders in the file that have
    no SIMEX Mail domain yet (or don't exist yet): what "use the MEL's sender domains" sets."""
    have = dict(Sender.objects.values_list("local_part", "domain"))
    out = {}
    for r in records:
        lp, domain = r.get("sender_local"), r.get("sender_domain")
        if lp and domain and not have.get(lp) and lp not in out:
            out[lp] = (r.get("sender_shown") or "", domain)
    return out


def ensure_senders(records, use_domains=False):
    """Create the senders the MEL uses. With use_domains, a sender without a domain takes the
    MEL's (SIMEX Mail shows it); a domain set on the Senders tab is never changed."""
    domains = sender_domains(records) if use_domains else {}
    have = {x.local_part: x for x in Sender.objects.all()}
    for r in records:
        lp = r.get("sender_local")
        if lp and lp not in have:
            # "undac-{team}-tl" -> "UNDAC {team_name} TL"; edit names on the Senders tab.
            name = " ".join("{team_name}" if "{" in p else p.upper() for p in lp.split("-"))
            shown, domain = domains.get(lp, ("", ""))
            have[lp] = Sender.objects.create(local_part=lp, display_name=name, inapp_local=shown, domain=domain)
        elif lp in domains and not have[lp].domain:
            have[lp].inapp_local, have[lp].domain = domains[lp]
            have[lp].save(update_fields=["inapp_local", "domain"])

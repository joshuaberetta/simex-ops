"""iCalendar (RFC 5545) for SIMEX Calendar's email channel (iMIP, RFC 6047): the text/calendar part
of an invitation email (METHOD REQUEST, a change as REQUEST with a higher SEQUENCE, CANCEL), and
reading a guest's METHOD:REPLY from a received email. Hand-written, no dependency; what is read
is untrusted and capped."""

import re
from datetime import datetime, timezone as dt_tz

from django.conf import settings
from django.utils import timezone

MAX_BYTES = 256 * 1024  # a received calendar part read at most
PARTSTATS = {"ACCEPTED": "accepted", "TENTATIVE": "tentative", "DECLINED": "declined"}


def uid(ev):
    return f"{ev.uid}@{settings.MAIL_DOMAIN}"


def _esc(text):
    return (str(text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line):
    """Lines of at most 75 octets, continued with a space (RFC 5545 3.1)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def _utc(dt):
    return dt.astimezone(dt_tz.utc).strftime("%Y%m%dT%H%M%SZ")


def _cn(name):
    return '"' + str(name or "").replace('"', "'") + '"'


def build(ev, method, organiser, attendees, description=""):
    """The iCalendar text of ev: method REQUEST or CANCEL; organiser (name, email); attendees
    [(name, email, partstat)]. Times in UTC (real), all-day as dates."""
    now = timezone.now()
    lines = ["BEGIN:VCALENDAR", "PRODID:-//SIMEX ops//SIMEX Calendar//EN", "VERSION:2.0", "CALSCALE:GREGORIAN",
             f"METHOD:{method}", "BEGIN:VEVENT", f"UID:{uid(ev)}", f"DTSTAMP:{_utc(now)}", f"SEQUENCE:{ev.sequence}"]
    if ev.all_day:
        lines += [f"DTSTART;VALUE=DATE:{timezone.localtime(ev.starts_at):%Y%m%d}",
                  f"DTEND;VALUE=DATE:{timezone.localtime(ev.ends_at):%Y%m%d}"]
    else:
        lines += [f"DTSTART:{_utc(ev.starts_at)}", f"DTEND:{_utc(ev.ends_at)}"]
    lines.append(f"SUMMARY:{_esc(ev.title)}")
    if ev.location:
        lines.append(f"LOCATION:{_esc(ev.location)}")
    if description:
        lines.append(f"DESCRIPTION:{_esc(description)}")
    name, email = organiser
    lines.append(f"ORGANIZER;CN={_cn(name)}:mailto:{email}")
    for a_name, a_email, partstat in attendees:
        lines.append(f"ATTENDEE;CN={_cn(a_name)};ROLE=REQ-PARTICIPANT;PARTSTAT={partstat};RSVP=TRUE:mailto:{a_email}")
    lines.append("STATUS:CANCELLED" if method == "CANCEL" or ev.status == "cancelled" else "STATUS:CONFIRMED")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return ("\r\n".join(_fold(x) for x in lines) + "\r\n").encode("utf-8")


# --- reading a REPLY ------------------------------------------------------------------------------

def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def _prop(line):
    """("NAME", {params}, value) of a content line (a quoted parameter may hold ":" and ";")."""
    m = re.match(r'^((?:[^":]|"[^"]*")*):(.*)$', line)
    if not m:
        raise ValueError("not a content line")
    return _prop_split(m.group(1), m.group(2))


def _prop_split(head, value):
    parts = re.findall(r'(?:[^;"]|"[^"]*")+', head)
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.strip().upper()] = v.strip().strip('"')
    return parts[0].strip().upper(), params, value


def parse_reply(data):
    """A received calendar part -> {"uid", "answers": [(email, answer)]} for METHOD:REPLY with a
    known PARTSTAT (accepted | tentative | declined), else None. Anything malformed: None."""
    if isinstance(data, bytes):
        data = data[:MAX_BYTES].decode("utf-8", errors="replace")
    text = _unfold((data or "")[:MAX_BYTES])
    if "BEGIN:VCALENDAR" not in text.upper():
        return None
    method, the_uid, answers, in_event = "", "", [], False
    for line in text.splitlines()[:2000]:
        if not line.strip():
            continue
        try:
            name, params, value = _prop(line[:4000])
        except Exception:
            continue
        if name == "METHOD":
            method = value.strip().upper()
        elif name == "BEGIN" and value.strip().upper() == "VEVENT":
            in_event = True
        elif name == "END" and value.strip().upper() == "VEVENT":
            in_event = False
        elif in_event and name == "UID":
            the_uid = value.strip()[:200]
        elif in_event and name == "ATTENDEE":
            email = re.sub(r"^mailto:", "", value.strip(), flags=re.I).lower()[:320]
            answer = PARTSTATS.get(params.get("PARTSTAT", "").upper())
            if email and answer:
                answers.append((email, answer))
    if method != "REPLY" or not the_uid or not answers:
        return None
    return {"uid": the_uid, "answers": answers}


def event_uid(the_uid):
    """The CalendarEvent.uid behind an iCalendar UID of ours, or ""."""
    local, _, domain = (the_uid or "").partition("@")
    return local if domain.lower() == settings.MAIL_DOMAIN.lower() and re.fullmatch(r"[0-9a-f]{8,32}", local) else ""


def parse_dt(value):
    """(Tests) a UTC iCalendar time -> aware datetime."""
    return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt_tz.utc)

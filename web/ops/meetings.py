"""ExCon's Meetings (Team inboxes → Meetings, /meetings/): every event on SIMEX Calendar across the
teams and the personas, with who organised it, the guests and their answers, the personas'
organisations, the time (real and scenario), who on the persona desk plays it, its role play,
and where it came from (an email, the mail by SIMEX AI). Filters, CSV and XLSX export, and a
version the page polls to refresh itself. Computed from what is kept; nothing stored."""

import csv
import hashlib
import io
from datetime import datetime, timedelta

from django.db.models import Count, Max, Q
from django.utils import timezone

from . import calendar, desk, roleplay, simclock
from .models import CalendarAttendee, CalendarEvent, DeskConversation, Mail, Team

STATUSES = ["confirmed", "cancelled"]
ANSWER = {"accepted": "yes", "tentative": "maybe", "declined": "no", "needs_action": "no answer"}


def excon_url(m):
    """A Mail in ExCon's read-only views (the desk's in the desk)."""
    if m is None:
        return ""
    if m.team_id is None:
        return f"/inbox/desk/t/{m.thread}/"
    base = f"/inbox/as/{m.team_id}"
    if m.owner_id:
        base += "/leader" if m.owner and m.owner.is_leader else f"/member/{m.owner_id}"
    return f"{base}/t/{m.thread}/"


def _day(text, end=False):
    try:
        d = datetime.strptime((text or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None
    if end:
        d += timedelta(days=1)
    return timezone.make_aware(d, timezone.get_current_timezone())


def query(f):
    """The events matching filters f (a dict): kind (live | test), team (id), persona (username),
    status, when (all | today | next | range, with hours / since / until), needs (a player)."""
    qs = CalendarEvent.objects.all()
    qs = qs.filter(is_test=(f.get("kind") == "test"))
    if f.get("team"):
        try:
            tid = int(f["team"])
        except ValueError:
            tid = None
        if tid:
            qs = qs.filter(Q(team_id=tid) | Q(attendees__team_id=tid)).distinct()
    if f.get("persona"):
        p = f["persona"]
        qs = qs.filter(Q(organiser_key=f"p:{p}") | Q(attendees__persona=p)).distinct()
    if f.get("status") in STATUSES:
        qs = qs.filter(status=f["status"])
    now = timezone.now()
    when = f.get("when") or "all"
    if when == "today":
        start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        qs = qs.filter(starts_at__lt=start + timedelta(days=1), ends_at__gt=start)
    elif when == "next":
        try:
            hours = max(1, min(24 * 14, int(f.get("hours") or 4)))
        except ValueError:
            hours = 4
        qs = qs.filter(starts_at__lt=now + timedelta(hours=hours), ends_at__gt=now)
    elif when == "range":
        a, b = _day(f.get("since")), _day(f.get("until"), end=True)
        if a:
            qs = qs.filter(ends_at__gt=a)
        if b:
            qs = qs.filter(starts_at__lt=b)
    return qs.select_related("source_mail", "source_mail__owner", "seeded").prefetch_related("attendees").order_by(
        "starts_at", "id")


def rows(f):
    """The Meetings table: one dict per event."""
    names, out = {}, []
    sim = simclock.enabled()
    team_names = dict(Team.objects.values_list("id", "name"))
    owners = {}
    for ev in query(f)[:2000]:
        atts = list(ev.attendees.all())
        personas = [a for a in atts if a.persona or a.key.startswith("p:") and a.team_id is None]
        thread = calendar.desk_thread(ev) if personas else ""
        if thread and thread not in owners:
            c = DeskConversation.objects.filter(thread=thread).first()
            owners[thread] = c.owner_name if c and c.owner_id else ""
        player = owners.get(thread, "")
        needs = bool(personas) and ev.status == "confirmed" and not player
        if f.get("needs") and not needs:
            continue
        invitation = (Mail.objects.filter(event=ev, team=ev.team, owner=ev.owner).exclude(folder="drafts")
                      .select_related("owner").order_by("id").first())
        teams = sorted({a.team_id for a in atts if a.team_id})
        day = timezone.localtime(ev.starts_at).strftime("%Y-%m-%d")
        out.append({
            "event": ev, "id": ev.id, "title": ev.title, "status": ev.status,
            "start": timezone.localtime(ev.starts_at), "end": timezone.localtime(ev.ends_at), "all_day": ev.all_day,
            "sim": calendar.when_text(ev, "sim") if sim else "",
            "mode": dict(calendar.MODES).get(ev.mode, ev.mode), "location": ev.location,
            "organiser": ev.organiser_name or ev.organiser_key,
            "guests": [{"name": a.name or a.key, "org": calendar.persona_org(a.key[2:], names) if a.key.startswith("p:") else "",
                        "answer": ANSWER.get(a.response, a.response), "response": a.response}
                       for a in atts if a.role != "organiser" and not a.hidden],
            "player": player, "needs_player": needs, "desk_url": f"/inbox/desk/t/{thread}/" if thread else "",
            "role_play": roleplay.for_event(ev) if personas else None, "personas": bool(personas),
            "seeded": bool(ev.seeded_id), "from_email": bool(ev.source_mail_id and not ev.seeded_id),
            "thread_url": excon_url(ev.source_mail or invitation),
            "calendars": [{"team": team_names.get(t, "?"), "url": f"/inbox/as/{t}/calendar/?view=day&date={day}"}
                          for t in teams],
            "test": ev.is_test,
        })
    return out


def version():
    """Changes when anything on the calendars or who plays a meeting changes."""
    e = CalendarEvent.objects.aggregate(n=Count("id"), u=Max("updated_at"))
    a = CalendarAttendee.objects.aggregate(n=Count("id"), r=Max("responded_at"))
    d = DeskConversation.objects.exclude(owner=None).aggregate(n=Count("id"), t=Max("assigned_at"))
    return hashlib.sha1(repr((e, a, d)).encode()).hexdigest()[:12]


COLUMNS = ["Start (real)", "End (real)", "Scenario time", "Title", "Status", "How", "Where", "Organiser", "Guests",
           "Played by", "Role play", "From", "Teams"]


def table(f):
    """The export: COLUMNS, then a row per event."""
    out = [COLUMNS]
    for r in rows(f):
        guests = "; ".join(f"{g['name']}{' (' + g['org'] + ')' if g['org'] else ''}: {g['answer']}" for g in r["guests"])
        out.append([r["start"].strftime("%Y-%m-%d %H:%M"), r["end"].strftime("%Y-%m-%d %H:%M"), r["sim"], r["title"],
                    r["status"], r["mode"], r["location"], r["organiser"], guests,
                    r["player"] or ("nobody" if r["needs_player"] else ""), (r["role_play"] or {}).get("key", ""),
                    "the mail (SIMEX AI)" if r["seeded"] else "an email" if r["from_email"] else "",
                    ", ".join(c["team"] for c in r["calendars"])])
    return out


def csv_bytes(f):
    buf = io.StringIO()
    csv.writer(buf).writerows(table(f))
    return buf.getvalue().encode("utf-8-sig")


def xlsx_bytes(f):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Meetings"
    for row in table(f):
        ws.append(row)
    for col, width in zip("ABCDEFGHIJKLM", (17, 17, 26, 36, 11, 10, 24, 28, 60, 16, 12, 18, 20)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def personas():
    return [(s.local_part, s.display_name or s.local_part) for s in desk.personas()]

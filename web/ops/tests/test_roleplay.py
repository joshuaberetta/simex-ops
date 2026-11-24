"""Role-player coordination (ops/roleplay.py, views_roleplay.py, views_rp.py): the schedule
parsed from the sheets, ExCon's board, the role players' pages and their reminders, and the
wall around the role-play material (scripts, outcomes) that nothing participant-facing reads."""

import datetime
import io
import pathlib
import re
from zoneinfo import ZoneInfo

import openpyxl
import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone

from ops import conf, push, roleplay
from ops.models import Notice, Person, PushSubscription, RolePlay, RolePlaySlot, Team
from ops.tests.test_leaders import leader
from ops.tests.test_ops import participant

TZ = ZoneInfo("Africa/Nairobi")


@pytest.mark.parametrize("text,duration,role,expected", [
    ("13:00", "60 min max", "Team Leaders", [(13, 0, 60, [])]),
    ("13:00:00", "", "", [(13, 0, None, [])]),
    ("1440 - A\n", "10 min max per team", "", [(14, 40, 10, ["a"])]),
    ("1430 - A\n1450 - B\n1510 - C\n1530 - D", "", "",
     [(14, 30, None, ["a"]), (14, 50, None, ["b"]), (15, 10, None, ["c"]), (15, 30, None, ["d"])]),
    ("18:00 A - B \n18:20 C - D", "2 x 15 min", "", [(18, 0, 15, ["a", "b"]), (18, 20, 15, ["c", "d"])]),
    ("18:00 + 18:20", "2 x 15 min", "RC - teams A&C", [(18, 0, 15, ["a"]), (18, 20, 15, ["c"])]),
    ("19:10", "4 x 5 min\n(D C B A)", "Press",
     [(19, 10, 5, ["d"]), (19, 15, 5, ["c"]), (19, 20, 5, ["b"]), (19, 25, 5, ["a"])]),
    ("17:05", "4 x 5 min", "A&A Cell coordinator",
     [(17, 5, 5, ["a"]), (17, 10, 5, ["b"]), (17, 15, 5, ["c"]), (17, 20, 5, ["d"])]),
    ("After VTC \n1430 - C\n1500 - D", "", "", [(14, 30, None, ["c"]), (15, 0, None, ["d"])]),
    ("1440 - A", "", "", [(14, 40, None, ["a"])]),
    ("15:35 - B", "4 x 5 min", "", [(15, 35, 5, ["b"]), (15, 40, 5, ["c"]), (15, 45, 5, ["d"]), (15, 50, 5, ["a"])]),
    ("18:00", "4x20 min\nD C B A", "",
     [(18, 0, 20, ["d"]), (18, 20, 20, ["c"]), (18, 40, 20, ["b"]), (19, 0, 20, ["a"])]),
    ("13:45 A&B\n14:45 C&D", "", "", [(13, 45, None, ["a", "b"]), (14, 45, None, ["c", "d"])]),
    ("TBC", "", "", []),
])
def test_slot_times(text, duration, role, expected):
    assert roleplay.parse_slots(text, duration, role, ["a", "b", "c", "d"]) == expected


def test_split_players():
    assert roleplay.split_players("Kenyan RP 1 + Pierre") == ["Kenyan RP 1", "Pierre"]
    assert roleplay.split_players("Silva - Stefania - Rogerio - Sebastian") == ["Silva", "Stefania", "Rogerio",
                                                                               "Sebastian"]
    assert roleplay.split_players("Janet, Aida") == ["Janet", "Aida"]
    assert roleplay.split_players("Jean-Pierre and Ann Lee") == ["Jean-Pierre", "Ann Lee"]
    assert roleplay.split_players("n/a truck driver from HPSS") == [] and roleplay.split_players("") == []


# --- a synthetic workbook (no exercise content in git) ----------------------------------------------

def workbook(mel=True, schedule=True, stale=False):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    if mel:
        ws = wb.create_sheet("MEL-MIL")
        ws.append(["Serial", "Time", "Duration of event (min)", "Description", "Story", "Inject \nmethod", "Content",
                   "Expected outcomes", "Supporting documents", "Day", "Send date", "Send time", "Sender alias"])
        ws.append([None, "Monday\n 21 May 2035"])
        ws.append(["9-01", datetime.time(12, 0), None, "Inject by mail", "x", "Email", "Hello", None, None, "Monday",
                   datetime.datetime(2026, 11, 23), datetime.time(12, 0), "news@x.org"])
        ws.append(["9-02", datetime.time(13, 0), "60 min max", "Kick-off meeting", "story", "F2F - TL",
                   "SCRIPT: greet the team", "Team introduced", "None", "Monday"])
        ws.append(["9-03", "1440 - A\n1450 - B", "10 min max per team", "Passport check", "s", "F2F",
                   "SCRIPT: check passports", "Passports checked", "Airport posters\nNone", "Monday"])
        ws.append(["9-04", "18:00 A - B \n18:20 C - D", "2 x 15 min", "Meet the coordinator", "s", "F2F",
                   "SCRIPT: you are the coordinator", "Overview given", None, "Monday"])
        ws.append(["9-05", datetime.time(9, 0), None, "Call from HQ", "s", "Phone", "SCRIPT: call", None, None,
                   "Tuesday", datetime.datetime(2026, 11, 24)])
    if schedule:
        ws = wb.create_sheet("Role player schedule")
        ws.append(["Role player schedule"])
        ws.append(["Serial", None, "Time", "Duration", "Role", "Role player", "Done", "Event"])
        ws.append(["9-02", "Monday", "13:00", "60 min max", "Team Leaders", "Lena - Omar", None, "Kick-off"])
        ws.append(["9-03", None, "1440 - A\n1450 - B", "10 min max per team", "Immigration officers x 2",
                   "Kenyan RP 1 + Pierre", None, "Immigration"])
        ws.append(["9-04", None, "18:00 + 18:20", "2 x 15 min", "Coordinator - teams A&C", "Peter + Janet", None,
                   "Meet the coordinator"])
        ws.append(["9-04", None, "18:00 + 18:20", "2 x 15 min", "Coordinator - teams B&D", "Ben + Aida", None,
                   "Meet the coordinator"])
        ws.append(["9-06", None, "20:00", "4 x 5 min", "Hotline", "Peter", None, "Routine call"])
        ws.append(["9-07", None, "21:00", "", "Driver", "n/a", None, "OPTIONAL: truck driver"])
    if stale:
        ws = wb.create_sheet("Roleplay schedule")
        ws.append(["Ser", "Day", "Time", "Duration", "Role", "Role player name", "Location"])
        ws.append(["1-004", "Tuesday", "08:00", "", "TL", "Someone", "Old airport"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def teams4():
    for i, code in enumerate("abcd"):
        Team.objects.get_or_create(code=code, defaults={"name": f"Team {code.upper()}", "sort": i})
    Team.objects.filter(code__in="abcd").update(active=True)


def leaders(*names):
    out = []
    for name, code in zip(names, "abcd"):
        out.append(Person.objects.create(name=name, role="tl", team=Team.objects.get(code=code)))
    return out


def do_import(data=None, sheets=None, **kw):
    data = data or workbook()
    sources = roleplay.read_workbook(data)
    recs = roleplay.records(sources, sheets)
    days = roleplay.guess_days(data)
    return roleplay.apply(recs, days=days, **kw)


def test_import_merges_the_mel_and_the_schedule():
    teams4()
    lena, omar = leaders("Lena Park", "Omar Diallo")
    counts = do_import()
    assert roleplay.days_map() == {"monday": "2026-11-23", "tuesday": "2026-11-24"}
    keys = list(RolePlay.objects.values_list("key", flat=True))
    assert keys == ["9-02", "9-03", "9-04", "9-04-b", "9-05", "9-06", "9-07"]  # 9-01 (email) is an inject
    assert counts["new"] == 7
    kick = RolePlay.objects.get(key="9-02")
    assert (kick.script, kick.outcomes, kick.method, kick.role) == ("SCRIPT: greet the team", "Team introduced",
                                                                    "F2F - TL", "Team Leaders")
    assert set(kick.players.all()) == {lena, omar}  # first names matched to the team leaders
    s = kick.slots.get()
    assert s.start == datetime.datetime(2026, 11, 23, 13, 0, tzinfo=TZ) and s.minutes == 60 and not s.teams.exists()
    imm = RolePlay.objects.get(key="9-03")
    assert imm.documents == "Airport posters"
    assert [p.name for p in imm.players.order_by("name")] == ["Kenyan RP 1", "Pierre"]
    assert all(p.role == "rp" and not p.user for p in imm.players.all())  # added as role players, no login yet
    rc_a, rc_b = RolePlay.objects.get(key="9-04"), RolePlay.objects.get(key="9-04-b")
    assert rc_a.script == rc_b.script == "SCRIPT: you are the coordinator"
    assert [(timezone.localtime(x.start).strftime("%H:%M"), [t.code for t in x.teams.all()])
            for x in rc_a.slots.all()] == [("18:00", ["a"]), ("18:20", ["c"])]
    assert [[t.code for t in x.teams.all()] for x in rc_b.slots.all()] == [["b"], ["d"]]
    assert RolePlay.objects.get(key="9-05").slots.get().start.date() == datetime.date(2026, 11, 24)  # Tuesday
    assert RolePlay.objects.get(key="9-06").slots.count() == 4
    drv = RolePlay.objects.get(key="9-07")
    assert drv.optional and not drv.players.exists()
    assert RolePlay.objects.get(key="9-06").day == "Monday"  # the merged day cell, carried forward


def test_reimport_keeps_excon_edits_and_started_slots():
    teams4()
    do_import()
    imm = RolePlay.objects.get(key="9-03")
    imm.location = "Arrivals hall"
    imm.title = "Immigration (edited)"
    imm.save()
    s = imm.slots.first()
    roleplay.set_status(s, "done", "ExCon")
    recs = roleplay.records(roleplay.read_workbook(workbook()))
    for r in recs:
        if r["key"] == "9-03":
            r["title"], r["time_text"] = "Immigration v2", "1500 - A\n1510 - B"
    rows, missing, new_names = roleplay.plan(recs)
    row = next(r for r in rows if r["rec"]["key"] == "9-03")
    assert row["action"] == "conflict" and {c["field"] for c in row["changes"]} == {"title", "time_text"}
    roleplay.apply(recs)
    imm.refresh_from_db()
    assert imm.title == "Immigration (edited)" and imm.location == "Arrivals hall"  # ExCon's edits stay
    assert imm.time_text == "1500 - A\n1510 - B"
    assert imm.slots.filter(status="done").exists()  # a started schedule is never rebuilt
    assert timezone.localtime(imm.slots.first().start).strftime("%H:%M") == "14:40"


def test_the_mel_and_the_schedule_in_separate_uploads():
    teams4()
    do_import(workbook(schedule=False))
    rc = RolePlay.objects.get(key="9-04")
    assert rc.script == "SCRIPT: you are the coordinator" and not rc.players.exists() and rc.slots.count() == 2
    do_import(workbook(mel=False))  # the schedule, later: roles and players, the scripts stay
    rc.refresh_from_db()
    assert rc.script == "SCRIPT: you are the coordinator" and rc.role == "Coordinator - teams A&C"
    assert [p.name for p in rc.players.order_by("name")] == ["Janet", "Peter"]
    b = RolePlay.objects.get(key="9-04-b")
    assert b.script == rc.script and b.outcomes == "Overview given"  # the second row takes the MEL's script
    assert [[t.code for t in s.teams.all()] for s in b.slots.all()] == [["b"], ["d"]]
    # 9-03: the schedule's "1440 - A\n1450 - B" is the MEL's; were it a plain "14:40", the MEL's per-team times stay.
    assert RolePlay.objects.get(key="9-03").slots.count() == 2
    do_import(workbook(schedule=False))  # the MEL again: the players stay
    assert RolePlay.objects.get(key="9-04").players.count() == 2


def test_a_plain_schedule_time_keeps_the_mels_per_team_times():
    mel = {"serial": "9-03", "time": "1440 - A\n1450 - B", "duration": "10 min", "title": "x", "day": "Monday",
           "location": "", "documents": "", "method": "F2F", "script": "", "outcomes": "", "line": 2}
    sched = {"serial": "9-03", "time": "14:40", "duration": "", "title": "", "day": "Monday", "location": "",
             "documents": "", "role": "Immigration", "players": "Ann", "line": 3}
    pair = {**sched, "serial": "9-04", "time": "18:00 + 18:20", "role": "RC - teams A&C"}
    mel4 = {**mel, "serial": "9-04", "time": "18:00 A - B\n18:20 C - D"}
    recs = roleplay.records([{"sheet": "M", "kind": "mel", "rows": [mel, mel4]},
                             {"sheet": "S", "kind": "schedule", "rows": [sched, pair]}])
    assert [r["time_text"] for r in recs] == ["1440 - A\n1450 - B", "18:00 + 18:20"]


def test_stale_sheets_are_left_out_on_request():
    teams4()
    sources = roleplay.read_workbook(workbook(stale=True))
    assert [(s["sheet"], s["kind"]) for s in sources] == [("MEL-MIL", "mel"), ("Role player schedule", "schedule"),
                                                          ("Roleplay schedule", "schedule")]
    assert roleplay.default_sheets(sources) == {"MEL-MIL", "Role player schedule"}  # the old one: other serials
    keys = [r["key"] for r in roleplay.records(sources, roleplay.default_sheets(sources))]
    assert "1-004" not in keys and "9-02" in keys


def test_checks_find_double_bookings_and_gaps():
    teams4()
    do_import()
    peter = Person.objects.get(name="Peter")
    found = [(c["kind"], c["play"].key) for c in roleplay.checks()]
    assert ("location", "9-03") in found
    assert ("unassigned", "9-07") not in found  # optional
    # Peter also takes the 18:00 hotline: double-booked.
    RolePlay.objects.get(key="9-06").slots.update(start=datetime.datetime(2026, 11, 23, 18, 5, tzinfo=TZ))
    texts = [c["text"] for c in roleplay.checks()]
    assert any(t.startswith("Peter is in 9-04") for t in texts)
    assert peter


def test_slip_moves_pending_slots_and_tells_the_players():
    teams4()
    do_import()
    rc = RolePlay.objects.get(key="9-04")
    first = rc.slots.first()
    roleplay.set_status(first, "done")
    moved = roleplay.slip(datetime.datetime(2026, 11, 23, 17, 0, tzinfo=TZ), 15)
    starts = {timezone.localtime(s.start).strftime("%H:%M") for s in moved}
    assert "18:35" in starts and first.id not in {s.id for s in moved}  # the done one stays
    assert all(s.play.day == "Monday" for s in moved)  # Tuesday's untouched
    sent = []
    push.set_sender(lambda info, data: sent.append(data) or 201)
    try:
        peter = Person.objects.get(name="Peter")
        PushSubscription.objects.create(person=peter, endpoint="https://fcm.googleapis.com/x", p256dh="k", auth="a")
        peter.user = get_user_model().objects.create_user("peter", password="pw")
        peter.save()
        push.RUN_INLINE = True
        with transaction_on_commit():
            roleplay.tick(datetime.datetime(2026, 11, 23, 12, 0, tzinfo=TZ))
    finally:
        push.set_sender(None)
        push.RUN_INLINE = False
    assert any("Changed: 9-04 now 18:35 (was 18:20)" in s for s in sent)


class transaction_on_commit:
    """Run on_commit callbacks at once (tests run in a transaction)."""

    def __enter__(self):
        from django.db import transaction
        self._orig = transaction.on_commit
        transaction.on_commit = lambda fn, *a, **k: fn()

    def __exit__(self, *exc):
        from django.db import transaction
        transaction.on_commit = self._orig


def test_reminders_before_and_at_the_start_then_excon_hears_of_a_no_show():
    teams4()
    do_import()
    peter = Person.objects.get(name="Peter")
    peter.user = get_user_model().objects.create_user("peter", password="pw")
    peter.save()
    PushSubscription.objects.create(person=peter, endpoint="https://fcm.googleapis.com/x", p256dh="k", auth="a")
    sent = []
    push.set_sender(lambda info, data: sent.append(data) or 201)
    push.RUN_INLINE = True
    try:
        with transaction_on_commit():
            day = datetime.datetime(2026, 11, 23, tzinfo=TZ)
            roleplay.tick(day.replace(hour=17, minute=40))  # 20 min before 9-04: nothing yet
            assert sent == []
            roleplay.tick(day.replace(hour=17, minute=46))
            assert len(sent) == 1 and "In 14 min: 9-04" in sent[0]
            roleplay.tick(day.replace(hour=17, minute=50))  # once only
            assert len(sent) == 1
            roleplay.tick(day.replace(hour=18, minute=0))
            assert len(sent) == 2 and "Now: 9-04" in sent[1]
            roleplay.tick(day.replace(hour=18, minute=3))
    finally:
        push.set_sender(None)
        push.RUN_INLINE = False
    late = list(Notice.objects.filter(key__startswith="rp-late:"))
    assert len(late) == 2 and all("9-04" in n.title for n in late)  # both 18:00 sittings (A, and B)
    assert {n.note for n in late} == {"Janet, Peter", "Aida, Ben"}


def test_email_reminder_for_role_players_without_alerts():
    from ops.models import Message
    teams4()
    do_import()
    conf.put("rp_email", "1")
    Person.objects.filter(name="Peter").update(personal_email="peter@example.org")
    Person.objects.filter(name="Janet").update(personal_email="janet@gmail.com")
    roleplay.tick(datetime.datetime(2026, 11, 23, 17, 50, tzinfo=TZ))
    roleplay.tick(datetime.datetime(2026, 11, 23, 17, 51, tzinfo=TZ))
    m = Message.objects.get(kind="rp")  # example.org is a placeholder address: Janet's only, and once
    assert m.to == ["janet@gmail.com"] and m.status == "sent" and "9-04" in m.subject


# --- the wall ---------------------------------------------------------------------------------

def test_role_play_material_stays_out_of_participant_facing_code():
    """Scripts and expected outcomes are control-side: only the role-play modules read them."""
    here = pathlib.Path(roleplay.__file__).parent
    for name in ("views_inbox.py", "views_leader.py", "ai.py", "npc.py", "report.py", "views_ai.py",
                 "views_report.py", "importer.py", "render.py", "delivery.py", "desk.py", "inbound.py", "calendar.py",
                 "views_calendar.py"):
        src = (here / name).read_text()
        assert not re.search(r"\bRolePlay\b|\.script\b|\.outcomes\b|rp_briefing|rp_faq", src), name

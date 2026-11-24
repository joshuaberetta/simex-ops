"""SIMEX Calendar, step 4: reminders (calendar.tick in the scheduler's main loop), the poll's
`cal`, the side bar's Upcoming, the persona desk's and ExCon's notices."""

import json
from datetime import datetime, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from ops import calendar, conf, push, simclock
from ops.models import CalendarAttendee, CalendarEvent, DeskConversation, DeskNotice, Mail, Notice, PushSubscription, Sender
from ops.tests.test_calendar_invites import invite, setup
from ops.tests.test_desk import desk_client
from ops.tests.test_leaders import leader
from ops.tests.test_ops import participant


@pytest.fixture
def pushed():
    out = []
    push.set_sender(lambda info, data: out.append((info, json.loads(data))) or 201)
    push.RUN_INLINE = True
    yield out
    push.set_sender(None)
    push.RUN_INLINE = False


def subscribe(*clients):
    for c in clients:
        PushSubscription.objects.create(person=c.person, endpoint=f"https://fcm.googleapis.com/x/{c.person.id}",
                                        p256dh="k", auth="a")


def to(pushed):
    """Who got which reminder: [(person id, title)]."""
    subs = {s.endpoint: s.person_id for s in PushSubscription.objects.all()}
    return sorted((subs.get(info.get("endpoint") if isinstance(info, dict) else getattr(info, "endpoint", "")), d["title"])
                  for info, d in pushed)


def at(minutes, clk="real"):
    """Start, end ("YYYY-MM-DDTHH:MM" on that clock: a member enters the scenario's) of an event
    in minutes from now."""
    a = (timezone.now() + timedelta(minutes=minutes)).replace(second=0, microsecond=0)
    return calendar.shown(a, clk), calendar.shown(a + timedelta(hours=1), clk)


def test_the_timing(excon, pushed, django_capture_on_commit_callbacks):
    setup(excon)
    jane, john = participant("a", "Jane"), participant("a", "John")
    subscribe(jane, john)
    s, e = at(60)
    invite(jane, "t", [f"m:{john.person.id}"], start=s, end=e)
    ev = CalendarEvent.objects.get()
    start = ev.starts_at
    with django_capture_on_commit_callbacks(execute=True):
        assert calendar.tick(start - timedelta(minutes=30)) == 0  # not yet
        assert calendar.tick(start - timedelta(minutes=9)) == 2  # the team's calendar and John's own
        assert calendar.tick(start - timedelta(minutes=8)) == 0  # once
    titles = {t for _, t in to(pushed)}
    assert titles == {"In 9 min: Coordination meeting"}
    got = to(pushed)
    assert sorted(p for p, _ in got) == sorted([jane.person.id, john.person.id, john.person.id])  # team: both; own: John
    # Moved: asked again; reminded again.
    s2, e2 = at(120)
    jane.post("/inbox/calendar/save/", {"id": ev.id, "layer": "t", "title": "Coordination meeting", "start": s2, "end": e2})
    assert not CalendarAttendee.objects.filter(event=ev).exclude(reminded_at=None).exists()
    ev.refresh_from_db()
    # Found too late (the scheduler was down): dropped, never sent later.
    pushed.clear()
    with django_capture_on_commit_callbacks(execute=True):
        assert calendar.tick(ev.starts_at - timedelta(minutes=2)) == 0
        assert calendar.tick(ev.starts_at) == 0
    assert pushed == []


def test_declined_and_cancelled_get_none(excon, pushed, django_capture_on_commit_callbacks):
    setup(excon)
    jane, john = participant("a", "Jane"), participant("a", "John")
    subscribe(jane, john)
    s, e = at(60)
    invite(jane, "t", [f"m:{john.person.id}"], start=s, end=e)
    ev = CalendarEvent.objects.get()
    john.post("/inbox/me/calendar/respond/", {"event": ev.id, "layer": f"o{john.person.id}", "answer": "declined"})
    with django_capture_on_commit_callbacks(execute=True):
        assert calendar.tick(ev.starts_at - timedelta(minutes=5)) == 1  # the team's only
    s, e = at(90)
    invite(jane, "t", [], title="Cancelled one", start=s, end=e)
    ev2 = CalendarEvent.objects.get(title="Cancelled one")
    CalendarEvent.objects.filter(pk=ev2.pk).update(status="cancelled")
    with django_capture_on_commit_callbacks(execute=True):
        assert calendar.tick(ev2.starts_at - timedelta(minutes=5)) == 0
    conf.put("cal_lead", "0")
    s, e = at(120)
    invite(jane, "t", [], title="No reminders", start=s, end=e)
    ev3 = CalendarEvent.objects.get(title="No reminders")
    assert calendar.tick(ev3.starts_at - timedelta(minutes=1)) == 0


def test_a_leaders_own_and_members_see_scenario_time(excon, pushed, django_capture_on_commit_callbacks):
    setup(excon)
    simclock.set_clock(timezone.make_aware(datetime(2035, 5, 20, 9, 0)), 1)
    la, jane = leader("a"), participant("a", "Jane")
    subscribe(la, jane)
    s, e = at(60)
    invite(la, f"o{la.person.id}", [], title="Leaders' call", start=s, end=e)
    s, e = at(60, "sim")
    invite(jane, "t", [], title="Team huddle", start=s, end=e)
    latest = max(CalendarEvent.objects.values_list("starts_at", flat=True))
    with django_capture_on_commit_callbacks(execute=True):
        assert calendar.tick(latest - timedelta(minutes=9)) == 2
    by = {d["title"].split(": ", 1)[1]: (info, d) for info, d in pushed}
    lead_d = by["Leaders' call"][1]
    team_d = by["Team huddle"][1]
    assert "(scenario" in lead_d["body"]  # the leader: real time, scenario beside it
    real = timezone.localdate()
    assert "(scenario" not in team_d["body"] and str(real.year) not in team_d["url"] and "2035-05-20" in team_d["url"]
    assert len([1 for info, d in pushed if d["title"].endswith("Leaders' call")]) == 1  # the leader only
    assert len([1 for info, d in pushed if d["title"].endswith("Team huddle")]) == 1  # Jane (the leader isn't a member)


def test_persona_meetings_reach_the_desk_and_exCon(excon, pushed, django_capture_on_commit_callbacks):
    setup(excon)
    jane = participant("a", "Jane")
    anna = desk_client()
    subscribe(anna)
    s, e = at(60)
    invite(jane, "t", ["p:gov-plaida"], start=s, end=e)
    ev = CalendarEvent.objects.get()
    anna.post("/inbox/desk/calendar/respond/", {"event": ev.id, "layer": "p:gov-plaida", "answer": "accepted"},
              HTTP_X_REQUESTED_WITH="fetch")
    with django_capture_on_commit_callbacks(execute=True):
        calendar.tick(ev.starts_at - timedelta(minutes=10))
        calendar.tick(ev.starts_at - timedelta(minutes=9))
    assert DeskNotice.objects.filter(user=anna.person.user, kind="calendar").count() == 1
    assert Notice.objects.filter(key=f"cal-unstaffed:{ev.id}").count() == 1
    # Once someone has it, no notice for the next one.
    s, e = at(180)
    invite(jane, "t", ["p:gov-plaida"], title="Second", start=s, end=e)
    ev2 = CalendarEvent.objects.get(title="Second")
    anna.post("/inbox/desk/calendar/respond/", {"event": ev2.id, "layer": "p:gov-plaida", "answer": "accepted"},
              HTTP_X_REQUESTED_WITH="fetch")
    thread = calendar.desk_thread(ev2)
    DeskConversation.objects.update_or_create(thread=thread, defaults={"owner": anna.person.user, "owner_name": "Anna"})
    calendar.tick(ev2.starts_at - timedelta(minutes=10))
    assert not Notice.objects.filter(key=f"cal-unstaffed:{ev2.id}").exists()


def test_the_poll_and_the_side_bar(excon):
    setup(excon)
    simclock.set_clock(timezone.make_aware(datetime(2035, 5, 20, 9, 0)), 1)
    jane, john = participant("a", "Jane"), participant("a", "John")
    s, e = at(5, "sim")
    invite(jane, "t", [f"m:{john.person.id}"], start=s, end=e)
    j = john.get("/inbox/me/poll/").json()
    assert j["cal"]["invites"] == 1 and j["cal"]["badge"] == 2 and j["cal"]["soon"][0]["title"].startswith("In ")
    real = timezone.localdate()
    assert str(real.year) not in str(j["cal"])
    page = john.get("/inbox/me/").content.decode()
    assert "data-upcoming" in page and "Coordination meeting" in page and 'data-app="calendar"' in page
    assert str(real.isoformat()) not in page
    assert jane.get("/inbox/poll/").json()["cal"]["invites"] == 0
    assert participant("b", "Bea").get("/inbox/poll/").json()["cal"] == {"soon": [], "invites": 0, "badge": 0}


def test_settings_keep_the_lead(excon):
    r = excon.post("/settings/", {"cal_lead": "15", "grace_minutes": "10", "max_upload_mb": "10", "desk_wait_minutes": "20"})
    assert r.status_code == 302 and calendar.lead() == 15
    assert b"Remind before an event" in excon.get("/settings/").content

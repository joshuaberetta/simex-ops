"""SIMEX Calendar (ops/calendar.py, views_calendar.py) and the app drawer: each mailbox's
calendar, teams never crossing, read-only views that can't write, members seeing only the
simulation time, Clear mailboxes and Start afresh."""

import json
from datetime import datetime, timedelta

from django.utils import timezone

from ops import calendar, reset, simclock
from ops.models import CalendarAttendee, CalendarEvent, MailAction, Person, Sender, Team
from ops.tests.test_desk import desk_client
from ops.tests.test_leaders import leader
from ops.tests.test_ops import SAMPLE, do_import, participant


def local(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%dT%H:%M")


def new_event(c, layer, start, end, base="/inbox", **extra):
    r = c.post(f"{base}/calendar/save/", {"layer": layer, "title": extra.pop("title", "Coordination meeting"),
                                          "start": start, "end": end, **extra})
    return r


def listed(c, base="/inbox", day=None):
    day = day or timezone.localdate()
    r = c.get(f"{base}/calendar/events/", {"from": (day - timedelta(days=3)).isoformat(),
                                           "to": (day + timedelta(days=4)).isoformat()})
    assert r.status_code == 200, r.content
    return r.json()["events"]


def soon(hours=2, length=1):
    a = timezone.localtime() + timedelta(hours=hours)
    a = a.replace(minute=0, second=0, microsecond=0)
    return local(a), local(a + timedelta(hours=length))


def test_members_share_the_team_calendar_and_keep_their_own():
    jane, john, other = participant("a", "Jane"), participant("a", "John"), participant("b", "Bea")
    ta = Team.objects.get(code="a")
    start, end = soon()
    r = new_event(jane, "t", start, end, location="OSOCC tent", mode="in_person")
    assert r.status_code == 200 and r.json()["ok"], r.content
    ev = CalendarEvent.objects.get()
    assert ev.team == ta and ev.owner is None and ev.organiser_key == f"t:{ta.id}" and local(ev.starts_at) == start
    assert ev.attendees.get().role == "organiser"
    assert new_event(jane, f"o{jane.person.id}", start, end, title="My own").json()["ok"]
    assert {e["title"] for e in listed(jane)} == {"Coordination meeting", "My own"}
    assert [e["title"] for e in listed(john)] == ["Coordination meeting"]  # not Jane's own
    assert listed(other) == []  # teams never cross
    assert listed(jane, "/inbox/me") and {e["layer"] for e in listed(jane)} == {"t", f"o{jane.person.id}"}
    # Another team can't change or delete it, nor write to Jane's own calendar.
    assert other.post("/inbox/calendar/save/", {"id": ev.id, "layer": "t", "title": "x", "start": start, "end": end}).status_code == 404
    assert other.post(f"/inbox/calendar/delete/{ev.id}/").status_code == 404
    assert new_event(john, f"o{jane.person.id}", start, end).status_code == 400
    # A teammate may change the team's event (it is the team's), and it is logged in the team's box.
    r = john.post("/inbox/calendar/save/", {"id": ev.id, "layer": "t", "title": "Moved", "start": start, "end": end})
    assert r.json()["ok"] and CalendarEvent.objects.get(pk=ev.id).title == "Moved"
    acts = list(MailAction.objects.filter(action__startswith="cal_").order_by("id").values_list("action", "owner", "person"))
    assert acts == [("cal_create", None, jane.person.id), ("cal_create", jane.person.id, jane.person.id),
                    ("cal_update", None, john.person.id)]
    assert john.post(f"/inbox/calendar/delete/{ev.id}/").json()["ok"]
    assert not CalendarEvent.objects.filter(pk=ev.id).exists() and not CalendarAttendee.objects.filter(event_id=ev.id).exists()
    page = jane.get("/inbox/calendar/")
    assert page.status_code == 200 and b"SIMEX <b>Calendar</b>" in page.content and b"data-cal-create" in page.content


def test_bad_input_is_refused():
    jane = participant("a", "Jane")
    start, end = soon()
    assert new_event(jane, "t", end, start).json()["error"] == "It has to end after it starts."
    assert new_event(jane, "t", "soon", end).status_code == 400
    far = local(timezone.localtime() + timedelta(days=20))
    assert "at most" in new_event(jane, "t", start, far).json()["error"]
    assert new_event(jane, "nope", start, end).status_code == 400
    r = new_event(jane, "t", start[:10], start[:10], all_day="1", title="")
    ev = CalendarEvent.objects.get()
    assert r.json()["ok"] and ev.all_day and ev.title == "(No title)" and ev.ends_at - ev.starts_at == timedelta(days=1)


def test_leader_writes_their_own_and_reads_the_team_and_members():
    la, jane = leader("a"), participant("a", "Jane")
    lead = la.person
    start, end = soon()
    assert new_event(jane, "t", start, end, title="Team thing").json()["ok"]
    assert new_event(jane, f"o{jane.person.id}", start, end, title="Jane's").json()["ok"]
    assert new_event(la, "t", start, end).status_code == 400  # the team's calendar is read-only for the leader
    assert new_event(la, f"o{jane.person.id}", start, end).status_code == 400
    r = new_event(la, f"o{lead.id}", start, end, title="Leaders' call")
    assert r.json()["ok"]
    ev = CalendarEvent.objects.get(title="Leaders' call")
    assert ev.owner == lead and ev.organiser_key == f"l:{lead.id}"
    assert {e["title"]: e["editable"] for e in listed(la)} == {"Team thing": False, "Jane's": False, "Leaders' call": True}
    assert "Leaders' call" not in {e["title"] for e in listed(jane)}  # members never see the leader's own
    # Read-only views: the team's calendar and a member's have no write routes.
    assert la.get("/inbox/team/calendar/").status_code == 200
    assert {e["title"] for e in listed(la, "/inbox/team")} >= {"Team thing"}
    assert la.post("/inbox/team/calendar/save/", {"layer": "t"}).status_code == 404
    assert la.get(f"/inbox/members/{jane.person.id}/calendar/").status_code == 200
    # Another team leader's mailbox has no calendar.
    lb = leader("b")
    assert lb.get(f"/inbox/leaders/{Team.objects.get(code='a').id}/calendar/").status_code == 404
    assert listed(lb) == []


def test_excon_and_the_desk_view_a_team_read_only(excon):
    jane = participant("a", "Jane")
    ta = Team.objects.get(code="a")
    start, end = soon()
    assert new_event(jane, "t", start, end).json()["ok"]
    base = f"/inbox/as/{ta.id}"
    assert excon.get(f"{base}/calendar/").status_code == 200
    assert [e["title"] for e in listed(excon, base)] == ["Coordination meeting"]
    assert not listed(excon, base)[0]["editable"]
    assert new_event(excon, "t", start, end, base=base).status_code == 403
    ev = CalendarEvent.objects.get()
    assert excon.post(f"{base}/calendar/delete/{ev.id}/").status_code == 403
    desk = desk_client()
    assert desk.get(f"{base}/calendar/").status_code == 200
    assert new_event(desk, "t", start, end, base=base).status_code == 403


def test_the_desk_has_a_layer_per_persona(excon):
    do_import(excon, SAMPLE)
    desk = desk_client()
    personas = [s.local_part for s in Sender.objects.filter(active=True, team_leader=False)]
    assert personas
    r = desk.get("/inbox/desk/calendar/")
    assert r.status_code == 200
    data = json.loads(r.content.split(b'id="simex-cal" type="application/json">')[1].split(b"</script>")[0])
    assert [lay["key"] for lay in data["layers"]] == [f"p:{p}" for p in personas]
    start, end = soon()
    assert new_event(desk, f"p:{personas[0]}", start, end, base="/inbox/desk").json()["ok"]
    ev = CalendarEvent.objects.get()
    row = ev.attendees.get()
    assert ev.team is None and ev.owner is None and ev.organiser_key == f"p:{personas[0]}" and row.persona == personas[0]
    assert ev.created_by == desk.person
    assert [e["title"] for e in listed(desk, "/inbox/desk")] == ["Coordination meeting"]
    assert listed(participant("a", "Jane")) == []  # never on a team's calendar
    assert not MailAction.objects.exists()  # the desk's actions are not the teams'
    assert participant("a", "Ann").get("/inbox/desk/calendar/").status_code == 403


def test_members_see_only_the_simulation_time(excon):
    simclock.set_clock(timezone.make_aware(datetime(2035, 5, 20, 9, 0)), 1)
    jane, la = participant("a", "Jane"), leader("a")
    real_today = timezone.localdate()
    # Jane enters scenario time: 20 May 2035 11:00-12:00, two hours from the clock's now.
    r = new_event(jane, "t", "2035-05-20T11:00", "2035-05-20T12:00")
    assert r.json()["ok"] and r.json()["event"]["start"] == "2035-05-20T11:00" and "sim" not in r.json()["event"]
    ev = CalendarEvent.objects.get()
    assert abs((ev.starts_at - timezone.now()) - timedelta(hours=2)) < timedelta(minutes=2)  # kept real
    r = jane.get("/inbox/calendar/events/", {"from": "2035-05-17", "to": "2035-05-24"})
    body = r.content.decode()
    assert [e["start"] for e in r.json()["events"]] == ["2035-05-20T11:00"]
    assert real_today.isoformat() not in body and str(real_today.year) not in body
    page = jane.get("/inbox/calendar/").content.decode()
    assert real_today.isoformat() not in page and "2035-05-20" in page
    # The leader sees the real time, with the scenario time beside it.
    e = listed(la, day=real_today)[0]
    assert e["start"] == calendar.shown(ev.starts_at, "real") and e["sim"]["start"] == "2035-05-20T11:00"
    # Moving the clock moves the scenario time; the event keeps its real time.
    simclock.set_clock(timezone.make_aware(datetime(2035, 5, 21, 9, 0)), 1)
    r = jane.get("/inbox/calendar/events/", {"from": "2035-05-20", "to": "2035-05-23"})
    assert [x["start"] for x in r.json()["events"]] == ["2035-05-21T11:00"]


def test_simclock_maps_both_ways():
    assert simclock.at(timezone.now()) is None and simclock.real_at(timezone.now()) is not None
    simclock.set_clock(timezone.make_aware(datetime(2035, 5, 20, 9, 0)), 4)
    later = timezone.now() + timedelta(minutes=30)
    sim = simclock.at(later)
    assert abs(sim - timezone.make_aware(datetime(2035, 5, 20, 11, 0))) < timedelta(minutes=1)
    assert abs(simclock.real_at(sim) - later) < timedelta(seconds=1)
    simclock.set_rate(0)  # stopped: events still keep their length and order
    a, b = simclock.at(later), simclock.at(later + timedelta(hours=1))
    assert b - a == timedelta(hours=1)


def test_clearing_and_starting_afresh_empty_the_calendars(excon):
    jane, bea = participant("a", "Jane"), participant("b", "Bea")
    start, end = soon()
    new_event(jane, "t", start, end)
    new_event(jane, f"o{jane.person.id}", start, end)
    new_event(bea, "t", start, end)
    ta = Team.objects.get(code="a")
    reset.clear_team(ta, scope="members")
    assert CalendarEvent.objects.filter(team=ta).count() == 1 and not CalendarEvent.objects.filter(owner=jane.person).exists()
    reset.clear_team(ta)
    assert not CalendarEvent.objects.filter(team=ta).exists() and CalendarEvent.objects.count() == 1
    reset.start_afresh()
    assert not CalendarEvent.objects.exists() and not CalendarAttendee.objects.exists()


def test_backup_keeps_the_calendars(excon):
    from ops import backup
    jane = participant("a", "Jane")
    start, end = soon()
    new_event(jane, "t", start, end, title="Kept")
    data = backup.export()
    CalendarEvent.objects.all().delete()
    backup.restore(data)
    assert CalendarEvent.objects.get().title == "Kept" and CalendarAttendee.objects.count() == 1


def test_the_app_drawer_shows_each_login_its_apps(excon):
    jane, la, desk = participant("a", "Jane"), leader("a"), desk_client()
    page = jane.get("/inbox/").content.decode()
    assert "data-apps-toggle" in page and 'href="/inbox/calendar/"' in page and 'data-app="me"' in page
    assert 'data-app="dashboard"' not in page
    page = jane.get("/inbox/calendar/").content.decode()
    assert 'class="sx-app on" role="menuitem" aria-current="page" data-app="calendar"' in page
    page = la.get("/inbox/").content.decode()
    assert all(f'data-app="{k}"' in page for k in ("mail", "team", "members", "calendar", "dashboard", "help"))
    page = desk.get("/inbox/desk/").content.decode()
    assert all(f'data-app="{k}"' in page for k in ("desk", "calendar", "teams", "dashboard", "assign"))
    ta = Team.objects.get(code="a")
    page = excon.get(f"/inbox/as/{ta.id}/").content.decode()
    assert f'href="/inbox/as/{ta.id}/calendar/"' in page and 'data-app="excon"' in page
    rp = Person.objects.create(name="Rita", role="rp")
    from django.contrib.auth import get_user_model
    from django.test import Client
    rp.user = get_user_model().objects.create_user("rita", password="pw")
    rp.save()
    c = Client()
    c.login(username="rita", password="pw")
    page = c.get("/inbox/rp/").content.decode()
    assert 'data-app="rp"' in page and "/inbox/calendar/" not in page


def test_calendar_routes_are_mailbox_scoped():
    """Every calendar route sits on a mailbox base: anonymous users go to sign in, a member can't
    reach another team's, the desk's or a leader's read-only views."""
    from django.test import Client
    jane = participant("a", "Jane")
    tb = Team.objects.get(code="b")
    for url in ("/inbox/calendar/", "/inbox/calendar/events/", "/inbox/desk/calendar/", f"/inbox/as/{tb.id}/calendar/",
                "/inbox/team/calendar/"):
        assert Client().get(url).status_code == 302, url
    assert jane.get(f"/inbox/as/{tb.id}/calendar/").status_code == 403
    assert jane.get("/inbox/team/calendar/").status_code == 403
    assert jane.get("/inbox/desk/calendar/").status_code == 403
    assert calendar.clock  # (module loaded)

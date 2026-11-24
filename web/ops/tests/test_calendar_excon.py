"""SIMEX Calendar, step 6: ExCon's Meetings, Make a role play, the reports' figures and the Network."""

import io
from datetime import timedelta

import openpyxl
from django.utils import timezone

from ops import network, report
from ops.models import CalendarEvent, DeskConversation, RolePlay, RolePlaySlot, Team
from ops.tests.test_calendar_invites import invite, listed, setup
from ops.tests.test_desk import desk_client
from ops.tests.test_leaders import leader
from ops.tests.test_ops import participant


def test_the_meetings_overview(excon):
    setup(excon)
    jane, anna = participant("a", "Jane"), desk_client()
    invite(jane, "t", ["p:gov-plaida"])
    invite(jane, "t", [], title="Team huddle")
    ev = CalendarEvent.objects.get(title="Coordination meeting")
    page = excon.get("/meetings/").content.decode()
    assert "Coordination meeting" in page and "Team huddle" in page and "Grace Mwangi" in page
    assert "Make a role play" in page and "nobody" in page
    page = excon.get("/meetings/?needs=1").content.decode()
    assert "Coordination meeting" in page and "Team huddle" not in page
    page = excon.get("/meetings/?persona=gov-plaida").content.decode()
    assert "Team huddle" not in page
    page = excon.get("/meetings/?kind=test").content.decode()
    assert "Coordination meeting" not in page
    page = excon.get("/meetings/?when=next&hours=1").content.decode()
    assert "Coordination meeting" not in page  # three hours away
    assert "Coordination meeting" in excon.get("/meetings/?when=next&hours=6").content.decode()
    # Played by: the desk conversation's owner.
    thread = listed(anna, "/inbox/desk")[0]["desk"]["thread"]
    v = excon.get("/meetings/v/").json()["v"]
    DeskConversation.objects.update_or_create(thread=thread, defaults={"owner": anna.person.user, "owner_name": "Anna Desk",
                                                                       "assigned_at": timezone.now()})
    assert excon.get("/meetings/v/").json()["v"] != v
    assert "Anna Desk" in excon.get("/meetings/").content.decode()
    csv = excon.get("/meetings/export/csv/").content.decode("utf-8-sig")
    assert csv.startswith("Start (real),") and "Coordination meeting" in csv and "Anna Desk" in csv
    wb = openpyxl.load_workbook(io.BytesIO(excon.get("/meetings/export/xlsx/").content))
    assert wb.active.cell(1, 4).value == "Title" and wb.active.max_row == 3
    assert jane.get("/meetings/").status_code == 403 and jane.get("/meetings/export/csv/").status_code == 403
    assert ev  # (used)


def test_make_a_role_play_from_a_persona_meeting(excon):
    setup(excon)
    jane = participant("a", "Jane")
    ta = Team.objects.get(code="a")
    invite(jane, "t", ["p:gov-plaida"], mode="phone")
    ev = CalendarEvent.objects.get()
    r = excon.post(f"/roleplay/from-event/{ev.id}/", HTTP_X_REQUESTED_WITH="fetch")
    assert r.json()["ok"] and r.json()["key"] == f"CAL-{ev.id}"
    rp = RolePlay.objects.get(event=ev)
    slot = rp.slots.get()
    assert rp.method == "Phone" and rp.title == "Coordination meeting" and slot.start == ev.starts_at
    assert list(slot.teams.all()) == [ta] and rp.script == ""
    assert excon.post(f"/roleplay/from-event/{ev.id}/", HTTP_X_REQUESTED_WITH="fetch").json()["key"] == rp.key  # once
    # The desk sees it on the event, ExCon with a link; never the team.
    anna = desk_client()
    assert listed(anna, "/inbox/desk")[0]["desk"]["rolePlay"]["key"] == rp.key
    assert "rolePlay" not in str(listed(jane)) and rp.key not in jane.get(f"/inbox/t/{ev.uid}/").content.decode()
    # Moved: the sitting follows; cancelled: skipped.
    s = (timezone.localtime(ev.starts_at) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    e = (timezone.localtime(ev.ends_at) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    invite(jane, "t", ["p:gov-plaida"], eid=ev.id, start=s, end=e, mode="phone")
    slot.refresh_from_db()
    ev.refresh_from_db()
    assert slot.start == ev.starts_at and slot.was_start and slot.changed_at
    jane.post(f"/inbox/calendar/delete/{ev.id}/")
    slot.refresh_from_db()
    assert slot.status == "skipped"
    # Deleting the role play unlinks it from the event.
    rp.delete()
    assert CalendarEvent.objects.filter(pk=ev.pk).exists() and listed(anna, "/inbox/desk")[0]["desk"]["rolePlay"] is None
    # Only persona meetings.
    invite(jane, "t", [], title="Team huddle")
    huddle = CalendarEvent.objects.get(title="Team huddle")
    assert excon.post(f"/roleplay/from-event/{huddle.id}/", HTTP_X_REQUESTED_WITH="fetch").status_code == 400
    assert jane.post(f"/roleplay/from-event/{huddle.id}/").status_code == 403


def test_the_reports_count_the_meetings(excon):
    setup(excon)
    jane, john, la = participant("a", "Jane"), participant("a", "John"), leader("a")
    ta = Team.objects.get(code="a")
    invite(jane, "t", ["p:gov-plaida", f"m:{john.person.id}"])
    ev = CalendarEvent.objects.get()
    anna = desk_client()
    anna.post("/inbox/desk/calendar/respond/", {"event": ev.id, "layer": "p:gov-plaida", "answer": "accepted"},
              HTTP_X_REQUESTED_WITH="fetch")
    invite(la, f"o{la.person.id}", [f"t:{ta.id}"], title="Leader's briefing")
    lb = CalendarEvent.objects.get(title="Leader's briefing")
    jane.post("/inbox/calendar/respond/", {"event": lb.id, "layer": "t", "answer": "accepted"}, HTTP_X_REQUESTED_WITH="fetch")
    c = report.calendar_figures(ta)
    assert c["meetings_organised"] == 2 and c["meetings_with_personas"] == 1
    assert c["invitations_received"] == 2 and c["invitations_answered"] == 1 and c["invitations_unanswered"] == 1
    assert c["median_minutes_to_answer"] is not None
    fig = report.team_figures(ta, "live")
    assert fig["calendar"] == c
    assert "John" not in str(report.for_team(fig, False)["calendar"])
    ex = report.exercise_figures("live", [fig])
    assert ex["teams"][0]["calendar"]["meetings_organised"] == 2


def test_the_network_counts_invitations_once(excon):
    setup(excon)
    jane, la, anna = participant("a", "Jane"), leader("a"), desk_client()
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    invite(la, f"o{la.person.id}", [f"t:{ta.id}"], title="Leader's briefing")
    invite(anna, "p:gov-plaida", [f"t:{ta.id}", f"t:{tb.id}"], base="/inbox/desk", title="Road briefing")
    ss = network.graph()["sends"]
    leader_sends = [s for s in ss if s["s"].startswith("Invitation: Leader's briefing")]
    desk_sends = [s for s in ss if s["s"].startswith("Invitation: Road briefing")]
    assert len(leader_sends) == 1 and leader_sends[0]["to"] == [f"t:{ta.id}"]
    assert sorted(t for s in desk_sends for t in s["to"]) == sorted([f"t:{ta.id}", f"t:{tb.id}"]) and len(desk_sends) == 2
    ev = CalendarEvent.objects.get(title="Road briefing")
    jane.post("/inbox/calendar/respond/", {"event": ev.id, "layer": "t", "answer": "accepted"}, HTTP_X_REQUESTED_WITH="fetch")
    answers = [s for s in network.graph()["sends"] if s["s"].startswith("Accepted: Road briefing")]
    assert len(answers) == 1

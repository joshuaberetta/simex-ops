"""Meetings in the mail (ops/cal_seed.py): SIMEX AI (a fake client) finds meetings already arranged
in the teams' mail; ExCon checks and applies them quietly onto the right calendars."""

import json
from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from ops import ai, cal_seed, conf, reset, simclock
from ops.models import (CalendarAttendee, CalendarEvent, CalendarSeed, CalendarSeedItem, Inject, Mail, Notice, Sender,
                        Team)
from ops.tests.test_ai import FakeClient
from ops.tests.test_leaders import setup_exercise
from ops.tests.test_ops import participant

ANSWER = {"meetings": []}


@pytest.fixture
def fake():
    c = FakeClient(lambda kw: (ANSWER, "end_turn"))
    ai.set_client(c)
    conf.put("ai_enabled", "1")
    return c


def setup(excon):
    setup_exercise(excon)
    Sender.objects.get_or_create(local_part="gov-plaida", defaults={"display_name": "Grace Mwangi",
                                                                    "notes": "SECRET EXCON NOTE"})
    jane = participant("a", "Jane")
    jane.post("/inbox/send/", {"to": "p:gov-plaida", "subject": "Meeting on the roads",
                               "body": "Could we meet tomorrow at 10:00 at the OSOCC?"})
    sent = Mail.objects.get(team__code="a", owner=None, folder="sent", subject="Meeting on the roads")
    return jane, sent


def meeting(mail, **kw):
    out = {"thread": "T1", "title": "Roads meeting", "start": "", "end": "", "time_base": "real", "anchor_mail": mail.id,
           "all_day": False, "location": "OSOCC", "mode": "in_person", "organiser": f"t:{mail.team_id}",
           "guests": [{"key": "p:gov-plaida", "answer": "accepted"}], "status": "confirmed", "confidence": 90,
           "sources": [{"mail": mail.id, "quote": "Could we meet tomorrow at 10:00"}], "note": ""}
    a = (timezone.localtime(mail.created_at) + timedelta(days=1)).replace(hour=10, minute=0)
    out["start"], out["end"] = a.strftime("%Y-%m-%dT%H:%M"), (a + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    out.update(kw)
    return out


def find(excon, **data):
    r = excon.post("/meetings/find/", data)
    assert r.status_code == 302, r.content[:300]
    return CalendarSeed.objects.order_by("-id").first()


def test_found_checked_and_applied_without_any_mail(excon, fake):
    jane, sent = setup(excon)
    ANSWER["meetings"] = [meeting(sent)]
    seed = find(excon)
    assert seed.status == "done" and seed.threads >= 1 and Notice.objects.filter(key=f"cal_seed:{seed.id}").exists()
    call = fake.calls[-1]
    text = json.dumps(call["messages"])
    assert "Could we meet tomorrow" in text and "SECRET EXCON NOTE" not in text and "SECRET" not in text
    item = seed.items.get()
    assert item.ticked and item.organiser_key == f"t:{sent.team_id}" and item.guests == [{"key": "p:gov-plaida", "response": "accepted"}]
    assert item.starts_at.hour == (timezone.localtime(sent.created_at) + timedelta(days=1)).replace(hour=10).astimezone(item.starts_at.tzinfo).hour
    page = excon.get(f"/meetings/{seed.id}/")
    assert page.status_code == 200 and b"Roads meeting" in page.content and b"Could we meet tomorrow" in page.content
    mails_before = Mail.objects.count()
    r = excon.post(f"/meetings/{seed.id}/", {"action": "apply", f"i{item.id}-ticked": "1", f"i{item.id}-title": "Roads meeting",
                                             f"i{item.id}-start": item.starts_at and timezone.localtime(item.starts_at).strftime("%Y-%m-%dT%H:%M"),
                                             f"i{item.id}-end": timezone.localtime(item.ends_at).strftime("%Y-%m-%dT%H:%M"),
                                             f"i{item.id}-location": "OSOCC", f"i{item.id}-mode": "in_person",
                                             f"i{item.id}-status": "confirmed", f"i{item.id}-organiser": item.organiser_key,
                                             f"i{item.id}-guests": "p:gov-plaida"})
    assert r.status_code == 302
    assert Mail.objects.count() == mails_before  # quietly: no invitation, no answer
    ev = CalendarEvent.objects.get()
    assert ev.seeded == seed and ev.source_mail == sent and ev.team == sent.team and ev.owner is None
    rows = {a.key: a for a in ev.attendees.all()}
    assert rows["p:gov-plaida"].persona == "gov-plaida" and rows["p:gov-plaida"].response == "accepted"
    # On the team's calendar, "From the mail", with the conversation linked; and on the desk's.
    day = timezone.localtime(ev.starts_at).date()
    e = jane.get("/inbox/calendar/events/", {"from": (day - timedelta(days=1)).isoformat(),
                                             "to": (day + timedelta(days=1)).isoformat()}).json()["events"][0]
    assert e["seeded"] and e["source"]["url"] == f"/inbox/t/{sent.thread}/"
    assert "On the calendar (from the mail)" in jane.get(f"/inbox/t/{sent.thread}/").content.decode()
    # Running again skips what was read, unless asked.
    ANSWER["meetings"] = []
    again = find(excon)
    assert again.threads == 0
    again = find(excon, include_seeded="1")
    assert again.threads >= 1


def test_keys_from_outside_its_conversations_are_left_out(excon, fake):
    jane, sent = setup(excon)
    tb = Team.objects.get(code="b")
    bea = participant("b", "Bea")
    ANSWER["meetings"] = [meeting(sent, guests=[{"key": f"t:{tb.id}", "answer": "accepted"},
                                                {"key": f"m:{bea.person.id}", "answer": "accepted"},
                                                {"key": "p:gov-plaida", "answer": "needs_action"}]),
                          meeting(sent, thread="T99"), meeting(sent, sources=[{"mail": 999999, "quote": "x"}]),
                          meeting(sent, organiser=f"t:{tb.id}", title="Hijack")]
    seed = find(excon)
    items = list(seed.items.order_by("id"))
    assert len(items) == 2  # an unknown thread, and evidence not in it, are dropped
    a, hijack = items
    assert a.guests == [{"key": "p:gov-plaida", "response": "needs_action"}]
    assert any("left out" in p for p in a.problems)
    assert hijack.organiser_key == f"t:{sent.team_id}"  # never Team B's calendar
    # Editing can't put it on another team's calendar either.
    cal_seed.edit(a, {"organiser": f"t:{tb.id}", "guests": f"t:{tb.id}, p:gov-plaida", "ticked": "1",
                      "start": timezone.localtime(a.starts_at).strftime("%Y-%m-%dT%H:%M"),
                      "end": timezone.localtime(a.ends_at).strftime("%Y-%m-%dT%H:%M")})
    a.refresh_from_db()
    assert a.organiser_key == f"t:{sent.team_id}" and [g["key"] for g in a.guests] == ["p:gov-plaida"]
    cal_seed.apply(seed)
    assert not CalendarAttendee.objects.filter(team=tb).exists()
    assert participant("b", "Bo").get("/inbox/calendar/events/", {"from": "2000-01-01", "to": "2000-02-01"}).json()["events"] == []


def test_duplicates_are_flagged(excon, fake):
    jane, sent = setup(excon)
    m = meeting(sent)
    jane.post("/inbox/calendar/save/", {"layer": "t", "title": "Roads", "start": m["start"], "end": m["end"],
                                        "guests": ["p:gov-plaida"], "guests_given": "1"})
    ANSWER["meetings"] = [m]
    item = find(excon).items.get()
    assert item.duplicate is not None and "same guests" in item.duplicate_why and not item.ticked
    CalendarEvent.objects.update(source_mail=sent)
    item.duplicate = None
    from ops.cal_seed import duplicate_of
    assert duplicate_of(item)[1] == "made from the same conversation"


def test_scenario_times_are_placed_through_their_mail(excon, fake):
    simclock.set_clock(timezone.make_aware(datetime(2035, 5, 20, 9, 0)), 1)
    jane, sent = setup(excon)
    ANSWER["meetings"] = [meeting(sent, start="2035-05-21T10:00", end="2035-05-21T11:00", time_base="scenario")]
    item = find(excon).items.get()
    expect = sent.created_at + (timezone.make_aware(datetime(2035, 5, 21, 10, 0)) - sent.sim_at)
    assert abs(item.starts_at - expect) < timedelta(minutes=1)
    page = excon.get(f"/meetings/{item.seed_id}/").content.decode()
    assert "scenario 2035-05-21 10:00" in page


def test_off_without_ai_and_excon_only(excon):
    setup_exercise(excon)
    page = excon.get("/meetings/find/").content.decode()
    assert "disabled" in page
    assert excon.post("/meetings/find/", {}).status_code == 302 and not CalendarSeed.objects.exists()
    pa = participant("a", "Jane")
    assert pa.get("/meetings/find/").status_code == 403 and pa.post("/meetings/find/", {}).status_code == 403


def test_reset_clears_the_runs(excon, fake):
    jane, sent = setup(excon)
    ANSWER["meetings"] = [meeting(sent)]
    seed = find(excon)
    reset.clear_team(sent.team)
    assert not CalendarSeedItem.objects.exists() and CalendarSeed.objects.exists()
    reset.start_afresh()
    assert not CalendarSeed.objects.exists()


def test_no_planned_injects_or_role_plays_are_read(excon, fake):
    jane, sent = setup(excon)
    Inject.objects.filter(mel_id="3-13").update(body="PLANNED INJECT TEXT", status="scheduled")
    ANSWER["meetings"] = []
    find(excon)
    text = json.dumps(fake.calls[-1]["messages"])
    assert "PLANNED INJECT TEXT" not in text and "SECRET" not in text

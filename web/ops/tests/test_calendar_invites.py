"""SIMEX Calendar, step 2: guests and invitations. Guests come from the organiser mailbox's
address book (checked on save as on send), invitations travel as mail through that mailbox's
own way of sending (so the persona desk, the Network and withdrawing see them), guests answer
Yes / Maybe / No, and members never get a real date."""

import json
from datetime import datetime, timedelta

from django.utils import timezone

from ops import conf, delivery, desk, network, simclock
from ops.models import CalendarAttendee, CalendarEvent, Mail, MailAction, Message, NpcReply, Response, Sender, Team
from ops.tests.test_desk import desk_client
from ops.tests.test_leaders import leader, setup_exercise
from ops.tests.test_npc import make_npc
from ops.tests.test_ops import arm, participant


def soon(hours=3, length=1):
    a = (timezone.localtime() + timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    f = "%Y-%m-%dT%H:%M"
    return a.strftime(f), (a + timedelta(hours=length)).strftime(f)


def invite(c, layer, guests, base="/inbox", eid=None, title="Coordination meeting", start=None, end=None, **extra):
    s, e = soon()
    data = {"layer": layer, "title": title, "start": start or s, "end": end or e, "guests": guests, "guests_given": "1",
            "location": "OSOCC tent", **extra}
    if eid:
        data["id"] = eid
    return c.post(f"{base}/calendar/save/", data)


def listed(c, base="/inbox", day=None):
    day = day or timezone.localdate()
    r = c.get(f"{base}/calendar/events/", {"from": (day - timedelta(days=2)).isoformat(),
                                           "to": (day + timedelta(days=3)).isoformat()})
    assert r.status_code == 200, r.content
    return r.json()["events"]


def persona(local="gov-plaida", name="Grace Mwangi"):
    return Sender.objects.get_or_create(local_part=local, defaults={"display_name": name})[0]


def setup(excon):
    setup_exercise(excon)  # personas, the team leader persona, live armed
    persona()


def test_a_member_invites_a_persona_and_a_teammate(excon):
    setup(excon)
    jane, john = participant("a", "Jane"), participant("a", "John")
    ta = Team.objects.get(code="a")
    r = invite(jane, "t", ["p:gov-plaida", f"m:{john.person.id}"])
    assert r.status_code == 200 and r.json()["ok"], r.content
    ev = CalendarEvent.objects.get()
    rows = {a.key: a for a in ev.attendees.all()}
    assert rows["p:gov-plaida"].team is None and rows["p:gov-plaida"].persona == "gov-plaida"
    assert rows[f"m:{john.person.id}"].owner == john.person and rows[f"t:{ta.id}"].role == "organiser"
    sent = Mail.objects.get(team=ta, owner=None, folder="sent", event=ev)
    assert sent.invite == "request" and sent.thread == ev.uid and sent.subject.startswith("Invitation: Coordination meeting")
    # The persona's side: a Response matched to no inject, on the desk as waiting.
    resp = Response.objects.get(mail=sent)
    assert resp.persona == "gov-plaida" and resp.inject_id is None and resp.match == "none"
    on_desk = Mail.objects.get(team=None, folder="inbox", event=ev)
    assert on_desk.invite == "request" and desk.states([on_desk.thread])[on_desk.thread]["waiting"]
    # John has it in his own mailbox, and on his calendar.
    assert Mail.objects.filter(team=ta, owner=john.person, folder="inbox", event=ev).exists()
    assert [e["title"] for e in listed(john, "/inbox/me")] == ["Coordination meeting"]
    assert listed(john, "/inbox/me")[0]["answerable"]
    # The desk answers as the persona: a reply in the team's conversation, the desk's waiting cleared.
    dk = desk_client()
    assert [e["title"] for e in listed(dk, "/inbox/desk")] == ["Coordination meeting"]
    r = dk.post("/inbox/desk/calendar/respond/", {"event": ev.id, "layer": "p:gov-plaida", "answer": "accepted",
                                                  "note": "See you there."}, HTTP_X_REQUESTED_WITH="fetch")
    assert r.json()["ok"], r.content
    assert CalendarAttendee.objects.get(event=ev, key="p:gov-plaida").response == "accepted"
    reply = Mail.objects.get(team=ta, owner=None, folder="inbox", event=ev, invite="reply")
    assert reply.subject == "Accepted: Coordination meeting" and reply.thread == sent.thread and "See you there." in reply.body
    assert Message.objects.get(pk=reply.message_id).kind == "reply"
    assert not desk.states([on_desk.thread])[on_desk.thread]["waiting"]
    # John answers from his own mailbox: to the team inbox, the organiser.
    r = john.post("/inbox/me/calendar/respond/", {"event": ev.id, "layer": f"o{john.person.id}", "answer": "tentative",
                                                  "next": f"/inbox/me/t/{ev.uid}/"})
    assert r.status_code == 302 and r["Location"] == f"/inbox/me/t/{ev.uid}/"
    assert Mail.objects.filter(team=ta, owner=None, folder="inbox", event=ev, invite="reply", subject__startswith="Maybe").exists()
    assert MailAction.objects.filter(action="cal_rsvp", owner=john.person).exists()
    assert {g["key"]: g["response"] for g in listed(jane)[0]["guests"]} == {"p:gov-plaida": "accepted",
                                                                             f"m:{john.person.id}": "tentative"}


def test_the_thread_shows_the_card_and_the_list_a_chip(excon):
    setup(excon)
    jane, john = participant("a", "Jane"), participant("a", "John")
    invite(jane, "t", [f"m:{john.person.id}", "p:gov-plaida"])
    ev = CalendarEvent.objects.get()
    page = john.get(f"/inbox/me/t/{ev.uid}/").content.decode()
    assert "gm-invite-card" in page and "Going?" in page and 'name="layer" value="o%d"' % john.person.id in page
    assert "gm-invite" in john.get("/inbox/me/").content.decode()
    # The organiser's own copy: the card without the buttons.
    page = jane.get(f"/inbox/t/{ev.uid}/").content.decode()
    assert "gm-invite-card" in page and "Going?" not in page
    dk = desk_client()
    on_desk = Mail.objects.get(team=None, folder="inbox", event=ev)
    page = dk.get(f"/inbox/desk/t/{on_desk.thread}/").content.decode()
    assert "Going?" in page and 'value="p:gov-plaida"' in page
    page = leader("a").get(f"/inbox/team/t/{ev.uid}/").content.decode()  # read-only: no buttons
    assert "gm-invite-card" in page and "Going?" not in page


def test_guests_are_who_the_mailbox_may_write_to(excon):
    setup(excon)
    jane, la = participant("a", "Jane"), leader("a")
    tb = Team.objects.get(code="b")
    assert invite(jane, "t", [f"t:{tb.id}"]).status_code == 400  # team-to-team off
    assert invite(jane, "t", ["p:undac-b-tl"]).status_code == 400  # another team's leader persona
    assert invite(jane, "t", ["m:999999"]).status_code == 400
    r = invite(la, f"o{la.person.id}", ["p:gov-plaida"])
    assert r.status_code == 400 and "can't invite" in r.json()["error"]  # a leader writes to the team only
    assert not CalendarEvent.objects.exists()
    conf.put("team_to_team", "1")
    assert invite(jane, "t", [f"t:{tb.id}"]).json()["ok"]
    ev = CalendarEvent.objects.get()
    assert ev.attendees.filter(team=tb, owner=None).exists()
    assert Mail.objects.filter(team=tb, owner=None, folder="inbox", event=ev).exists()
    bea = participant("b", "Bea")
    e = listed(bea)[0]
    assert e["title"] == "Coordination meeting" and e["answerable"] and not e["editable"]
    assert bea.post("/inbox/calendar/respond/", {"event": ev.id, "layer": "t", "answer": "declined"},
                    HTTP_X_REQUESTED_WITH="fetch").json()["ok"]
    assert Mail.objects.filter(team=Team.objects.get(code="a"), folder="inbox", event=ev, invite="reply").exists()
    # Team C never sees it.
    assert listed(participant("c", "Cy")) == []


def test_the_leader_invites_the_team_and_members(excon):
    setup(excon)
    la, jane = leader("a"), participant("a", "Jane")
    ta = Team.objects.get(code="a")
    r = invite(la, f"o{la.person.id}", [f"t:{ta.id}", f"m:{jane.person.id}"])
    assert r.json()["ok"], r.content
    ev = CalendarEvent.objects.get()
    assert Message.objects.filter(kind="leader", mails__event=ev).exists()
    assert Mail.objects.filter(team=ta, owner=None, folder="inbox", event=ev).exists()
    assert {e["title"] for e in listed(jane)} == {"Coordination meeting"}
    # Jane answers from the team inbox: to the team leader persona, in the leader's Inbox.
    assert jane.post("/inbox/calendar/respond/", {"event": ev.id, "layer": "t", "answer": "accepted"},
                     HTTP_X_REQUESTED_WITH="fetch").json()["ok"]
    assert Mail.objects.filter(team=ta, owner=la.person, folder="inbox", event=ev, invite="reply").exists()
    assert not Mail.objects.filter(team=None, event=ev).exists()  # never the team leader persona's on the desk


def test_changes_removals_and_cancelling(excon):
    setup(excon)
    jane, john = participant("a", "Jane"), participant("a", "John")
    ta = Team.objects.get(code="a")
    invite(jane, "t", [f"m:{john.person.id}", "p:gov-plaida"])
    ev = CalendarEvent.objects.get()
    john.post("/inbox/me/calendar/respond/", {"event": ev.id, "layer": f"o{john.person.id}", "answer": "accepted"})
    s, e = soon(hours=5)
    r = invite(jane, "t", [f"m:{john.person.id}", "p:gov-plaida"], eid=ev.id, start=s, end=e)
    assert r.json()["ok"], r.content
    ev.refresh_from_db()
    assert ev.sequence == 1
    assert CalendarAttendee.objects.get(event=ev, key=f"m:{john.person.id}").response == "needs_action"  # asked again
    assert Mail.objects.filter(team=ta, owner=john.person, event=ev, invite="update").exists()
    # Dragging (no guests sent) keeps them.
    jane.post("/inbox/calendar/save/", {"id": ev.id, "layer": "t", "title": "Coordination meeting", "start": s, "end": e,
                                        "description": "Bring the 3W."})
    assert ev.attendees.count() == 3
    # The persona taken off: a cancellation to it, its row gone.
    invite(jane, "t", [f"m:{john.person.id}"], eid=ev.id, start=s, end=e)
    assert not ev.attendees.filter(key="p:gov-plaida").exists()
    assert Mail.objects.filter(team=None, folder="inbox", event=ev, invite="cancel").exists()
    # Deleting: cancelled, struck through on John's calendar, with a cancellation.
    assert jane.post(f"/inbox/calendar/delete/{ev.id}/").json()["ok"]
    ev.refresh_from_db()
    assert ev.status == "cancelled"
    assert Mail.objects.filter(team=ta, owner=john.person, event=ev, invite="cancel").exists()
    mine = listed(john, "/inbox/me")[0]
    assert mine["status"] == "cancelled" and not mine["answerable"]
    assert john.post("/inbox/me/calendar/respond/", {"event": ev.id, "layer": f"o{john.person.id}", "answer": "accepted"},
                     HTTP_X_REQUESTED_WITH="fetch").status_code == 400


def test_ai_personas_never_answer_invitations(excon):
    setup(excon)
    conf.put("npc_enabled", "1")
    conf.put("npc_scope", "all")
    Sender.objects.filter(local_part="gov-plaida").delete()
    make_npc()  # gov-plaida, an AI persona
    jane = participant("a", "Jane")
    invite(jane, "t", ["p:gov-plaida"])
    assert Response.objects.filter(persona="gov-plaida").exists() and not NpcReply.objects.exists()
    # An ordinary message to it still queues an answer.
    jane.post("/inbox/send/", {"to": "p:gov-plaida", "subject": "Roads", "body": "Which roads are open?"})
    assert NpcReply.objects.count() == 1


def test_the_desk_invites_a_team_and_a_withdrawal_takes_it_back(excon):
    setup(excon)
    dk = desk_client()
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    r = invite(dk, "p:gov-plaida", [f"t:{ta.id}", f"t:{tb.id}"], base="/inbox/desk")
    assert r.json()["ok"], r.content
    ev = CalendarEvent.objects.get()
    assert ev.team is None and ev.organiser_key == "p:gov-plaida"
    msgs = list(Message.objects.filter(kind="desk", mails__event=ev).distinct())
    assert len(msgs) == 2 and all(Mail.objects.filter(message=m, team=None, folder="sent", event=ev).exists() for m in msgs)
    jane = participant("a", "Jane")
    assert listed(jane)[0]["answerable"]
    m_a = next(m for m in msgs if m.inbox_team_id == ta.id)
    delivery.withdraw(m_a, [ta.id])
    assert not ev.attendees.filter(team=ta).exists() and listed(jane) == []
    ev.refresh_from_db()
    assert ev.status == "confirmed"  # Team B is still invited
    delivery.withdraw(next(m for m in msgs if m.inbox_team_id == tb.id), [tb.id])
    ev.refresh_from_db()
    assert ev.status == "cancelled"


def test_the_network_counts_an_invitation_once(excon):
    setup(excon)
    jane = participant("a", "Jane")
    invite(jane, "t", ["p:gov-plaida"])
    ss = [s for s in network.graph()["sends"] if any("gov-plaida" in t for t in s["to"])]
    assert len(ss) == 1 and ss[0]["f"] == f"m:{jane.person.id}"


def test_members_never_get_a_real_date(excon):
    setup(excon)
    simclock.set_clock(timezone.make_aware(datetime(2035, 5, 20, 9, 0)), 1)
    jane, john, la = participant("a", "Jane"), participant("a", "John"), leader("a")
    real = timezone.localdate()
    r = invite(jane, "t", [f"m:{john.person.id}"], start="2035-05-20T11:00", end="2035-05-20T12:00")
    assert r.json()["ok"], r.content
    ev = CalendarEvent.objects.get()
    mail = Mail.objects.get(team=ev.team, owner=john.person, event=ev)
    assert "Sun 20 May 2035, 11:00 – 12:00 (scenario time)" in mail.body and str(real.year) not in mail.body + mail.subject
    for url in (f"/inbox/me/t/{ev.uid}/", "/inbox/me/", "/inbox/me/calendar/"):
        page = john.get(url).content.decode()
        assert real.isoformat() not in page and real.strftime("%b %-d, %Y") not in page, url
    body = json.dumps(listed(john, "/inbox/me", day=datetime(2035, 5, 20).date()))
    assert "2035-05-20T11:00" in body and str(real.year) not in body
    # The leader's card shows the real time and the scenario's.
    page = la.get(f"/inbox/members/{john.person.id}/t/{ev.uid}/").content.decode()
    assert "Sun 20 May 2035, 11:00" in page and str(real.year) in page


def test_create_event_from_a_conversation(excon):
    setup(excon)
    jane = participant("a", "Jane")
    jane.post("/inbox/send/", {"to": "p:gov-plaida", "subject": "Meeting on the roads", "body": "Can we meet at 10?"})
    sent = Mail.objects.get(folder="sent", subject="Meeting on the roads")
    page = jane.get(f"/inbox/t/{sent.thread}/").content.decode()
    assert f"/inbox/calendar/?from={sent.thread}" in page
    page = jane.get(f"/inbox/calendar/?from={sent.thread}").content.decode()
    data = json.loads(page.split('id="simex-cal" type="application/json">')[1].split("</script>")[0])
    assert data["prefill"]["title"] == "Meeting on the roads" and [g["key"] for g in data["prefill"]["guests"]] == ["p:gov-plaida"]
    r = invite(jane, "t", ["p:gov-plaida"], source=data["prefill"]["source"])
    ev = CalendarEvent.objects.get()
    assert r.json()["ok"] and ev.source_mail == sent
    page = jane.get(f"/inbox/t/{sent.thread}/").content.decode()
    assert "On the calendar" in page
    assert listed(jane)[0]["source"]["url"] == f"/inbox/t/{sent.thread}/"

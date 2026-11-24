"""SIMEX Calendar, step 4: personas and the persona desk. The organisation a persona speaks for
(shown to the teams; its category never), who on the desk plays a persona's meeting (the desk's
eyes only), ExCon's Compose → Invite to a meeting, AI personas leaving invitations to the desk,
the desk's notices, and the Calendar tab."""

import io
import json

import openpyxl
from django.utils import timezone

from ops import desk, desk_board, senders_import
from ops.models import CalendarEvent, DeskConversation, DeskNotice, Mail, Message, NpcProfile, NpcReply, Response, Sender, Team
from ops.tests.test_calendar_invites import invite, listed, setup
from ops.tests.test_desk import desk_client
from ops.tests.test_leaders import leader
from ops.tests.test_ops import participant


def orgs(excon):
    setup(excon)
    Sender.objects.filter(local_part="gov-plaida").update(organisation="Plaida Province", category="SECRETCAT",
                                                          notes="SECRET NOTE")


def test_organisation_round_trips_through_the_senders_list(excon):
    orgs(excon)
    wb = openpyxl.load_workbook(io.BytesIO(senders_import.export()))
    rows = list(wb.active.iter_rows(values_only=True))
    assert rows[0][3] == "Organisation (shown to teams)"
    assert next(r for r in rows if r[0] == "gov-plaida")[3] == "Plaida Province"
    # A file with it changes it (flagged as replacing what was set); a plain "Organisation" column stays ExCon's notes.
    csv = "Username,Organisation (shown to teams),Organisation\ngov-plaida,Plaida County,Use for roads\n".encode()
    items, _ = senders_import.plan(senders_import.read("s.csv", csv))
    ch = {c["field"]: c for c in items[0]["changes"]}
    assert ch["organisation (shown to teams)"]["new"] == "Plaida County" and ch["organisation (shown to teams)"]["replaces"]
    assert ch["organisation / use"]["new"] == "Use for roads"
    senders_import.apply(items, {items[0]["row"]["line"]})
    s = Sender.objects.get(local_part="gov-plaida")
    assert s.organisation == "Plaida County" and s.notes == "Use for roads"
    old = senders_import.read("s.csv", b"Username,Organisation\ngov-plaida,Old style\n")
    assert old[0]["notes"] == "Old style" and old[0]["organisation"] == ""
    # The Senders tab edits it.
    excon.post("/senders/", {"id": s.id, "local_part": "gov-plaida", "display_name": "Grace Mwangi", "organisation": "Plaida",
                             "category": "x", "notes": "", "active": "1"})
    assert Sender.objects.get(pk=s.pk).organisation == "Plaida"


def test_teams_see_the_organisation_never_the_category(excon):
    orgs(excon)
    jane = participant("a", "Jane")
    invite(jane, "t", ["p:gov-plaida"])
    ev = CalendarEvent.objects.get()
    e = listed(jane)[0]
    assert {g["key"]: g["org"] for g in e["guests"]} == {"p:gov-plaida": "Plaida Province"}
    for url in (f"/inbox/t/{ev.uid}/", "/inbox/", "/inbox/calendar/"):
        page = jane.get(url).content.decode()
        assert "SECRETCAT" not in page and "SECRET NOTE" not in page, url
    assert "Plaida Province" in jane.get(f"/inbox/t/{ev.uid}/").content.decode()
    assert "Plaida Province" in jane.get("/inbox/calendar/").content.decode()  # in the address book, for chips


def test_who_plays_it_is_the_desks_own(excon):
    orgs(excon)
    jane, la, anna = participant("a", "Jane"), leader("a"), desk_client()
    invite(jane, "t", ["p:gov-plaida"])
    ev = CalendarEvent.objects.get()
    e = listed(anna, "/inbox/desk")[0]
    assert e["desk"]["owner"] == "" and e["desk"]["thread"]
    thread = e["desk"]["thread"]
    # Take it from the calendar (desk.mark, as the Assign board does).
    assert anna.post("/inbox/desk/mark/", {"threads": thread, "act": "take"}, HTTP_X_REQUESTED_WITH="fetch").json()["ok"]
    assert DeskConversation.objects.get(thread=thread).owner == anna.person.user
    e = listed(anna, "/inbox/desk")[0]
    assert e["desk"]["mine"] and e["desk"]["owner"] == "Anna Desk"
    board = desk_board.board(anna.person.user)
    assert any(thread in json.dumps(col) for col in board["columns"])
    # Give it to someone else.
    bob = desk_client("Bob Desk", "bob.desk")
    assert anna.post("/inbox/desk/mark/", {"threads": thread, "act": "assign", "assignee": bob.person.user.id},
                     HTTP_X_REQUESTED_WITH="fetch").json()["ok"]
    assert DeskConversation.objects.get(thread=thread).owner == bob.person.user
    page = anna.get(f"/inbox/desk/t/{thread}/").content.decode()
    assert "Played by <b>Bob Desk</b>" in page
    # The teams never see it.
    assert "desk" not in listed(jane)[0] and "desk" not in listed(la, "/inbox/team")[0]
    for c, url in ((jane, f"/inbox/t/{ev.uid}/"), (la, f"/inbox/team/t/{ev.uid}/")):
        page = c.get(url).content.decode()
        assert "Played by" not in page and "Bob Desk" not in page, url


def test_compose_invites_to_a_meeting_as_a_persona(excon):
    orgs(excon)
    ta = Team.objects.get(code="a")
    jane = participant("a", "Jane")
    start = (timezone.localtime() + timezone.timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    f = "%Y-%m-%dT%H:%M"
    r = excon.post("/compose/meeting/", {"from": "p:gov-plaida", "title": "Road briefing", "start": start.strftime(f),
                                         "end": (start + timezone.timedelta(hours=1)).strftime(f), "mode": "vtc",
                                         "to": [f"t:{ta.id}"]})
    assert r.status_code == 302
    ev = CalendarEvent.objects.get()
    assert ev.team is None and ev.organiser_key == "p:gov-plaida" and ev.created_by is None
    m = Message.objects.filter(kind="desk", mails__event=ev, mails__team=ta).distinct().get()
    assert m.author is None and Mail.objects.filter(team=None, folder="sent", message=m, event=ev).exists()
    assert Mail.objects.get(team=ta, owner=None, folder="inbox", event=ev).invite == "request"
    assert listed(jane)[0]["answerable"]
    assert b"Invite to a meeting" in excon.get("/compose/").content
    assert excon.post("/compose/meeting/", {"from": "p:gov-plaida", "title": "x", "start": "", "end": "",
                                            "to": [f"t:{ta.id}"]}).status_code == 302
    assert CalendarEvent.objects.count() == 1  # refused, with the reason


def test_ai_personas_leave_invitations_to_the_desk(excon):
    from ops import conf
    from ops.tests.test_npc import make_npc
    orgs(excon)
    Sender.objects.filter(local_part="gov-plaida").delete()
    conf.put("npc_enabled", "1")
    conf.put("npc_scope", "all")
    p = make_npc()
    jane, anna = participant("a", "Jane"), desk_client()
    invite(jane, "t", ["p:gov-plaida"])
    ev = CalendarEvent.objects.get()
    resp = Response.objects.get(persona="gov-plaida")
    assert not NpcReply.objects.exists()
    # Had an answer been queued for it (e.g. before this rule), the desk's answer skips it.
    from ops import inbound
    q = NpcReply.objects.create(npc=p, team=resp.team, response=resp, thread_key=inbound.thread_key(resp)[:300],
                                due_at=timezone.now())
    anna.post("/inbox/desk/calendar/respond/", {"event": ev.id, "layer": "p:gov-plaida", "answer": "accepted"},
              HTTP_X_REQUESTED_WITH="fetch")
    q.refresh_from_db()
    assert q.status == "skipped"


def test_the_desk_hears_of_invitations_changes_and_cancellations(excon):
    orgs(excon)
    jane, anna = participant("a", "Jane"), desk_client()
    invite(jane, "t", ["p:gov-plaida"])
    ev = CalendarEvent.objects.get()
    n = DeskNotice.objects.filter(user=anna.person.user)
    assert n.filter(kind="calendar").count() == 1 and not n.filter(kind="reply").exists()
    assert "Team A invites Grace Mwangi" in n.get(kind="calendar").title
    s = (timezone.localtime(ev.starts_at) + timezone.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    e = (timezone.localtime(ev.ends_at) + timezone.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    invite(jane, "t", ["p:gov-plaida"], eid=ev.id, start=s, end=e)
    jane.post(f"/inbox/calendar/delete/{ev.id}/")
    titles = sorted(x.title for x in n.filter(kind="calendar"))
    assert len(titles) == 3 and any("changed" in t for t in titles) and any("cancelled" in t for t in titles)


def test_the_calendar_tab(excon):
    setup(excon)
    anna, la = desk_client(), leader("a")
    page = anna.get("/inbox/desk/calendar/").content.decode()
    assert 'href="/inbox/desk/calendar/" class="on"' in page and 'href="/inbox/desk/" class=""' in page
    page = la.get("/inbox/calendar/").content.decode()
    assert 'href="/inbox/calendar/" class="on"' in page
    page = participant("a", "Jane").get("/inbox/calendar/").content.decode()
    assert 'href="/inbox/calendar/" class="on"' in page
    assert 'href="/inbox/desk/calendar/" class=""' in anna.get("/inbox/desk/").content.decode()


def test_a_calendar_must_be_chosen(excon):
    setup(excon)
    jane, anna = participant("a", "Jane"), desk_client()
    for c, base in ((jane, "/inbox"), (anna, "/inbox/desk")):
        r = invite(c, "", [], base=base)
        assert r.status_code == 400 and r.json()["error"] == "Choose the calendar it goes on.", base
        assert invite(c, "nope", [], base=base).status_code == 400
    assert not CalendarEvent.objects.exists()
    page = anna.get("/inbox/desk/calendar/").content.decode()
    assert '<option value="" disabled>Choose a calendar…</option>' in page


def _prefill(c, url):
    page = c.get(url).content.decode()
    return json.loads(page.split('id="simex-cal" type="application/json">')[1].split("</script>")[0])["prefill"]


def test_create_event_picks_the_mailbox_of_the_conversation(excon):
    setup(excon)
    jane = participant("a", "Jane")
    jane.post("/inbox/me/send/", {"to": "p:gov-plaida", "subject": "Roads", "body": "Meet at 10?", "from": "me"})
    mine = Mail.objects.get(team__code="a", owner=jane.person, folder="sent")
    assert _prefill(jane, f"/inbox/me/calendar/?from={mine.thread}")["layer"] == f"o{jane.person.id}"  # My calendar
    team_copy = Mail.objects.get(team__code="a", owner=None, folder="sent")
    assert _prefill(jane, f"/inbox/calendar/?from={team_copy.thread}")["layer"] == "t"

"""The persona desk's Scheduled (views_desk_npc, npc.edit / reschedule / cancel / send_now): an
AI persona's answer is drafted as soon as it is queued, shown in the conversation and in
Scheduled, and a person can edit it, send it now, change its time or cancel it."""

from datetime import timedelta

from django.utils import timezone

from ops import desk, npc
from ops.models import Mail, Message, NpcReply
from ops.tests.test_desk import FETCH, desk_client, setup
from ops.tests.test_npc import due_now, fake, make_npc, write_to  # noqa: F401 (fixture)
from ops.tests.test_ops import participant


def desk_thread(r):
    return Mail.objects.get(team=None, owner=None, folder="inbox", desk_response=r).thread


def test_team_message_drafted_at_once_and_edited_on_the_desk(excon, fake):
    make_npc("bureaucrat")
    pa = participant("test", "Jo")
    r = write_to(pa)
    d = desk_client()
    t = desk_thread(r)
    job = NpcReply.objects.get()
    # Before the draft: listed in Scheduled, the conversation says SIMEX AI is drafting.
    page = d.get("/inbox/desk/?folder=scheduled")
    assert [x["thread"] for x in page.context["rows"]] == [t]
    assert "drafting Grace Mwangi" in page.content.decode()
    npc.tick()
    job.refresh_from_db()
    assert job.status == "queued" and "Km 12" in job.body and job.due_at > timezone.now() + timedelta(minutes=30)
    # The list says who answers and when, the filter finds it, the side bar counts it.
    inbox = d.get("/inbox/desk/")
    assert "Grace Mwangi (AI) sends at" in inbox.content.decode()
    assert inbox.context["desk_side"]["scheduled"]["n"] == 1
    assert [x["thread"] for x in d.get("/inbox/desk/?folder=inbox&st=npc").context["rows"]] == [t]
    # The conversation shows the draft, editable.
    th = d.get(f"/inbox/desk/t/{t}/").content.decode()
    assert 'id="scheduled-answer"' in th and "The coast road is cut at Km 12" in th and "Read the draft" in th
    # A person edits it: theirs from now on, never rewritten.
    assert d.post(f"/inbox/desk/answers/{job.id}/", {"act": "save", "body": "Dear team, the road is shut. Grace"},
                  headers=FETCH).json()["ok"]
    job.refresh_from_db()
    assert job.edited_by == "Anna Desk" and job.body.startswith("Dear team, the road")
    calls = len(fake.calls)
    npc.tick()
    assert len(fake.calls) == calls
    # Changing the time, then sending now.
    assert d.post(f"/inbox/desk/answers/{job.id}/", {"act": "time", "minutes": "5"}, headers=FETCH).json()["ok"]
    job.refresh_from_db()
    assert timedelta(minutes=4) < job.due_at - timezone.now() <= timedelta(minutes=5)
    j = d.post(f"/inbox/desk/answers/{job.id}/", {"act": "send", "body": "Dear team, the road is shut until Friday. Grace"},
               headers=FETCH).json()
    assert j["ok"], j
    job.refresh_from_db()
    m = job.message
    assert job.status == "sent" and m.npc and m.body.endswith("Friday. Grace") and len(fake.calls) == calls
    assert Mail.objects.filter(team=r.team, owner=None, folder="inbox", message=m).exists()
    assert "Written by the AI persona, edited by Anna Desk" in d.get(f"/inbox/desk/t/{t}/").content.decode()
    assert not d.get("/inbox/desk/?folder=scheduled").context["rows"]
    # Moved on: nothing changes.
    assert d.post(f"/inbox/desk/answers/{job.id}/", {"act": "cancel"}, headers=FETCH).status_code == 400


def test_a_later_message_rewrites_the_draft_unless_a_person_edited_it(excon, fake):
    make_npc("bureaucrat")
    pa = participant("test", "Jo")
    first = write_to(pa)
    npc.tick()
    job = NpcReply.objects.get()
    assert job.draft_for == first.id
    second = write_to(pa, "Re: Roads", "Hello? Urgent.", parent=Mail.objects.get(pk=first.mail_id))
    job.refresh_from_db()
    assert job.response == second and not npc.draft_current(job)
    npc.tick()
    job.refresh_from_db()
    assert job.draft_for == second.id and "Hello? Urgent." in str(fake.calls[-1]["messages"])
    # Edited, then the team writes again: held for the person to look at.
    npc.edit(job, "My own words.", "Anna Desk")
    write_to(pa, "Re: Roads", "Anyone?", parent=Mail.objects.get(pk=first.mail_id))
    job.refresh_from_db()
    assert job.status == "held" and "edited this answer" in job.note and job.body == "My own words."
    d = desk_client()
    t = desk_thread(first)
    assert d.get("/inbox/desk/").context["desk_side"]["scheduled"]["held"] == 1
    assert desk.states([t])[t]["section"] == desk.ATTENTION
    # Held: scheduling it again puts it back in the queue.
    assert d.post(f"/inbox/desk/answers/{job.id}/", {"act": "time", "minutes": "15"}, headers=FETCH).json()["ok"]
    job.refresh_from_db()
    assert job.status == "queued"


def test_cancel_to_answer_myself_and_due_drafts_go_without_a_person(excon, fake):
    make_npc("bureaucrat")
    pa = participant("test", "Jo")
    r = write_to(pa)
    d = desk_client()
    t = desk_thread(r)
    job = NpcReply.objects.get()
    assert d.post(f"/inbox/desk/answers/{job.id}/", {"act": "cancel"}, headers=FETCH).json()["ok"]
    job.refresh_from_db()
    assert job.status == "skipped" and "Anna Desk" in job.note
    st = desk.states([t])[t]
    assert st["section"] == desk.ATTENTION and st["npc"]["status"] == "skipped"
    assert not d.get("/inbox/desk/?folder=scheduled").context["rows"]
    due_now()
    npc.tick()
    assert not Message.objects.filter(npc=True).exists()
    # Another one: drafted, and when due it goes as it is.
    r2 = write_to(pa, "Bridges", "Is the bridge open?")
    npc.tick()
    due_now()
    calls = len(fake.calls)
    assert npc.tick() == 1 and len(fake.calls) == calls
    assert NpcReply.objects.get(response=r2).status == "sent"


def test_persona_mail_scheduled_on_the_desk(excon, fake):
    setup(excon)
    make_npc("bureaucrat")
    d = desk_client()
    assert d.post("/inbox/desk/send/", {"from": "p:cnn", "to": "p:gov-plaida", "subject": "Support needed",
                                        "body": "Anyone on the ground?"}, headers=FETCH).json()["ok"]
    got = Mail.objects.get(team=None, folder="inbox", subject="Support needed")
    st = desk.states([got.thread])[got.thread]
    assert st["npc"]["status"] == "queued" and st["npc"]["persona_mail"] and st["section"] == desk.LATER
    assert [x["thread"] for x in d.get("/inbox/desk/?folder=scheduled").context["rows"]] == [got.thread]
    npc.tick()
    th = d.get(f"/inbox/desk/t/{got.thread}/").content.decode()
    assert 'id="scheduled-answer"' in th and "Km 12" in th
    job = NpcReply.objects.get()
    assert d.post(f"/inbox/desk/answers/{job.id}/", {"act": "send", "body": ""}, headers=FETCH).json()["ok"]
    job.refresh_from_db()
    assert job.status == "sent" and job.message.kind == desk.PERSONA_KIND and not job.edited_by
    # Persona desk only.
    assert participant("test", "Jo").post(f"/inbox/desk/answers/{job.id}/", {"act": "cancel"}).status_code in (302, 403)


def test_held_answer_scheduled_by_a_person_goes(excon, fake):
    make_npc("instant", auto_send=False)
    write_to(participant("test", "Jo"))
    due_now()
    npc.tick()
    job = NpcReply.objects.get()
    assert job.status == "held"
    d = desk_client()
    assert d.post(f"/inbox/desk/answers/{job.id}/", {"act": "time", "minutes": "5"}, headers=FETCH).json()["ok"]
    due_now()
    npc.tick()
    job.refresh_from_db()
    assert job.status == "sent" and job.message.npc

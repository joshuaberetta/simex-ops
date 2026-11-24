"""ChatSIMEX (ops/desk_chat.py, views_desk_chat.py): the persona desk's chat with SIMEX AI. What it
may read (the desk's mailbox, the teams' internal mailboxes as the desk reads them), how each
source is labelled per team, "who is" lookups, the draft check (teams never cross), the private
chats and the routes. A fake model; nothing here is exercise content."""

import json
import re
from types import SimpleNamespace as NS

import pytest
from django.test import Client

from ops import ai, conf, desk_chat, kb, reset
from ops.models import (AiCall, DeskChat, DeskChatTurn, Mail, NpcProfile, Person, RolePlay, Sender, Team)
from ops.tests.test_desk import desk_client, setup, team_writes
from ops.tests.test_leaders import leader
from ops.tests.test_ops import deliver_now, participant
from ops.tests.test_suggest import DOCS, bundle

FETCH = {"x-requested-with": "fetch"}


class Script:
    """A fake model that plays a list of steps: each a list of (tool, input) or a callable
    giving one (so a step can use ids made during the test)."""

    def __init__(self, steps):
        self.steps, self.calls = steps, []
        self.messages = NS(create=self.create)
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=str)))
        step = self.steps[min(len(self.calls), len(self.steps)) - 1]
        step = step() if callable(step) else step
        content = [NS(type="tool_use", id=f"t{len(self.calls)}{i}", name=n, input=inp) for i, (n, inp) in enumerate(step)]
        usage = NS(input_tokens=500, output_tokens=50, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return NS(model=kw["model"], stop_reason="tool_use", stop_details=None, usage=usage, content=content)


@pytest.fixture
def chat_on():
    conf.put("ai_enabled", "1")
    desk_chat.RUN_INLINE = True
    yield
    desk_chat.RUN_INLINE = False


def world(excon):
    """Teams A and B have the 3-13 inject and wrote back to its persona; Team A has internal mail."""
    setup(excon)
    kb.apply(kb.read_bundle(bundle(DOCS)))
    deliver_now("3-13")
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    _, ra = team_writes(participant("a", "Jane"), ta, "Is the Zorba bridge open?")
    _, rb = team_writes(participant("b", "Omar"), tb, "We heard the Zorba bridge is closed.")
    inside = Mail.objects.create(team=ta, owner=None, folder="inbox", thread="int-1", subject="Bridge plan",
                                 body="Internal: we will cross the Zorba bridge at dawn.", from_name="Jane")
    member = Person.objects.get(name="Jane")
    own = Mail.objects.create(team=ta, owner=member, folder="inbox", thread="own-1", subject="Zorba note",
                              body="My own Zorba note.", from_name="Jane")
    desk_a = Mail.objects.get(team=None, owner=None, folder="inbox", desk_response=ra)
    desk_b = Mail.objects.get(team=None, owner=None, folder="inbox", desk_response=rb)
    return NS(ta=ta, tb=tb, inside=inside, own=own, desk_a=desk_a, desk_b=desk_b)


def _results(call):
    return json.dumps(call["messages"][-1])


def test_chat_reads_desk_and_team_mail_and_checks_the_draft(excon, chat_on):
    w = world(excon)
    fake = Script([
        [("search_mail", {"query": "Zorba bridge", "where": "all"}), ("search", {"query": "Zorba"})],
        [("who", {"name": "donotreply"}), ("read", {"ref": f"mail:{w.own.id}"})],
        lambda: [("answer", {
            "answer": f"Team B was told nothing yet (mail:{w.desk_b.id}). Team A plans to cross (mail:{w.inside.id}).",
            "sources": [{"ref": f"mail:{w.desk_b.id}", "what": "Team B's message"}],
            "draft": {"team": "Team A", "persona": "donotreply", "body": "Hello, the bridge status is being checked.",
                      "facts": [{"fact": "Team B heard it is closed", "source": f"mail:{w.desk_b.id}"},
                                {"fact": "They will cross at dawn", "source": f"mail:{w.inside.id}"},
                                {"fact": "Flooding", "source": "doc:d2"},
                                {"fact": "They asked about the bridge", "source": f"mail:{w.desk_a.id}"}]},
            "hold_back": [], "not_available": ["bridge status"], "questions": []})],
    ])
    conf.put("personal_mailboxes", "0")  # members' own mailboxes: out of the desk's reach
    ai.set_client(fake)
    from ops.models import MailAction
    actions = MailAction.objects.count()
    d = desk_client()
    c = d.post("/inbox/desk/chat/", {"thread": w.desk_a.thread}, headers=FETCH).json()["chat"]
    assert c["team"] == w.ta.id and c["thread"] == w.desk_a.thread  # pinned: the conversation's team
    t = d.post(f"/inbox/desk/chat/{c['id']}/ask/", {"question": "How do I answer this?", "thread": w.desk_a.thread},
               headers=FETCH).json()["turn"]
    assert t["status"] == "done", t["error"]
    first = fake.calls[0]["messages"][0]["content"][0]["text"]
    assert "THE PINNED CONVERSATION" in first and "Is the Zorba bridge open?" in first and "How do I answer this?" in first
    found = _results(fake.calls[1])
    # the desk's mailbox, Team B's labelled as another team's; Team A's internal mail as team-internal
    assert f"mail:{w.desk_a.id} " in found and f"mail:{w.desk_b.id} " in found
    assert "other team: don't pass on to Team A" in found
    assert f"mail:{w.inside.id} | Team A internal | team inbox" in found and "the persona doesn't know this" in found
    assert f"mail:{w.own.id} " not in found  # a member's own mailbox: only with personal mailboxes
    assert "doc:d1" in found and "the participants have it" in found
    read = _results(fake.calls[2])
    assert "No mail" in read and "My own Zorba note" not in read
    assert "persona:donotreply" in read and "inject:3-13" in read
    tt = DeskChatTurn.objects.get()
    assert [x["tool"] for x in tt.trace] == ["search_mail", "search", "who", "read"]
    draft = t["body"]["draft"]
    states = [f["state"] for f in draft["facts"]]
    assert states == ["not_given", "not_given", "upcoming", "known"]
    assert any("teams never cross" in x for x in draft["warnings"])
    assert any("can't know it" in x for x in draft["warnings"])
    assert draft["thread"] == w.desk_a.thread and draft["mail"] == w.desk_a.id
    assert AiCall.objects.filter(purpose="chat").count() == 3
    # nothing read or recorded in the teams' mailboxes
    w.inside.refresh_from_db()
    assert w.inside.read_at is None
    assert MailAction.objects.count() == actions


def test_members_own_mailboxes_with_personal_mailboxes(excon, chat_on):
    w = world(excon)
    conf.put("personal_mailboxes", "1")
    sc = desk_chat.Scope(w.ta)
    out = desk_chat.tool_search_mail(sc, "Zorba", "team")
    assert f"mail:{w.own.id} " in out and "Jane's own" in out
    assert "Team B internal" not in out
    assert desk_chat.tool_read(sc, f"mail:{w.own.id}").count("My own Zorba note") == 1


def test_labels_for_every_team_in_scope(excon, chat_on):
    w = world(excon)
    from ops.models import Inject
    inj = Inject.objects.exclude(mel_id="3-13").exclude(status="cancelled").first()
    sc = desk_chat.Scope()
    label = sc.label(lambda tl: tl.inject("3-13"))
    assert "has it" in label and "Team A" in label and "NOT YET" not in label
    label = desk_chat.Scope(w.tb).label(lambda tl: tl.inject(inj.mel_id))
    assert "NOT YET" in label or "not for this team" in label


def test_who_is_a_name(excon, chat_on):
    w = world(excon)
    s = Sender.objects.create(local_part="salma.haddad", display_name="Dr Salma Haddad", notes="Plays it tough")
    NpcProfile.objects.create(sender=s, organisation="WHO", role="Health lead", knowledge="Clinic list")
    RolePlay.objects.create(key="9-50", serial="9-50", title="Checkpoint", role="Driver Kofi", script="You are Kofi, the driver.")
    sc = desk_chat.Scope(w.ta)
    out = desk_chat.tool_who(sc, "Salma")
    assert out.startswith("persona:salma.haddad | Dr Salma Haddad")
    assert "Organisation: WHO" in out and "ExCon's notes (desk only): Plays it tough" in out
    assert "haven't written to any team" in out
    assert desk_chat.tool_who(sc, "Salmah").startswith("persona:salma.haddad")  # spelling
    kofi = desk_chat.tool_who(sc, "Kofi")
    assert "No persona matches" in kofi and "rp:9-50" in kofi
    assert "No persona matches" in desk_chat.tool_who(sc, "Zebedee")
    assert "Jane: member of Team A" in desk_chat.tool_who(sc, "Jane")
    brief = desk_chat.tool_who(desk_chat.Scope(), "donotreply")
    assert "What they have said so far" in brief and "Team A" in brief and "Team B" in brief
    assert "Open now (a team is waiting)" in brief


def test_chats_are_private_and_one_question_at_a_time(excon, chat_on):
    world(excon)
    ai.set_client(Script([[("answer", {"answer": "First answer.", "sources": []})]]))
    d, e = desk_client(), desk_client("Ben Desk", "ben.desk")
    c = d.post("/inbox/desk/chat/", {}, headers=FETCH).json()["chat"]
    assert e.get(f"/inbox/desk/chat/{c['id']}/").status_code == 404
    assert e.post(f"/inbox/desk/chat/{c['id']}/ask/", {"question": "x"}).status_code == 404
    assert e.get("/inbox/desk/chat/").json()["chats"] == []
    t = d.post(f"/inbox/desk/chat/{c['id']}/ask/", {"question": "Who is Salma?"}, headers=FETCH).json()["turn"]
    assert t["answer"] == "First answer." and DeskChat.objects.get().title == "Who is Salma?"
    assert e.get(f"/inbox/desk/chat/turn/{t['id']}/").status_code == 404
    # the next question carries the earlier question and answer, not its tool calls
    fake = Script([[("answer", {"answer": "Second.", "sources": []})]])
    ai.set_client(fake)
    d.post(f"/inbox/desk/chat/{c['id']}/ask/", {"question": "And her boss?"}, headers=FETCH)
    msgs = fake.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"][0]["text"] == "Who is Salma?" and msgs[1]["content"][0]["text"] == "First answer."
    DeskChatTurn.objects.create(chat_id=c["id"], question="slow", status="running")
    assert d.post(f"/inbox/desk/chat/{c['id']}/ask/", {"question": "again"}, headers=FETCH).status_code == 409
    # ExCon on the desk has its own chats
    assert excon.get("/inbox/desk/chat/").json()["chats"] == []
    assert d.post(f"/inbox/desk/chat/{c['id']}/delete/").json()["ok"]
    assert not DeskChat.objects.exists()


def test_nobody_else_reaches_it_and_it_can_be_turned_off(excon, chat_on):
    w = world(excon)
    ai.set_client(Script([[("answer", {"answer": "-", "sources": []})]]))
    d = desk_client()
    c = d.post("/inbox/desk/chat/", {}, headers=FETCH).json()["chat"]
    for cl in (participant("a", "Zed"), leader("a")):
        assert cl.get("/inbox/desk/chat/").status_code == 403
        assert cl.post(f"/inbox/desk/chat/{c['id']}/ask/", {"question": "x"}).status_code == 403
    assert Client().get("/inbox/desk/chat/").status_code == 302
    page = d.get("/inbox/desk/").content.decode()
    assert "data-chat-toggle" in page and "ChatSIMEX" in page
    assert "data-chat-toggle" in d.get(f"/inbox/as/{w.ta.id}/").content.decode()  # on the teams it views too
    assert "data-chat-toggle" not in participant("b", "Ola").get("/inbox/").content.decode()
    assert "data-chat-toggle" not in leader("b").get("/inbox/").content.decode()
    conf.put("desk_chat", "0")
    r = d.post(f"/inbox/desk/chat/{c['id']}/ask/", {"question": "x"}, headers=FETCH)
    assert r.status_code == 400 and "ChatSIMEX is off" in r.json()["error"]
    assert "data-chat-toggle" not in d.get("/inbox/desk/").content.decode()


def test_start_afresh_clears_the_chats(excon, chat_on):
    world(excon)
    ai.set_client(Script([[("answer", {"answer": "Ok.", "sources": []})]]))
    d = desk_client()
    c = d.post("/inbox/desk/chat/", {}, headers=FETCH).json()["chat"]
    d.post(f"/inbox/desk/chat/{c['id']}/ask/", {"question": "x"}, headers=FETCH)
    reset.start_afresh()
    assert not DeskChat.objects.exists() and not DeskChatTurn.objects.exists()


def test_chat_stays_out_of_participant_facing_code():
    import pathlib
    here = pathlib.Path(desk_chat.__file__).parent
    for name in ("views_inbox.py", "views_leader.py", "ai.py", "npc.py", "report.py", "views_ai.py", "views_report.py",
                 "render.py", "delivery.py", "desk.py", "inbound.py", "desk_ai.py", "suggest_leader.py", "suggest.py"):
        src = (here / name).read_text()
        assert not re.search(r"\bdesk_chat\.|import desk_chat|DeskChat|views_desk_chat", src), name

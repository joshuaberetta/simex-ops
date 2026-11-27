"""SIMEX Messenger on the persona desk (ops/messenger_desk.py): a team's turns to a persona are
Responses, the desk answers as the persona (a Message of kind chat), the desk's sections and
DeskConversation chat:<room>, and AI personas answering in chat."""

from datetime import timedelta

from django.utils import timezone

from ops import conf, desk, messenger_desk, npc
from ops.models import DeskConversation, DeskNotice, Message, NpcReply, Post, Response, Room, Sender, Team
from ops.tests.test_desk import desk_client
from ops.tests.test_npc import fake, make_npc  # noqa: F401 (fixture)
from ops.tests.test_ops import participant

F = {"headers": {"x-requested-with": "fetch"}}


def setup():
    conf.put("chat_enabled", "1")
    conf.put("live_armed", "1")
    Sender.objects.create(local_part="mayor", display_name="The Mayor", organisation="City Hall")
    Sender.objects.create(local_part="wfp", display_name="WFP Logistics")


def say(c, rid, body, base="/chat", **extra):
    r = c.post(f"{base}/r/{rid}/send/", {"body": body, **extra}, **F)
    assert r.status_code == 200, r.content
    return r.json()["post"]


def test_a_team_chats_with_a_persona_and_the_desk_answers(excon):
    setup()
    pa = participant("a", "Jane")
    pb = participant("b", "Bea")
    dk = desk_client()
    book = [b["key"] for b in pa.get("/chat/book/", **F).json()["book"]]
    assert "p:mayor" in book
    rid = pa.post("/chat/new/", {"keys": ["p:mayor"]}, **F).json()["id"]
    room = Room.objects.get(pk=rid)
    assert room.has_persona and room.team.code == "a"
    say(pa, rid, "Hello Mayor")
    say(pa, rid, "Can we use the stadium?")
    r = Response.objects.get()
    assert (r.channel, r.persona, r.fetched, r.team.code) == ("chat", "mayor", False, "a")
    assert r.text == "Hello Mayor\nCan we use the stadium?" and Post.objects.filter(response=r).count() == 2
    # The desk sees it waiting; another team's member never sees the room.
    rows = dk.get("/chat/desk/rooms/", **F).json()["rooms"]
    assert [(x["id"], x["section"], x["unread"]) for x in rows] == [(rid, "attention", 2)]
    assert pb.get(f"/chat/r/{rid}/posts/", **F).status_code == 404
    assert pa.get("/chat/desk/").status_code == 403
    assert DeskNotice.objects.filter(key=f"chat-in:{r.id}").count() == 0  # nobody involved yet: audience empty
    # Answer as the persona: a Message of kind chat, then the Post; the turn closes.
    room_json = dk.get(f"/chat/desk/r/{rid}/posts/", **F).json()["room"]
    assert room_json["addressed"] == "mayor" and room_json["personas"][0]["name"] == "The Mayor"
    p = say(dk, rid, "Yes, from 14:00.", base="/chat/desk", persona="mayor", client_key="d1")
    m = Message.objects.get(kind="chat")
    assert (m.persona, m.response, m.status, m.thread, m.author) == ("mayor", r, "sent", f"chat:{rid}", dk.person)
    assert Post.objects.get(pk=p["id"]).message == m and Response.objects.get(pk=r.pk).fetched
    say(dk, rid, "Yes, from 14:00.", base="/chat/desk", persona="mayor", client_key="d1")  # a resend
    assert Message.objects.filter(kind="chat").count() == 1
    assert dk.get("/chat/desk/rooms/", **F).json()["rooms"][0]["section"] == "rest"
    got = pa.get(f"/chat/r/{rid}/posts/", **F).json()["posts"][-1]
    assert got["name"] == "The Mayor" and got["body"] == "Yes, from 14:00." and not got["mine"]
    assert dk.post(f"/chat/desk/r/{rid}/send/", {"body": "x", "persona": "wfp"}, **F).status_code == 400  # not in it
    # Take and done, through desk.mark on chat:<room>.
    say(pa, rid, "Thanks!")
    assert dk.post(f"/chat/desk/r/{rid}/mark/", {"act": "take"}, **F).status_code == 200
    c = DeskConversation.objects.get(thread=f"chat:{rid}")
    assert c.owner == dk.person.user
    dk.post(f"/chat/desk/r/{rid}/mark/", {"act": "done"}, **F)
    assert dk.get("/chat/desk/rooms/", **F).json()["rooms"][0]["section"] == "rest"


def test_groups_with_personas_chatter_mentions_and_join_point(excon):
    setup()
    pa, po = participant("a", "Jane"), participant("a", "Omar")
    dk = desk_client()
    gid = pa.post("/chat/new/", {"keys": [f"m:{po.person.id}"], "title": "Ops"}, **F).json()["id"]
    say(pa, gid, "Internal: our weak spot is fuel")
    assert dk.get("/chat/desk/rooms/", **F).json()["rooms"] == []  # no persona: the desk never sees it
    pa.post(f"/chat/r/{gid}/settings/", {"act": "add", "keys": ["p:mayor", "p:wfp"]}, **F)
    posts = dk.get(f"/chat/desk/r/{gid}/posts/", **F).json()["posts"]
    assert "Internal: our weak spot is fuel" not in [p["body"] for p in posts]  # from the personas' joining on
    say(po, gid, "Welcome both")
    r1 = Response.objects.get()
    assert r1.persona is None
    row = dk.get("/chat/desk/rooms/", **F).json()["rooms"][0]
    assert row["section"] == "chatter"
    say(pa, gid, "@[p:wfp] how much fuel can you bring?", mentions=["p:wfp"])
    r2 = Response.objects.order_by("-id").first()
    assert r2.persona == "wfp" and Response.objects.get(pk=r1.pk).fetched  # Omar's turn closed: another author
    assert dk.get("/chat/desk/rooms/", **F).json()["rooms"][0]["section"] == "attention"


def test_turns_close_when_quiet():
    setup()
    pa = participant("a", "Jane")
    rid = pa.post("/chat/new/", {"keys": ["p:mayor"]}, **F).json()["id"]
    say(pa, rid, "Hello")
    assert messenger_desk.tick() == 0
    assert messenger_desk.tick(timezone.now() + timedelta(minutes=3)) == 1
    assert Response.objects.get().fetched


def test_an_ai_persona_answers_in_the_chat(excon, fake):  # noqa: F811
    setup()
    conf.put("npc_scope", "all")
    make_npc()
    pa = participant("a", "Jane")
    rid = pa.post("/chat/new/", {"keys": ["p:gov-plaida"]}, **F).json()["id"]
    say(pa, rid, "Which roads are open?")
    job = NpcReply.objects.get()
    assert job.response.channel == "chat" and job.due_at <= timezone.now() + timedelta(minutes=2, seconds=5)
    rows = desk_client().get("/chat/desk/rooms/", **F).json()["rooms"]
    assert rows[0]["section"] == "later"  # the AI persona answers: nobody on the desk needs to
    npc.draft(job.id)
    NpcReply.objects.filter(pk=job.pk).update(due_at=timezone.now() - timedelta(seconds=1))
    npc.tick()
    job.refresh_from_db()
    assert job.status == "sent", (job.status, job.error)
    p = Post.objects.filter(room_id=rid, persona="gov-plaida").get()
    assert p.npc and p.message.npc and p.message.kind == "chat"
    assert desk.thread_url(f"chat:{rid}") == f"/chat/desk/r/{rid}/"
    assert Team.objects.get(code="a").id in Room.objects.get(pk=rid).teams


def test_network_counts_each_post_once(excon):
    from ops import network
    setup()
    pa, po = participant("a", "Jane"), participant("a", "Omar")
    dk = desk_client()
    rid = pa.post("/chat/new/", {"keys": ["p:mayor"]}, **F).json()["id"]
    say(pa, rid, "Hello Mayor")
    say(dk, rid, "Hello", base="/chat/desk", persona="mayor")
    gid = pa.post("/chat/new/", {"keys": [f"m:{po.person.id}"], "title": "Ops"}, **F).json()["id"]
    say(po, gid, "In the group")
    g = network.graph()
    chat = [e for e in g["sends"] if e["k"] in ("chat", "group")]
    assert [(e["f"], e["to"], e["k"]) for e in chat] == [
        (f"m:{pa.person.id}", ["p:mayor"], "chat"), ("p:mayor", [f"m:{pa.person.id}"], "chat"),
        (f"m:{po.person.id}", [f"m:{pa.person.id}"], "group")]
    assert not [e for e in g["sends"] if e["k"] not in ("chat", "group")]  # no Response or Message counted twice
    more, _ = network.sends("live", g["cursor"])
    assert not [e for e in more if e["id"].startswith("c")]

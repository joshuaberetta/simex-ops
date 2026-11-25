"""Persona mail (desk._persona_mail, npc.on_persona_mail): personas writing to each other on the
persona desk, teams in copy or not, and AI personas answering it, capped when they answer each
other."""

import json

from ops import conf, desk, npc
from ops.models import Mail, Message, NpcProfile, NpcReply, Sender, Team
from ops.tests.test_desk import FETCH, desk_client, setup
from ops.tests.test_npc import due_now, fake, make_npc  # noqa: F401 (fixture)
from ops.tests.test_ops import participant


def ai_persona(local, name):
    s = Sender.objects.create(local_part=local, display_name=name)
    p = NpcProfile(sender=s, enabled=True, personality="Brisk.", knowledge="Warehouse stock.")
    npc.apply_timing(p, "instant")
    p.save()
    return p


def run_all():
    """The AI loop until nothing is due."""
    for _ in range(10):
        due_now()
        if not npc.tick():
            return


def test_a_persona_writes_to_an_ai_persona_on_the_desk_only(excon, fake):
    setup(excon)
    make_npc()
    d = desk_client()
    j = d.post("/inbox/desk/send/", {"from": "p:cnn", "to": "p:gov-plaida", "subject": "Road status",
                                     "body": "Is the coast road open?"}, headers=FETCH).json()
    assert j["ok"], j
    m = Message.objects.get(kind="persona")
    assert m.team_id is None and m.to == ["p:gov-plaida"] and m.persona == "cnn" and not m.npc
    sent = Mail.objects.get(team=None, folder="sent", message=m)
    got = Mail.objects.get(team=None, folder="inbox", message=m)
    assert got.persona == "gov-plaida" and got.thread == sent.thread and got.recipients["from"] == "p:cnn"
    assert not Mail.objects.filter(team__isnull=False, subject="Road status").exists()  # no team sees it
    assert not desk.states([got.thread])[got.thread]["waiting"]
    job = NpcReply.objects.get()
    assert job.mail == got and job.team is None and job.response is None and job.status == "queued"
    # the desk's reply defaults: as gov-plaida, back to cnn
    assert d.get("/inbox/desk/").status_code == 200 and "Road status" in d.get("/inbox/desk/").content.decode()
    t = d.get(f"/inbox/desk/t/{got.thread}/").context["thread_js"]
    assert t["reply"] == ["p:cnn"] and t["from"] == "p:gov-plaida"
    run_all()
    job.refresh_from_db()
    assert job.status == "sent", (job.status, job.error)
    ans = Message.objects.filter(kind="persona", npc=True).get()
    assert ans.persona == "gov-plaida" and ans.to == ["p:cnn"] and ans.thread == got.thread and job.message == ans
    assert Mail.objects.filter(team=None, folder="inbox", message=ans, persona="cnn").exists()
    prompt = json.dumps(fake.calls[-1]["messages"])
    assert "Is the coast road open?" in prompt and "not a team" in prompt and "no team reads it" in prompt
    assert NpcReply.objects.count() == 1  # cnn isn't an AI persona: nobody answers it
    # in ExCon's queue as persona mail
    page = excon.get("/npcs/").content.decode()
    assert "persona mail" in page and f"/inbox/desk/t/{got.thread}/" in page


def test_ai_personas_answering_each_other_stop_after_the_cap(excon, fake):
    setup(excon)
    make_npc()
    ai_persona("wfp-log", "WFP Logistics")
    d = desk_client()
    assert d.post("/inbox/desk/send/", {"from": "p:wfp-log", "to": "p:gov-plaida", "subject": "Trucks",
                                        "body": "Can our trucks use the coast road?"}, headers=FETCH).json()["ok"]
    run_all()
    msgs = list(Message.objects.filter(kind="persona").order_by("id"))
    assert [x.npc for x in msgs] == [False] + [True] * npc.PP_AI_TURNS
    assert [x.persona for x in msgs] == ["wfp-log", "gov-plaida", "wfp-log", "gov-plaida"][:len(msgs)]
    assert len({x.thread for x in msgs}) == 1
    assert not NpcReply.objects.filter(status="queued").exists()
    # A person writing again starts a new run
    last = Mail.objects.filter(team=None, folder="inbox").order_by("-id").first()
    assert d.post("/inbox/desk/send/", {"from": f"p:{'wfp-log' if last.persona == 'wfp-log' else 'gov-plaida'}",
                                        "to": f"p:{last.recipients['from'][2:]}", "subject": "Re: Trucks",
                                        "body": "One more thing.", "in_reply_to": last.id}, headers=FETCH).json()["ok"]
    assert NpcReply.objects.filter(status="queued").count() == 1


def test_teams_in_copy_get_the_chatter_and_the_answer_in_one_conversation(excon, fake):
    setup(excon)
    make_npc()
    conf.put("npc_scope", "all")
    ta = Team.objects.get(code="a")
    d = desk_client()
    j = d.post("/inbox/desk/send/", {"from": "p:cnn", "to": "p:gov-plaida", "cc": f"t:{ta.id}", "subject": "Statement",
                                     "body": "Will the governor comment on the floods?"}, headers=FETCH).json()
    assert j["ok"], j
    team_copy = Mail.objects.get(team=ta, owner=None, folder="inbox", subject="Statement")
    pp = Mail.objects.get(team=None, folder="inbox", message__kind="persona")
    assert team_copy.thread == pp.thread and "Grace Mwangi" in team_copy.to_label
    run_all()
    answer = Mail.objects.filter(team=ta, owner=None, folder="inbox", thread=team_copy.thread).exclude(pk=team_copy.pk).get()
    assert answer.message.npc and answer.message.persona == "gov-plaida" and answer.subject == "Re: Statement"
    assert Message.objects.filter(kind="persona", npc=True, persona="gov-plaida").exists()
    assert "Team A is in copy" in json.dumps(fake.calls[-1]["messages"])
    # the team's reply all reaches the desk in the same conversation
    pa = participant("a")
    t = pa.get(f"/inbox/t/{answer.thread}/").context["thread_js"]
    assert "p:cnn" in t["replyAll"] and "p:gov-plaida" in t["replyAll"]


def test_teams_out_of_the_ai_scope_get_no_ai_answer(excon, fake):
    setup(excon)
    make_npc()
    conf.put("npc_scope", "test")
    ta = Team.objects.get(code="a")
    d = desk_client()
    assert d.post("/inbox/desk/send/", {"from": "p:cnn", "to": f"p:gov-plaida,t:{ta.id}", "subject": "S",
                                        "body": "?"}, headers=FETCH).json()["ok"]
    assert not NpcReply.objects.exists()


def test_held_answer_sent_by_excon_and_a_person_answering_skips_the_ai(excon, fake):
    setup(excon)
    p = make_npc()
    p.auto_send = False
    p.save()
    d = desk_client()
    d.post("/inbox/desk/send/", {"from": "p:cnn", "to": "p:gov-plaida", "subject": "Q", "body": "?"}, headers=FETCH)
    run_all()
    job = NpcReply.objects.get()
    assert job.status == "held"
    excon.post(f"/npcs/answers/{job.id}/", {"action": "send", "body": "Edited by ExCon."})
    job.refresh_from_db()
    assert job.status == "sent" and Message.objects.filter(kind="persona", body="Edited by ExCon.").exists()
    # a desk member answers as the AI persona before it does: its queued answer goes
    got = Mail.objects.filter(team=None, folder="inbox", persona="cnn").get()
    d.post("/inbox/desk/send/", {"from": "p:cnn", "to": "p:gov-plaida", "subject": "Re: Q", "body": "And?",
                                 "in_reply_to": got.id}, headers=FETCH)
    q = NpcReply.objects.get(status="queued")
    mine = Mail.objects.filter(team=None, folder="inbox", persona="gov-plaida").order_by("-id").first()
    d.post("/inbox/desk/send/", {"from": "p:gov-plaida", "to": "p:cnn", "subject": "Re: Q", "body": "By hand.",
                                 "in_reply_to": mine.id}, headers=FETCH)
    q.refresh_from_db()
    assert q.status == "skipped" and "persona desk" in q.note


def test_network_and_clearing(excon, fake):
    from ops import network, reset
    setup(excon)
    make_npc()
    d = desk_client()
    d.post("/inbox/desk/send/", {"from": "p:cnn", "to": "p:gov-plaida", "subject": "N", "body": "?"}, headers=FETCH)
    edges = network._messages(network.Ctx("live"))
    assert any(e["f"] == "p:cnn" and e["to"] == ["p:gov-plaida"] and e["k"] == "persona" for e in edges)
    excon.post("/inboxes/clear/", {"confirm": "CLEAR", "team": "all", "scope": "all", "deliveries": "1"})
    assert not Mail.objects.filter(team=None, message__kind="persona").exists()
    assert not Message.objects.filter(kind="persona").exists() and not NpcReply.objects.exists()
    assert reset.clear_persona_mail() == 0


def test_persona_mail_goes_to_the_default_owner(excon, fake):
    """Persona mail has no team: the "all teams" default of the persona written to gives it, else
    the writer's; the AI persona's answer stays in the same owner's Assigned to me."""
    from ops.models import DeskConversation, DeskDefault
    setup(excon)
    make_npc()
    d, e = desk_client(), desk_client("Ben Desk", "ben.desk")
    anna, ben = d.person.user, e.person.user
    DeskDefault.objects.create(sender=Sender.objects.get(local_part="gov-plaida"), team=None, user=anna)
    DeskDefault.objects.create(sender=Sender.objects.get(local_part="cnn"), team=None, user=ben)
    assert e.post("/inbox/desk/send/", {"from": "p:cnn", "to": "p:gov-plaida", "subject": "Road status",
                                        "body": "Is the coast road open?"}, headers=FETCH).json()["ok"]
    t = Mail.objects.get(team=None, folder="inbox", subject="Road status").thread
    c = DeskConversation.objects.get(thread=t)
    assert c.owner == anna and c.auto
    run_all()
    assert Message.objects.filter(kind="persona", npc=True, thread=t).exists()
    assert DeskConversation.objects.get(thread=t).owner == anna
    assert t in [r["thread"] for r in d.get("/inbox/desk/?folder=mine").context["rows"]]
    # Nobody's default for the persona written to: the writer's.
    Sender.objects.create(local_part="unhcr", display_name="UNHCR")
    assert e.post("/inbox/desk/send/", {"from": "p:cnn", "to": "p:unhcr", "subject": "Camps",
                                        "body": "How many?"}, headers=FETCH).json()["ok"]
    t2 = Mail.objects.get(team=None, folder="inbox", subject="Camps").thread
    assert DeskConversation.objects.get(thread=t2).owner == ben

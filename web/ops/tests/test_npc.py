"""AI personas (ops/npc.py) with a fake model client: intake, timing, answers in SIMEX Mail and
by email, ExCon's queue and sandbox, Generate with AI, the team leader's tab."""

import json
import random
from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from ops import ai, conf, inbound, npc
from ops.models import Mail, Message, Notice, NpcProfile, NpcReply, NpcSandbox, Response, Sender, Team
from ops.tests.test_ai import FakeClient
from ops.tests.test_leaders import leader
from ops.tests.test_ops import participant

ANSWER = {"body": "Dear team,\nThe coast road is cut at Km 12.\nGrace", "shared": ["coast road cut"], "flags": [],
          "note": "Shared the road status."}
PROFILE = {"username": "grace-mwangi", "display_name": "Grace Mwangi, Deputy Governor", "inapp_local": "g.mwangi",
           "domain": "plaida.go.ep", "organisation": "Plaida Province", "role": "Deputy Governor",
           "personality": "Proud and defensive.", "knowledge": "- Coast road cut at Km 12", "boundaries": "- No figures",
           "timing": "bureaucrat", "rationale": "Pressure from the province.", "new_facts": ["Km 12"]}


def answers(kw):
    if kw["system"] == ai.system("npc_new"):
        return PROFILE, "end_turn"
    if kw["system"] == ai.system("npc"):
        return ANSWER, "end_turn"
    return {}, "end_turn"


@pytest.fixture
def fake():
    c = FakeClient(answers)
    ai.set_client(c)
    conf.put("ai_enabled", "1")
    conf.put("npc_enabled", "1")
    npc.RNG = random.Random(1)
    return c


def make_npc(timing="instant", **kw):
    s = Sender.objects.create(local_part="gov-plaida", display_name="Grace Mwangi", inapp_local="g.mwangi",
                              domain="plaida.go.ep")
    p = NpcProfile(sender=s, enabled=True, role="Deputy Governor", knowledge="Coast road cut at Km 12.",
                   personality="Proud.", **kw)
    npc.apply_timing(p, timing)
    p.save()
    return p


def write_to(client, subject="Roads", body="Which roads are open?", parent=None):
    data = {"to": "p:gov-plaida", "subject": subject, "body": body}
    if parent:
        data["in_reply_to"] = parent.id
    r = client.post("/inbox/send/", data)
    assert r.status_code == 302, r.content[:300]
    return Response.objects.order_by("-id").first()


def due_now():
    NpcReply.objects.filter(status="queued").update(due_at=timezone.now() - timedelta(seconds=1))


def test_timing_draws():
    p = NpcProfile(min_minutes=20, max_minutes=90, quick_chance=0, no_reply_chance=0)
    rng = random.Random(3)
    assert all(20 <= npc.draw_minutes(p, rng) <= 90 for _ in range(200))
    p.no_reply_chance = 1
    assert npc.draw_minutes(p, rng) is None
    p.no_reply_chance, p.quick_chance = 0, 1
    assert all(npc.QUICK[0] <= npc.draw_minutes(p, rng) <= npc.QUICK[1] for _ in range(50))
    npc.apply_timing(p, "busy")
    assert (p.min_minutes, p.max_minutes, p.quick_chance, p.no_reply_chance) == (20, 90, 0.1, 0.1)
    # Office hours 8-17: a message at 20:00 is answered soon after 08:00 the next day.
    p.office_from, p.office_to = 8, 17
    evening = timezone.localtime().replace(hour=20, minute=0, second=0, microsecond=0)
    due = timezone.localtime(npc.office_hours(p, evening, rng))
    assert due.date() == (evening + timedelta(days=1)).date() and due.hour == 8
    noon = evening.replace(hour=12)
    assert npc.office_hours(p, noon, rng) == noon


def test_answers_in_simex_mail_after_its_delay(excon, fake):
    p = make_npc("instant")
    pa = participant("test", "Jo")
    r = write_to(pa)
    job = NpcReply.objects.get()
    assert job.response == r and job.status == "queued"
    assert timedelta(minutes=0.9) <= job.due_at - timezone.now() <= timedelta(minutes=3.1)
    assert npc.tick() == 1 and len(fake.calls) == 1  # drafted at once, not sent: not due yet
    job.refresh_from_db()
    assert job.status == "queued" and "Km 12" in job.body and job.draft_for == r.id
    assert not Message.objects.filter(npc=True).exists()
    assert npc.tick() == 0  # nothing more to write or send
    due_now()
    assert npc.tick() == 1 and len(fake.calls) == 1  # sent the draft: no second call
    job.refresh_from_db()
    m = job.message
    assert job.status == "sent" and m.npc and m.kind == "reply" and m.idem_key == f"npc-{job.id}" and m.status == "sent"
    mail = Mail.objects.get(team=r.team, owner=None, folder="inbox", message=m)
    assert mail.thread == r.mail.thread and mail.from_addr == "g.mwangi@plaida.go.ep" and "Km 12" in mail.body
    sent = json.dumps(fake.calls[0]["messages"])
    assert "Coast road cut at Km 12." in sent and "Which roads are open?" in sent
    assert fake.calls[0]["model"] == "claude-sonnet-5-5"
    assert Notice.objects.filter(key=f"npc:{job.id}:sent").exists()
    # The participant sees it; ExCon's reply page labels it.
    assert "coast road is cut" in pa.get(f"/inbox/t/{mail.thread}/").content.decode()
    assert "AI persona</a> answer" in excon.get(f"/replies/{r.id}/").content.decode()
    # Sent once: another pass makes no call and no second message.
    assert npc.tick() == 0 and Message.objects.filter(npc=True).count() == 1
    # Its next message in that conversation remembers the answer.
    write_to(pa, "Re: Roads", "And the bridge?", parent=mail)
    due_now()
    npc.tick()
    assert "The coast road is cut at Km 12" in json.dumps(fake.calls[-1]["messages"])


def test_messages_before_it_answers_get_one_answer(excon, fake):
    make_npc("bureaucrat")
    pa = participant("test", "Jo")
    first = write_to(pa)
    sent = Mail.objects.get(pk=first.mail_id)
    second = write_to(pa, "Re: Roads", "Hello? Urgent.", parent=sent)
    job = NpcReply.objects.get()
    assert job.response == second
    due_now()
    npc.tick()
    assert "Hello? Urgent." in json.dumps(fake.calls[0]["messages"]) and Message.objects.filter(npc=True).count() == 1


def test_scope_switch_and_no_reply_draw(excon, fake):
    p = make_npc("instant")
    pa = participant("a", "Ann")
    write_to(pa)
    assert not NpcReply.objects.exists()  # Test team only by default
    conf.put("npc_scope", "all")
    write_to(pa, "Bridges")
    assert NpcReply.objects.get().team.code == "a"
    conf.put("npc_enabled", "0")
    write_to(pa, "Ports")
    assert NpcReply.objects.count() == 1
    conf.put("npc_enabled", "1")
    p.no_reply_chance = 1
    p.save()
    write_to(pa, "Fuel")
    assert NpcReply.objects.order_by("-id").first().status == "skipped"


def test_held_answer_waits_for_excon_and_pause_stops_it(excon, fake):
    make_npc("instant", auto_send=False)
    pa = participant("test", "Jo")
    write_to(pa)
    due_now()
    conf.put("paused", "1")
    assert npc.tick() == 0
    conf.put("paused", "0")
    npc.tick()
    job = NpcReply.objects.get()
    assert job.status == "held" and not Message.objects.filter(npc=True).exists()
    page = excon.get(f"/npcs/{job.npc_id}/").content.decode()
    assert "coast road is cut" in page
    excon.post(f"/npcs/answers/{job.id}/", {"action": "send", "body": "Edited: the coast road is closed."})
    job.refresh_from_db()
    assert job.status == "sent" and job.message.body.startswith("Edited:")


def test_failed_answer_retries_then_fails(excon, fake):
    make_npc("instant")
    fake.answer = lambda kw: ({}, "refusal")
    write_to(participant("test", "Jo"))
    for _ in range(npc.MAX_ATTEMPTS):
        due_now()
        assert npc.tick() == 1
        assert npc.tick() == 0  # tried again only after RETRY_AFTER
        NpcReply.objects.update(updated_at=timezone.now() - npc.RETRY_AFTER)
    job = NpcReply.objects.get()
    assert job.status == "failed" and job.attempts == npc.MAX_ATTEMPTS
    assert Notice.objects.filter(key=f"npc:{job.id}:failed").exists()
    fake.answer = answers
    excon.post(f"/npcs/answers/{job.id}/", {"action": "retry"})
    npc.tick()
    assert NpcReply.objects.get().status == "sent"


def test_email_answer_waits_for_content_and_threads(excon, fake):
    make_npc("instant")
    pa = participant("test", "Jo")
    r = inbound._new_email("re_1", {"from": f"Jo <{pa.person.email}>", "to": [f"gov-plaida@{settings.MAIL_DOMAIN}"],
                                    "subject": "Roads", "created_at": timezone.now().isoformat()})
    assert r.channel == "email" and NpcReply.objects.get().response == r
    due_now()
    assert npc.tick() == 0  # its text isn't fetched yet
    Response.objects.filter(pk=r.pk).update(fetched=True, text="Which roads are open?", message_id_header="<abc@x>")
    assert npc.tick() == 2  # written, and sent as it is due
    m = Message.objects.get(npc=True)
    assert m.channel == "email" and m.to == [pa.person.email] and m.extra_headers["In-Reply-To"] == "<abc@x>"


def test_leader_sees_the_team_conversations(excon, fake):
    make_npc("instant")
    conf.put("npc_scope", "all")
    pa, lead = participant("a", "Ann"), leader("a")
    assert "/inbox/personas/" in lead.get("/inbox/").content.decode()
    write_to(pa, body="Which roads are open?")
    page = lead.get("/inbox/personas/").content.decode()
    assert "Which roads are open?" in page and "answer due about" in page
    due_now()
    npc.tick()
    page = lead.get("/inbox/personas/").content.decode()
    assert "coast road is cut" in page and "answered" in page and "/inbox/team/t/" in page
    # Never another team's, and never for a member.
    write_to(participant("b", "Bo"), body="Team B secret question")
    assert "Team B secret question" not in lead.get("/inbox/personas/").content.decode()
    assert pa.get("/inbox/personas/").status_code in (302, 403, 404)


def test_generate_and_sandbox(excon, fake):
    r = excon.post("/npcs/", {"brief": "A proud deputy governor"})
    p = NpcProfile.objects.get()
    assert r.url == f"/npcs/{p.id}/"
    s = p.sender
    assert p.gen_status == "done" and s.local_part == "grace-mwangi" and s.inapp_address == "g.mwangi@plaida.go.ep"
    assert not p.enabled and not s.active and p.timing == "bureaucrat" and p.min_minutes == 60
    assert "Km 12" in excon.get(f"/npcs/{p.id}/").content.decode()
    excon.post(f"/npcs/{p.id}/", {"display_name": s.display_name, "local_part": s.local_part, "inapp_local": "g.mwangi",
                                  "domain": "plaida.go.ep", "personality": "Proud.", "knowledge": "Km 12",
                                  "timing": "custom", "min_minutes": "2", "max_minutes": "4", "quick_pct": "0",
                                  "none_pct": "50", "enabled": "1", "auto_send": "1", "active": "1"})
    p.refresh_from_db()
    assert p.enabled and p.sender.active and p.timing == "custom" and p.no_reply_chance == 0.5 and p.gen_status == ""
    excon.post(f"/npcs/{p.id}/try/", {"text": "Which roads are open?"})
    box = NpcSandbox.objects.get()
    assert [t["who"] for t in box.turns] == ["team", "npc"] and "Km 12" in box.turns[1]["text"]
    assert not Message.objects.exists() and not Mail.objects.exists()
    assert "Km 12" in excon.get(f"/npcs/{p.id}/").content.decode()
    assert "AI personas" in excon.get("/settings/").content.decode()


def test_tab_and_setting(excon):
    conf.put("npc_enabled", "0")
    assert 'href="/npcs/"' in excon.get("/injects/").content.decode()  # always: profiles shape the desk's suggestions
    excon.post("/settings/npc/", {"npc_enabled": "1", "npc_scope": "all"})
    assert conf.flag("npc_enabled") and conf.get("npc_scope") == "all"
    assert 'href="/npcs/"' in excon.get("/injects/").content.decode()
    assert excon.get("/npcs/").status_code == 200
    s = Sender.objects.create(local_part="ifrc-head", display_name="IFRC")
    excon.post("/npcs/", {"sender": s.id})
    assert NpcProfile.objects.get(sender=s).timing == "responsive"
    tl = Sender.objects.create(local_part="lead-x", team_leader=True)
    excon.post("/npcs/", {"sender": tl.id})
    assert not NpcProfile.objects.filter(sender=tl).exists()
    assert Team.objects.filter(is_test=True).exists()


def test_seed_profiles_from_the_injects(excon, fake):
    from ops.models import Inject
    s = Sender.objects.create(local_part="moh-spokes", display_name="Ministry of Health")
    Inject.objects.create(mel_id="2-01", sender_local="moh-spokes", status="sent", sent_at=timezone.now(),
                          subject="Hospitals", body="Dear colleagues, three hospitals are full. Regards, MoH")
    Inject.objects.create(mel_id="2-09", sender_local="moh-spokes", status="scheduled", subject="Cholera",
                          body="PLANNED: cholera confirmed in Plaida")
    Inject.objects.create(mel_id="2-10", sender_local="undac-{team}-tl", status="scheduled", subject="x", body="y")
    assert [x.local_part for x in npc.seedable()] == ["moh-spokes"]
    excon.post("/npcs/", {"seed": "1"})
    p = NpcProfile.objects.get(sender=s)
    s.refresh_from_db()
    # Its username, name and address stay; the rest is Claude's, off until ExCon saves it.
    assert p.gen_status == "done" and not p.enabled and s.local_part == "moh-spokes" and s.display_name == "Ministry of Health"
    assert s.active and p.personality == "Proud and defensive." and p.timing == "bureaucrat"
    text = json.dumps(fake.calls[-1]["messages"])
    assert "three hospitals are full" in text and "[SENT]" in text and "PLANNED, NOT SEEN" in text
    assert not npc.seedable()


def test_existing_persona_without_injects_keeps_its_username_and_address(excon, fake):
    """From an existing persona, written with AI, for a persona that sends no injects: Claude's
    username, name and address are never put on it (it used to be renamed into a made-up persona,
    gone from the Senders list under its own username)."""
    s = Sender.objects.create(local_part="kobo", display_name="", domain="epeigonia.org", organisation="KoBo")
    before = s.inapp_address
    r = excon.post("/npcs/", {"sender": s.id, "ai": "1", "hint": "helpful"})
    p = NpcProfile.objects.get()
    s.refresh_from_db()
    assert r.url == f"/npcs/{p.id}/" and p.sender_id == s.id
    assert s.local_part == "kobo" and s.inapp_address == before and s.domain == "epeigonia.org"
    assert Sender.objects.filter(local_part="kobo").count() == 1 and not Sender.objects.filter(local_part="grace-mwangi").exists()
    assert p.gen_status == "done" and p.personality == "Proud and defensive."
    text = json.dumps(fake.calls[-1]["messages"])
    assert "existing persona kobo" in text and "sends no injects yet" in text and "helpful" in text
    # fill_from never renames an existing persona, even when told to
    npc.fill_from(p, PROFILE, keep=False)
    s.refresh_from_db()
    assert s.local_part == "kobo" and s.inapp_address == before


def test_npc_answer_not_delivered_is_failed_and_retry_sends(excon, fake):
    """An answer the mail provider refused is shown failed (not "sent"), and trying again delivers it."""
    from django.conf import settings
    from ops import inbound, mailer
    from ops.models import Message, NpcReply, Response
    from ops.tests.test_ops import participant

    class Failing(mailer.LogMailer):
        def send(self, payload, key):
            raise mailer.SendError("Resend 422: bad", retryable=False)

    make_npc("instant")
    pa = participant("test", "Jo")
    r = inbound._new_email("re_1", {"from": f"Jo <{pa.person.email}>", "to": [f"gov-plaida@{settings.MAIL_DOMAIN}"],
                                    "subject": "Roads", "created_at": timezone.now().isoformat()})
    Response.objects.filter(pk=r.pk).update(fetched=True, text="Which roads?")
    due_now()
    mailer.set_mailer(Failing())
    npc.tick()
    job = NpcReply.objects.get()
    assert job.status == "failed" and "422" in job.error
    mailer.set_mailer(mailer.LogMailer())
    npc.send(job)
    assert job.status == "sent"
    assert list(Message.objects.filter(npc=True).values_list("status", flat=True).order_by("id")) == ["failed", "sent"]

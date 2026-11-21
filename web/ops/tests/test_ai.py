"""Claude analysis (ops/ai.py) with a fake client: no network, no real model."""

import json
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from django.utils import timezone

from ops import ai, conf, render
from ops.models import AiCall, AiGrade, AiReport, Grade, Inject, ReplyAnalysis, Response, RubricCriterion, Team
from ops.tests.test_ops import SAMPLE, _rubric, arm, deliver_now, do_import, participant

REPLY_OUT = {"summary": "Team A commits to a plan by 15:00.", "sentiment": 5, "tone": "calm", "clarity": 4,
             "completeness": 9, "actionability": 3, "asks": ["2 trucks"], "commitments": ["plan by 15:00"],
             "facts": ["15:00"], "flags": ["escalate", "bogus"], "ai_written": 85, "ai_signals": "headings",
             "language": "English"}


class FakeMessages:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kw):
        self.owner.calls.append(kw)
        out, stop = self.owner.answer(kw)
        return NS(model=kw["model"], stop_reason=stop, stop_details=NS(category="cyber") if stop == "refusal" else None,
                  usage=NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0),
                  content=[NS(type="thinking", thinking=""), NS(type="text", text=json.dumps(out))])


class FakeClient:
    def __init__(self, answer):
        self.calls, self.answer = [], answer
        self.messages = FakeMessages(self)
        self.beta = NS(messages=FakeMessages(self))


def answers(kw):
    system = kw["system"]
    if system == ai.REPLY_SYSTEM:
        return REPLY_OUT, "end_turn"
    if system == ai.GRADE_SYSTEM:
        crit = list(RubricCriterion.objects.order_by("id"))
        return {"criteria": [{"criterion_id": crit[0].id, "points": 7, "rationale": "Clear plan.", "evidence": "Plan by 15:00"},
                             {"criterion_id": crit[1].id, "points": 1, "rationale": "On time.", "evidence": ""},
                             {"criterion_id": 99999, "points": 3, "rationale": "x", "evidence": ""}],
                "overall": "Solid.", "strengths": ["plan"], "gaps": ["no figures"]}, "end_turn"
    return {"headline": "Teams are coping.", "themes": [{"title": "Logistics", "detail": "Trucks."}],
            "teams": [{"team": "Team A", "strengths": "fast", "concerns": "thin"}], "watch": ["Team B silent"],
            "hotwash": ["Why trucks?"]}, "end_turn"


@pytest.fixture
def fake():
    c = FakeClient(answers)
    ai.set_client(c)
    conf.put("ai_enabled", "1")
    return c


def a_reply(excon):
    do_import(excon, SAMPLE)
    arm()
    deliver_now("1-15")
    pa = participant("a", "Jane")
    from ops.models import Mail
    mail = Mail.objects.get(team__code="a")
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re: Update", "body": "Plan by 15:00", "in_reply_to": mail.id})
    return Response.objects.get()


def test_off_by_default_makes_no_calls(excon):
    a_reply(excon)
    c = FakeClient(answers)
    ai.set_client(c)  # a client, but Settings → AI analysis is off
    assert ai.tick() == 0 and not c.calls


def test_reply_analysis_is_stored_and_clamped(excon, fake):
    r = a_reply(excon)
    assert ai.tick() >= 1
    a = ReplyAnalysis.objects.get(response=r)
    assert a.status == "done" and a.sentiment == 2 and a.completeness == 5 and a.flags == ["escalate"]
    assert a.ai_written == 85 and a.words == 3
    call = fake.calls[0]
    assert call["model"] == "claude-haiku-5-5" and call["output_config"]["effort"] == "medium"
    assert "Plan by 15:00" in json.dumps(call["messages"]) and "Hello Team A" in json.dumps(call["messages"])
    assert AiCall.objects.filter(purpose="reply").get().cost_usd == pytest.approx((1000 * 0.10 + 200 * 0.50) / 1e6)
    page = excon.get(f"/replies/{r.id}/").content.decode()
    assert "Team A commits to a plan" in page and "Needs ExCon attention" in page
    # the participant's own pages never show it
    assert "commits to a plan" not in participant("a", "Joe").get("/inbox/").content.decode()


def test_grade_suggestion_waits_for_quiet_thread_and_never_touches_grades(excon, fake):
    r = a_reply(excon)
    _rubric(r.inject)
    ai.tick()
    assert not AiGrade.objects.exists()  # thread still settling
    Response.objects.update(received_at=timezone.now() - timedelta(minutes=10))
    ai.tick()
    g = AiGrade.objects.get(inject=r.inject, team=r.team)
    c1, c2 = RubricCriterion.objects.order_by("id")
    assert g.scores == {str(c1.id): 3, str(c2.id): 1}  # clamped to max, unknown criterion dropped
    assert not Grade.objects.exists()
    assert fake.calls[-1]["model"] == "claude-sonnet-5-5"
    n = len(fake.calls)
    ai.tick()
    assert len(fake.calls) == n  # unchanged thread: not graded again
    page = excon.get(f"/grades/{r.inject_id}/{r.team_id}/").content.decode()
    assert "AI 3" in page and "Fill in the AI suggestions" in page and "Clear plan." in page
    assert "AI 4 / 5" in excon.get("/replies/").content.decode()


def test_spending_cap_stops_calls(excon, fake):
    a_reply(excon)
    conf.put("ai_daily_usd", "0")
    assert ai.tick() == 0 and not fake.calls


def test_refusal_is_recorded_as_failed(excon):
    r = a_reply(excon)
    ai.set_client(FakeClient(lambda kw: ({}, "refusal")))
    conf.put("ai_enabled", "1")
    ai.tick()
    a = ReplyAnalysis.objects.get(response=r)
    assert a.status == "failed" and "declined" in a.error and a.attempts == 1
    assert ai.tick() == 0  # retried only after a pause
    assert not AiCall.objects.filter(ok=True).exists()


def test_manual_mode_only_runs_queued(excon, fake):
    r = a_reply(excon)
    conf.put("ai_auto", "0")
    assert ai.tick() == 0
    excon.post(f"/replies/{r.id}/analyse/")
    assert ai.tick() == 1 and ReplyAnalysis.objects.get(response=r).status == "done"


def test_dashboard_and_summary(excon, fake):
    assert excon.get("/dashboard/").status_code == 200  # empty
    r = a_reply(excon)
    _rubric(r.inject)
    Response.objects.update(received_at=timezone.now() - timedelta(minutes=10))
    ai.tick()
    page = excon.get("/dashboard/").content.decode()
    assert "Team A" in page and "Possibly AI-written" in page and "85%" in page and "plan by 15:00" in page
    assert "80%" in excon.get("/dashboard/?score=ai").content.decode()  # 4 of 5 suggested
    excon.post("/dashboard/summary/", {"kind": "live"})
    rep = AiReport.objects.get()
    assert rep.status == "done" and rep.body["headline"] == "Teams are coping."
    assert "Team B silent" in excon.get("/dashboard/").content.decode()


def test_bedrock_model_ids(settings):
    settings.AI_PROVIDER = "bedrock"
    assert ai.model_id("fast") == "us.anthropic.claude-haiku-5-5"
    assert ai.base_model("arn:aws:bedrock:us-east-1:1:inference-profile/us.anthropic.claude-sonnet-5-5") == "claude-sonnet-5-5"


def test_settings_ai_form(excon):
    excon.post("/settings/ai/", {"ai_enabled": "1", "ai_daily_usd": "2.5"})
    assert conf.get("ai_enabled") == "1" and conf.get("ai_auto") == "0" and conf.get("ai_daily_usd") == "2.5"
    assert "No provider configured" in excon.get("/settings/").content.decode()


def test_inject_code_in_subject(excon):
    do_import(excon, SAMPLE)
    inj = Inject.objects.get(mel_id="1-15")
    assert render.build(inj, conf.get_all())[0][0].subject == "Update for Team A"
    excon.post("/settings/", {**conf.get_all(), "subject_inject_code": "1"})
    assert conf.get("subject_inject_code") == "1"
    d = render.build(inj, conf.get_all())[0][0]
    assert d.subject == "[1-15] Update for Team A"
    conf.put("subject_prefix", "[SIMEX] ")
    assert render.build(inj, conf.get_all())[0][0].subject == "[SIMEX] [1-15] Update for Team A"


def test_inject_review_flags_the_list_and_reruns_only_on_change(excon, fake):
    from ops.models import InjectReview
    conf.put("ai_auto", "0")
    t = timezone.now() + timedelta(days=1)
    inj = Inject.objects.create(mel_id="6-32", sender_local="donotreply", subject="Kits", body="Hygiene kits (see attached)",
                                send_at=t, status="scheduled")
    Inject.objects.create(mel_id="9-99", subject="Done", body="x", send_at=t, status="sent")
    issue = {"severity": "fix", "kind": "attachment", "detail": "Mentions 'see attached' but no file is attached."}
    fake.answer = lambda kw: ({"issues": [issue, {"severity": "bad", "kind": "bogus", "detail": "Odd."}]}, "end_turn")
    assert ai.tick() == 0  # manual mode: nothing until asked
    conf.put("ai_auto", "1")
    assert ai.tick() == 1  # the sent inject is never reviewed
    call = fake.calls[-1]
    assert call["model"] == "claude-haiku-5-5" and call["system"] == ai.INJECT_SYSTEM
    sent = json.dumps(call["messages"])
    assert "Hygiene kits (see attached)" in sent and "Files attached: none" in sent and "9-99" in sent  # schedule context
    rv = InjectReview.objects.get(inject=inj)
    assert rv.status == "done" and rv.issues[1] == {"severity": "check", "kind": "other", "detail": "Odd."}
    page = excon.get("/injects/").content.decode()
    assert "AI: attachment" in page and "no file is attached" in page and "AI checked 1/1" in page
    assert ai.tick() == 0  # unchanged: not read again
    # dismissed: off the list until the inject changes
    excon.post(f"/injects/{inj.id}/ai-check/", {"action": "dismiss"})
    assert "AI: attachment" not in excon.get("/injects/").content.decode()
    assert "dismissed" in excon.get(f"/injects/{inj.id}/").content.decode()
    inj.body = "Hygiene kits, list attached"
    inj.save()
    assert "AI checked 0/1" in excon.get("/injects/").content.decode()  # stale review hidden
    assert ai.tick() == 1
    assert "AI: attachment" in excon.get("/injects/").content.decode()
    excon.post("/injects/ai-check/")
    assert InjectReview.objects.get(inject=inj).status == "queued"


def test_inject_review_queue_unchecked_or_selected(excon, fake):
    from ops.models import InjectReview
    conf.put("ai_auto", "0")
    t = timezone.now() + timedelta(days=1)
    done, new, other = (Inject.objects.create(mel_id=m, subject=m, body="x", send_at=t, status="scheduled")
                        for m in ("1-01", "1-02", "1-03"))
    sent = Inject.objects.create(mel_id="9-99", subject="Done", body="x", send_at=t, status="sent")
    fake.answer = lambda kw: ({"issues": []}, "end_turn")
    ai.review_inject(done)
    page = excon.get("/injects/").content.decode()
    assert "Check unchecked with AI" in page and 'formaction="/injects/ai-check/"' in page
    excon.post("/injects/ai-check/", {"scope": "unchecked"})
    assert {rv.inject.mel_id: rv.status for rv in InjectReview.objects.all()} == {
        "1-01": "done", "1-02": "queued", "1-03": "queued"}
    InjectReview.objects.exclude(inject=done).delete()
    excon.post("/injects/ai-check/", {"scope": "selected", "ids": [new.id, sent.id]})  # the sent one is skipped
    assert {rv.inject.mel_id: rv.status for rv in InjectReview.objects.all()} == {"1-01": "done", "1-02": "queued"}
    resp = excon.post("/injects/ai-check/", {"scope": "selected", "back": "/injects/?day=none"})
    assert resp["Location"] == "/injects/?day=none" and InjectReview.objects.count() == 2


DRAFT_OUT = {"sender": "Donotreply@epeigonia.org", "from_name": "", "subject": "Road to Balanjar cut",
             "body": "Dear {team_name},\nThe coastal road is cut at km 12.", "scenario_time": "2035-05-20T10:00",
             "send_at": "2026-11-23 11:30", "recipients": "team", "expects_reply": True, "reply_due_minutes": 45,
             "documents": ["Road map.pdf"], "rationale": "Tests access planning.", "relies_on": ["1-15"],
             "new_facts": ["The road is cut at km 12."], "questions": []}


def test_draft_new_inject_from_a_reply_never_sends(excon, fake):
    from ops.models import InjectDraft, InjectReview, Message
    r = a_reply(excon)
    fake.answer = lambda kw: (DRAFT_OUT, "end_turn")
    page = excon.get(f"/replies/{r.id}/").content.decode()
    assert "Draft an answer" in page and "Draft a new inject reacting to this" in page
    sent = Message.objects.count()
    resp = excon.post("/injects/drafts/", {"mode": "new", "inject": r.inject_id, "team": r.team_id, "response": r.id,
                                           "prompt": "The road is cut"})
    d = InjectDraft.objects.get()
    assert resp["Location"] == f"/injects/drafts/{d.id}/" and d.status == "done" and d.response is None
    call = fake.calls[-1]
    assert call["model"] == "claude-sonnet-5-5" and call["system"] == ai.DRAFT_SYSTEM
    text = json.dumps(call["messages"])
    assert "Hello {team_name}" in text and "Plan by 15:00" in text and "The road is cut" in text and "undac-{team}-tl" in text
    assert "SECRET" not in text  # control-side fields never reached the app
    assert call["messages"][0]["content"][0]["cache_control"]  # injects and personas: cached
    page = excon.get(f"/injects/drafts/{d.id}/").content.decode()
    assert "Road to Balanjar cut" in page and "The road is cut at km 12." in page and "Make it a draft inject" in page
    excon.post(f"/injects/drafts/{d.id}/use/")
    inj = Inject.objects.get(mel_id="AI-1")
    assert inj.status == "draft" and inj.sender_local == "donotreply" and inj.team_ids == [r.team_id]
    assert inj.expects_reply and inj.reply_due_minutes == 45 and inj.supporting_documents == "Road map.pdf"
    assert timezone.localtime(inj.send_at).strftime("%Y-%m-%d %H:%M") == "2026-11-23 11:30"
    assert InjectReview.objects.get(inject=inj).status == "queued"
    excon.post(f"/injects/drafts/{d.id}/use/")
    assert Inject.objects.filter(mel_id__startswith="AI-").count() == 1  # not made twice
    assert "Drafted by Claude" in excon.get(f"/injects/{inj.id}/").content.decode()
    assert Message.objects.count() == sent
    # a revision carries the previous draft
    excon.post("/injects/drafts/", {"previous": d.id, "mode": "new", "inject": r.inject_id, "prompt": "Shorter"})
    assert "Your previous draft" in json.dumps(fake.calls[-1]["messages"]) and InjectDraft.objects.count() == 2


def test_draft_answer_and_followup_fill_the_forms(excon, fake):
    from ops.models import InjectDraft, Message
    r = a_reply(excon)
    fake.answer = lambda kw: ({**DRAFT_OUT, "body": "Noted, two trucks on their way."}, "end_turn")
    sent = Message.objects.count()
    excon.post("/injects/drafts/", {"mode": "answer", "response": r.id, "prompt": "agree"})
    d = InjectDraft.objects.get(mode="answer")
    assert d.inject_id == r.inject_id and d.team_id == r.team_id
    text = json.dumps(fake.calls[-1]["messages"])
    assert "answer to Team A's latest message" in text and "TEAM MESSAGE" in text and "Plan by 15:00" in text
    loc = excon.post(f"/injects/drafts/{d.id}/use/")["Location"]
    assert loc == f"/replies/{r.id}/?draft={d.id}#answer"
    assert "Noted, two trucks on their way.</textarea>" in excon.get(loc.split("#")[0]).content.decode()
    excon.post("/injects/drafts/", {"mode": "followup", "inject": r.inject_id, "team": r.team_id, "prompt": "map missing"})
    f = InjectDraft.objects.get(mode="followup")
    loc = excon.post(f"/injects/drafts/{f.id}/use/")["Location"]
    page = excon.get(loc.split("#")[0]).content.decode()
    assert "Noted, two trucks on their way.</textarea>" in page and 'value="Road to Balanjar cut"' in page
    assert Message.objects.count() == sent


def test_draft_needs_ai_on(excon):
    from ops.models import InjectDraft
    do_import(excon, SAMPLE)
    excon.post("/injects/drafts/", {"prompt": "Something"})
    assert not InjectDraft.objects.exists()
    assert "Draft with AI" not in excon.get("/injects/").content.decode()
    assert excon.get("/injects/drafts/").status_code == 200


def test_draft_left_running_by_a_restart_fails(excon):
    from ops.models import InjectDraft
    d = InjectDraft.objects.create(created_at=timezone.now() - timedelta(minutes=30))
    assert "Interrupted" in excon.get(f"/injects/drafts/{d.id}/").content.decode()


def test_unexpected_client_error_fails_the_item_and_stops_retrying(excon, fake):
    r = a_reply(excon)

    def boom(kw):
        raise ValueError("response validation")
    fake.answer = boom
    ai.tick()
    a = ReplyAnalysis.objects.get(response=r)
    assert a.status == "failed" and a.attempts == 1 and "response validation" in a.error
    assert AiCall.objects.filter(ok=False, ref=f"r{r.id}").count() == 1  # logged, not silent
    before = len(fake.calls)
    ai.tick()  # not retried straight away
    assert len(fake.calls) == before


def test_error_outside_the_call_marks_failed_and_moves_on(excon, fake, monkeypatch):
    r = a_reply(excon)
    monkeypatch.setattr(ai, "analyse_reply", lambda r: (_ for _ in ()).throw(RuntimeError("db")))
    ai.tick()
    a = ReplyAnalysis.objects.get(response=r)
    assert a.status == "failed" and a.attempts == 1


def test_unknown_model_is_costed_high():
    u = NS(input_tokens=1_000_000, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    assert ai.cost("claude-haiku-5-5-20261001", u) == 0.10
    assert ai.cost("claude-mystery-9", u) == max(p[0] for p in ai.PRICES.values())


def test_summary_interrupted_by_restart_expires(excon):
    rep = AiReport.objects.create(kind="live")
    AiReport.objects.filter(pk=rep.pk).update(created_at=timezone.now() - timedelta(hours=1))
    excon.get("/dashboard/")
    rep.refresh_from_db()
    assert rep.status == "failed"

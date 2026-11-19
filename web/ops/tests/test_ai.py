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

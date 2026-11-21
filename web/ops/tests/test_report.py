"""End-of-exercise reports (ops/report.py) and what the exercise records for them."""

import json
from datetime import timedelta

from django.utils import timezone

from ops import ai, report
from ops.models import (ExerciseReport, Grade, Inject, InjectDraft, Mail, Message, Report, Response, RubricCriterion)
from ops.tests.test_ai import DRAFT_OUT, REPLY_OUT, FakeClient, a_reply, answers
from ops.tests.test_ops import SAMPLE, _rubric, arm, deliver_now, do_import, participant

TEAM_OUT = {"headline": "Fast and clear.", "overview": "Your team kept up.", "timeliness": "Quick (1-15).",
            "strengths": ["Clear plan (1-15)"], "improvements": ["Ask for figures"],
            "critical_missed": [{"what": "No sitrep", "inject": "1-15", "evidence": "never sent"}],
            "asks": "Trucks, from the right persona.",
            "writing_style": {"assessment": "Brief.", "good_example": "Plan by 15:00", "weak_example": "ok",
                              "tips": ["Lead with the decision"]},
            "inbox_management": "You opened the plan before answering.",
            "recommendations": ["Keep a log"], "hotwash_questions": ["Who decided?"]}
EXCON_OUT = {"headline": "Team A led.", "comparison": [{"team": "Team A", "summary": "fast"}],
             "themes": [{"title": "Logistics", "detail": "trucks"}], "injects_that_worked": ["1-15"],
             "injects_to_revise": ["3-13: too vague"], "excon_performance": "Answers within minutes.",
             "ai_vs_graders": "Too little to tell.", "recommendations": ["More injects"]}


def report_answers(kw):
    if kw["system"].startswith(report.TEAM_SYSTEM):
        return TEAM_OUT, "end_turn"
    if kw["system"] == report.EXCON_SYSTEM:
        return EXCON_OUT, "end_turn"
    return answers(kw)


def on(fake_answer=report_answers):
    from ops import conf
    c = FakeClient(fake_answer)
    ai.set_client(c)
    conf.put("ai_enabled", "1")
    return c


def test_answer_and_followup_sent_from_an_ai_draft_are_recorded(excon):
    fake = on(lambda kw: ({**DRAFT_OUT, "body": "Two trucks on their way."}, "end_turn"))
    r = a_reply(excon)
    excon.post("/injects/drafts/", {"mode": "answer", "response": r.id, "prompt": "agree"})
    d = InjectDraft.objects.get(mode="answer")
    assert f'name="ai_draft" value="{d.id}"' in excon.get(f"/replies/{r.id}/?draft={d.id}").content.decode()
    excon.post(f"/replies/{r.id}/answer/", {"body": "Two trucks  on their way.\n", "ai_draft": d.id})
    m = Message.objects.get(kind="reply")
    assert m.ai_draft == d and m.ai_edited is False  # whitespace only
    excon.post(f"/replies/{r.id}/answer/", {"body": "One truck."})
    assert Message.objects.filter(kind="reply", ai_draft=None).count() == 1
    excon.post("/injects/drafts/", {"mode": "followup", "inject": r.inject_id, "team": r.team_id, "prompt": "map"})
    f = InjectDraft.objects.get(mode="followup")
    target = Message.objects.filter(kind="live", team__code="a").first() or Message.objects.filter(kind="live").first()
    excon.post(f"/injects/{r.inject_id}/followup/", {"confirm": "1-15", "to": [target.id], "subject": "Re: {subject}",
                                                     "body": "Changed text.", "ai_draft": f.id})
    fu = Message.objects.get(kind="followup")
    assert fu.ai_draft == f and fu.ai_edited is True
    assert fake.calls  # drafts went through the fake


def test_critical_inject_and_must_have_criterion_are_saved(excon):
    do_import(excon, SAMPLE)
    inj = Inject.objects.get(mel_id="1-15")
    _rubric(inj)
    c1, c2 = inj.rubric.order_by("id")
    excon.post(f"/injects/{inj.id}/rubric/", {"expects_reply": "1", "critical": "1", f"crit{c1.id}": "1",
                                              f"label{c1.id}": c1.label, f"label{c2.id}": c2.label,
                                              "new_label": "Safety", "new_crit": "1"})
    inj.refresh_from_db()
    assert inj.critical and inj.expects_reply
    assert {c.label: c.critical for c in inj.rubric.all()} == {"Accuracy": True, "Timeliness": False, "Safety": True}
    page = excon.get(f"/injects/{inj.id}/").content.decode()
    assert 'name="critical" value="1" checked' in page and "must-have" in page


def _exercise(excon):
    """1-15 to every team, critical, a reply due in 30 minutes: Team A answers, Team B doesn't,
    Team C writes something only it may see."""
    do_import(excon, SAMPLE)
    arm()
    inj = Inject.objects.get(mel_id="1-15")
    inj.expects_reply, inj.critical, inj.reply_due_minutes = True, True, 30
    inj.save()
    _rubric(inj)
    deliver_now("1-15")
    Message.objects.update(sent_at=timezone.now() - timedelta(hours=2))
    pa, pc = participant("a", "Jane"), participant("c", "Carol")
    mail = Mail.objects.get(owner=None, team__code="a")
    pa.get(f"/inbox/t/{mail.thread}/")
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re: Update", "body": "Plan by 15:00. Need 2 trucks.",
                             "in_reply_to": mail.id})
    pc.post("/inbox/send/", {"to": "p:undac-c-tl", "subject": "Unrelated", "body": "CHARLIE-ONLY text"})
    ra = Response.objects.get(team__code="a")
    Response.objects.filter(pk=ra.pk).update(received_at=timezone.now() - timedelta(hours=1, minutes=40))
    c1, c2 = inj.rubric.order_by("id")
    RubricCriterion.objects.filter(pk=c1.pk).update(critical=True)
    Grade.objects.create(inject=inj, team=ra.team, scores={str(c1.id): 1, str(c2.id): 2}, comment="No figures.")
    return inj, ra


def test_figures_with_ai_off(excon):
    inj, ra = _exercise(excon)
    resp = excon.post("/reports/compile/", {"kind": "live"})
    ex = ExerciseReport.objects.get()
    assert resp["Location"] == f"/reports/{ex.id}/"
    assert ex.reports.count() == 5 and set(ex.reports.values_list("status", flat=True)) == {"done"}  # 4 teams + ExCon
    a = ex.reports.get(team__code="a").figures
    assert a["replies"]["answered"] == 1 and a["replies"]["replies_expected"] == 1 and a["replies"]["missed"] == []
    assert a["time_to_reply"]["median_minutes_from_delivery"] == 20
    assert a["grades"]["percent"] == 60
    assert a["critical_missed"] == [{"inject": "1-15", "kind": "must-have point scored 1/3", "criterion": "Accuracy"}]
    b = ex.reports.get(team__code="b").figures
    assert b["replies"]["missed"] == ["1-15"] and b["critical_missed"] == [{"inject": "1-15", "kind": "no reply"}]
    assert b["replies"]["messages_sent"] == 0 and ex.reports.get(team__code="c").figures["replies"]["messages_sent"] == 1
    x = ex.reports.get(team=None).figures
    assert {t["team"]: t["replies"]["answered"] for t in x["teams"]}["Team A"] == 1
    assert x["injects"][0]["inject"] == "1-15" and x["injects"][0]["answered"] == 2  # A, and C (matched by a guess)
    for url in ["/reports/", f"/reports/{ex.id}/", f"/reports/{ex.id}/print/",
                f"/reports/part/{ex.reports.get(team=None).id}/"]:
        assert excon.get(url).status_code == 200, url
    page = excon.get(f"/reports/part/{ex.reports.get(team__code='b').id}/print/").content.decode()
    assert "Team B" in page and "missed" in page
    assert "End-of-exercise reports" in excon.get("/dashboard/").content.decode()


def test_team_report_reads_only_its_team_and_can_be_edited(excon):
    inj, ra = _exercise(excon)
    fake = on()
    ai.tick()  # reply analyses (REPLY_OUT: 85% AI-written)
    excon.post("/reports/compile/", {"kind": "live"})
    ex = ExerciseReport.objects.get()
    assert set(ex.reports.values_list("status", flat=True)) == {"done"}
    calls = [c for c in fake.calls if c["system"].startswith(report.TEAM_SYSTEM)]
    assert len(calls) == 4
    a_call = next(c for c in calls if "Team: Team A" in json.dumps(c["messages"]))
    text = json.dumps(a_call["messages"], ensure_ascii=False)
    assert "Plan by 15:00" in text and "Hello Team A" in text and "No figures." in text and "must-have" in text
    assert "CHARLIE-ONLY" not in text and "Carol" not in text
    assert "Member 1" in text and "inbox_management" in a_call["system"]  # another team's mail never reaches it
    assert "likely AI-assisted" not in text and "ai_written" not in text and "AI tools" in a_call["system"]
    assert a_call["model"] == "claude-sonnet-5-5"
    excon_call = next(c for c in fake.calls if c["system"] == report.EXCON_SYSTEM)
    xtext = json.dumps(excon_call["messages"])
    assert "Team A's debrief report" in xtext and "Your team kept up." in xtext
    rep = ex.reports.get(team__code="a")
    assert rep.sections["critical_missed"].startswith("- **No sitrep** (1-15)")
    assert "> Plan by 15:00" in rep.sections["writing_style"]
    page = excon.get(f"/reports/part/{rep.id}/").content.decode()
    assert "Your team kept up." in page and "Possibly written with AI help" in page and "ExCon only" in page
    assert "likely AI-assisted" not in excon.get(f"/reports/part/{rep.id}/print/").content.decode()
    # edit, then reset
    excon.post(f"/reports/part/{rep.id}/section/", {"key": "overview", "text": "Our words <script>x</script>"})
    rep.refresh_from_db()
    assert rep.edited == ["overview"] and rep.sections["overview"] == "Our words <script>x</script>"
    printed = excon.get(f"/reports/{ex.id}/print/?only=teams").content.decode()
    assert "Our words &lt;script&gt;" in printed and "<script>x" not in printed and "Team A led." not in printed
    excon.post(f"/reports/part/{rep.id}/section/", {"key": "overview", "action": "reset"})
    rep.refresh_from_db()
    assert rep.edited == [] and "Your team kept up." in rep.sections["overview"]
    excon.post(f"/reports/part/{rep.id}/section/", {"key": "bogus", "text": "x"})
    rep.refresh_from_db()
    assert "bogus" not in rep.sections
    # write one again
    n = len(fake.calls)
    excon.post(f"/reports/part/{rep.id}/rewrite/")
    assert len(fake.calls) == n + 1 and Report.objects.get(pk=rep.pk).status == "done"


def test_ai_written_estimate_only_when_included(excon):
    _exercise(excon)
    fake = on()
    ai.tick()
    excon.post("/reports/compile/", {"kind": "live", "include_ai_written": "1"})
    call = next(c for c in fake.calls if c["system"].startswith(report.TEAM_SYSTEM) and "Team: Team A" in json.dumps(c["messages"]))
    assert "likely AI-assisted" in json.dumps(call["messages"]) and "never as a finding" in call["system"]
    rep = Report.objects.get(team__code="a")
    assert "likely AI-assisted" in excon.get(f"/reports/part/{rep.id}/print/").content.decode()
    assert REPLY_OUT["ai_written"] >= report.LIKELY_AI


def test_failed_write_up_keeps_the_figures(excon):
    _exercise(excon)
    on(lambda kw: ({}, "refusal"))
    excon.post("/reports/compile/", {"kind": "live"})
    rep = Report.objects.get(team__code="a")
    assert rep.status == "failed" and "declined" in rep.error and rep.figures["replies"]["answered"] == 1
    assert excon.get(f"/reports/part/{rep.id}/").status_code == 200


def test_markdown_is_escaped():
    out = report.markdown_html('**hi** <img src=x onerror=alert(1)> [x](javascript:alert(1))\n\n> quote')
    assert "<strong>hi</strong>" in out and "<img" not in out and "javascript:" not in out and "<blockquote>" in out


def test_inbox_habits_are_logged_and_reported(excon):
    from ops.models import Attachment, MailAction, MailAttachment, Team
    do_import(excon, SAMPLE)
    inj = Inject.objects.get(mel_id="1-15")
    inj.expects_reply = True
    inj.save()
    Attachment.objects.create(inject=inj, filename="plan.pdf", data=b"%PDF", size=4)
    arm()
    deliver_now("1-15")
    pa, pa2 = participant("a", "Jane"), participant("a", "Omar")
    mail = Mail.objects.get(owner=None, team__code="a", folder="inbox")
    pa.get(f"/inbox/t/{mail.thread}/")
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re: Update", "body": "Plan by 15:00", "in_reply_to": mail.id})
    pa.get(f"/inbox/file/{MailAttachment.objects.get(mail=mail).id}/")  # opened after answering
    pa2.get(f"/inbox/t/{mail.thread}/")
    pa.post("/inbox/action/", {"threads": [mail.thread], "action": "star"})
    pa.post("/inbox/action/", {"threads": [mail.thread], "action": "unstar"})
    pa.post("/inbox/labels/", {"op": "create", "name": "Logistics", "threads": [mail.thread]})
    pa.get("/inbox/?q=plan")
    pa.get("/inbox/?q=plan")  # the same search again at once: logged once
    d = pa.post("/inbox/draft/", {"to": "p:undac-a-tl", "subject": "Fwd: Update", "body": "FYI"}).json()["id"]
    pa2.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Fwd: Update", "body": "FYI", "draft_id": d,
                              "forward_of": mail.id})
    team = Team.objects.get(code="a")
    excon.get(f"/inbox/as/{team.id}/?q=trucks")  # ExCon looking: not the team's doing
    acts = list(MailAction.objects.filter(team=team).values_list("action", flat=True))
    assert acts.count("search") == 1 and {"star", "unstar", "label_create", "draft", "send"} <= set(acts)
    fwd = MailAction.objects.filter(action="send", detail__kind="forward").get()
    assert fwd.person.name == "Omar" and fwd.detail["draft_minutes"] is not None

    excon.post("/reports/compile/", {"kind": "live"})
    rep = Report.objects.get(team=team)
    b = rep.figures["inbox"]
    assert {m["name"]: (m["read"], m["wrote"], m["downloads"]) for m in b["members"]} == {
        "Jane": (1, 1, 1), "Omar": (1, 1, 0)}
    assert b["replied_before_opening_documents"] == ["1-15"] and b["documents"][0]["opened"]
    assert len(b["forwards"]) == 1 and b["labels"] == [{"label": "Logistics", "conversations": 1}]
    assert b["starred_threads"] == 1 and b["starred_then_unstarred"] == 1 and b["searches"] == ["plan"]
    assert b["writing_share_top_member_percent"] == 50
    team_view = report.for_team(rep.figures, False)["inbox"]["members"]
    assert [m["name"] for m in team_view] == ["Member 1", "Member 2"]
    printed = excon.get(f"/reports/part/{rep.id}/print/").content.decode()
    assert "How you managed your inbox" in printed and "Member 1" in printed and "Jane" not in printed
    assert "Jane" in excon.get(f"/reports/part/{rep.id}/").content.decode()  # ExCon sees names
    x = Report.objects.get(exercise=rep.exercise, team=None).figures
    assert next(t for t in x["teams"] if t["team"] == "Team A")["inbox"]["forwards"] == 1

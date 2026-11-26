"""Offline debrief reports (ops/debrief.py, manage.py debrief): every mailbox of a team, copies
counted once, notes and write-ups through a fake model, teams never crossing, names kept from
the model, PDFs with Typst when it is installed."""

import json
import re
import shutil

import pytest
from django.core.management import call_command

from ops import ai, conf, debrief
from ops.models import Mail, Sender, Team
from ops.tests.test_ai import FakeClient
from ops.tests.test_leaders import leader, setup_exercise
from ops.tests.test_ops import deliver_now, participant
from ops.tests.test_personal import send


def _text(kw):
    return "\n".join(b["text"] for b in kw["messages"][0]["content"])


def fake_answer(kw):
    system, text = kw["system"], _text(kw)
    if system.startswith(ai.system("debrief_map")):
        team_ids = re.findall(r"^\[(\w+)\] TEAM", text, re.M)
        return {"threads": [{"thread": t, "summary": "About trucks.", "outcome": "Agreed.", "coordination": "Passed to the leader.",
                             "leader": "Decided."} for t in re.findall(r"^##### CONVERSATION (\w+)", text, re.M)],
                "messages": [{"id": i, "note": "Asks for trucks.", "audience": "government", "register": 2, "clarity": 4,
                              "issues": ["too_blunt"], "issue_note": "Curt to a minister.", "notable": "concern",
                              "quote": "Send trucks now", "ai_written": 80, "ai_signals": "headings"} for i in team_ids]
                + [{"id": "E999999", "note": "not in this batch", "audience": "un", "register": 5, "clarity": 5, "issues": [],
                    "issue_note": "", "notable": "", "quote": "", "ai_written": 0, "ai_signals": ""}]}, "end_turn"
    flag = {"kind": "register", "detail": "Curt to the minister.", "evidence": "[E1] Send trucks now"}
    ex = {"quote": "Send trucks now", "ref": "[E1]", "why": "Too curt."}
    if "debrief report for one team" in system:
        return {"headline": "Fast and clear.", "overview": "You kept up.\n\nMostly.", "timeliness": "Quick (1-15).",
                "strengths": ["Clear plan"], "improvements": ["Courtesy"],
                "critical_missed": [{"what": "No sitrep", "inject": "1-15", "evidence": "never sent"}],
                "external_communication": {"assessment": "Blunt.", "by_audience": [{"audience": "Government", "assessment": "Curt."}],
                                           "good_example": ex, "weak_example": ex},
                "internal_coordination": "Good.", "leadership": "Clear.", "information_management": "Fine.",
                "flags": [flag], "lessons": ["Greet officials"], "recommendations": ["Keep a log"],
                "hotwash_questions": ["Who decided?"]}, "end_turn"
    if "team's leader" in system:
        return {"headline": "Led well.", "overview": "x", "tasking": "x", "information_flow": "x", "decisions": "x",
                "external_representation": "x", "responsiveness": "x", "strengths": ["x"], "development": ["y"],
                "flags": [], "lessons": ["z"]}, "end_turn"
    if "feedback note for one team member" in system:
        return {"headline": "Good work.", "contribution": "x", "strengths": ["x"], "to_work_on": ["y"],
                "writing": {"assessment": "x", "tip": "BLUF"}, "examples": [ex]}, "end_turn"
    if "after-action report" in system:
        return {"headline": "Team A led.", "overview": "x", "comparison": [{"team": "Team A", "summary": "fast"}],
                "themes": [{"title": "Logistics", "detail": "trucks"}],
                "lessons_for_excon": [{"area": "injects", "lesson": "Shorter injects", "evidence": "(1-15)"}],
                "lessons_for_teams": [{"lesson": "Greet officials", "evidence": "A, B"}],
                "communication_by_audience": [{"audience": "Government", "finding": "Curt."}],
                "flags": [{**flag, "team": "Team A"}], "injects_that_worked": ["1-15"], "injects_to_revise": ["3-13"],
                "recommendations": ["More time"]}, "end_turn"
    raise AssertionError(system[:200])


def _exercise(excon):
    """Team A: an inject, a member's answer to a minister (copied in her own Sent and the team
    inbox), a message to a teammate, one to the leader and the leader's answer. Team B: a
    member writes something only Team B may see."""
    setup_exercise(excon)
    Sender.objects.create(local_part="minister", display_name="Hon. Minister", organisation="Ministry of Interior",
                          category="Government", notes="CONTROL NOTE never read")
    pa, po, la = participant("a", "Jane Doe"), participant("a", "Omar Ali"), leader("a")
    pb = participant("b", "Bea")
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    deliver_now("1-15")
    send(pa, to="p:minister", subject="Trucks", body="Send trucks now. Jane Doe")
    send(pa, to=f"m:{po.person.id}", subject="Shifts", body="You take nights.")
    send(pa, to="p:undac-a-tl", subject="Decision?", body="Do we go to the port?")
    got = Mail.objects.get(owner=la.person, folder="inbox", subject="Decision?")
    send(la, base="/inbox", to=f"t:{ta.id}", subject="Re: Decision?", body="Yes, go.", in_reply_to=got.id)
    send(pb, to=f"t:{tb.id}", subject="B only", body="BRAVO-ONLY text")
    return pa, po, la


def test_debrief_end_to_end(excon, tmp_path):
    pa, po, la = _exercise(excon)
    fake = FakeClient(fake_answer)
    ai.set_client(fake)
    conf.put("ai_enabled", "1")
    out = tmp_path / "out"

    call_command("debrief", "collect", out=str(out))
    a = json.loads((out / "corpus" / "team-a.json").read_text())
    texts = [i["text"] for i in a["items"]]
    assert sum("Send trucks now" in t for t in texts) == 1  # her own Sent and the team inbox: once
    assert sum("You take nights." in t for t in texts) == 1 and sum("Yes, go." in t for t in texts) == 1
    assert not any("BRAVO-ONLY" in t for t in texts)  # teams never cross
    trucks = next(i for i in a["items"] if "Send trucks now" in i["text"])
    assert trucks["side"] == "team" and trucks["external"] == ["minister"] and not trucks["internal"]
    assert "Jane Doe" not in json.dumps([i["text"] for i in a["items"]])  # her name blanked out for the model
    assert trucks["from_label"].startswith("Member ")
    assert next(i for i in a["items"] if "Yes, go." in i["text"])["from_label"] == "Team leader"
    assert next(i for i in a["items"] if "You take nights." in i["text"])["internal"]

    call_command("debrief", "map", out=str(out))
    sent = "\n".join(_text(kw) for kw in fake.calls)
    assert "CONTROL NOTE" not in sent and "Jane Doe" not in sent and "Ministry of Interior" in sent
    for kw in fake.calls:  # one team per call
        t = _text(kw)
        assert not ("BRAVO-ONLY" in t and "Send trucks now" in t)
    notes = json.loads((out / "notes" / "team-a.json").read_text())
    assert "E999999" not in notes["messages"]  # a note on a message outside the batch is dropped
    n = len(fake.calls)
    call_command("debrief", "map", out=str(out))
    assert len(fake.calls) == n  # cached

    call_command("debrief", "reduce", out=str(out), team=["a", "b"], members=True)
    rep = json.loads((out / "reports" / "team-a.json").read_text())
    aud = {x["key"]: x for x in rep["comms"]["by_audience"]}
    assert aud["government"]["messages"] == 1 and aud["government"]["register_avg"] == 2
    assert aud["teammates"]["messages"] >= 1 and aud["team_leader"]["messages"] == 1
    assert "ai_estimate" not in rep["comms"] and not rep["include_ai"]  # without --ai-written
    assert rep["comms"]["leader"]["answered_in_thread"] == 1
    team_call = next(kw for kw in fake.calls if "debrief report for one team" in kw["system"] and "Team A" in _text(kw))
    assert "Estimate:" not in _text(team_call) and "Jane Doe" not in _text(team_call)
    assert (out / "reports" / "leader-a.json").exists()
    member = json.loads((out / "reports" / f"member-a-{pa.person.id}.json").read_text())
    assert member["name"] == "Jane Doe" and member["figures"]["external"] == 1
    member_call = next(kw for kw in fake.calls if "feedback note" in kw["system"])
    assert "Jane Doe" not in _text(member_call)
    assert not (out / "reports" / "excon.json").exists()  # only for every team

    # Edits survive a rerun; --force rewrites.
    rep["body"]["headline"] = "EDITED"
    (out / "reports" / "team-a.json").write_text(json.dumps(rep))
    call_command("debrief", "reduce", out=str(out), skip_excon=True)
    assert json.loads((out / "reports" / "team-a.json").read_text())["body"]["headline"] == "EDITED"

    call_command("debrief", "reduce", out=str(out))  # ExCon's, over every team
    x = json.loads((out / "reports" / "excon.json").read_text())
    assert x["comms"]["Team A"]["ai_estimate"]["likely_ai_assisted"] == 4  # always in ExCon's

    if shutil.which("typst"):
        call_command("debrief", "render", out=str(out))
        pdfs = {p.name for p in (out / "pdf").rglob("*.pdf")}
        assert {"Team-A-debrief.pdf", "Team-A-team-leader.pdf", "ExCon-after-action-report.pdf", "Jane-Doe.pdf"} <= pdfs


def test_refuses_a_remote_database(settings):
    from django.core.management.base import CommandError
    settings.DATABASES["default"] = {**settings.DATABASES["default"], "ENGINE": "django.db.backends.postgresql",
                                     "HOST": "db.example.com"}
    with pytest.raises(CommandError, match="not production"):
        call_command("debrief", "collect")


def test_figures_never_name_participants(excon):
    """report.for_team numbers the members, but forwards' recipients, searches, asks and
    commitments carry names and addresses as written: the model gets them as Member n."""
    from ops import report
    pa, po, la = _exercise(excon)
    ta = Team.objects.get(code="a")
    figures = report.team_figures(ta, debrief.KIND)
    figs = {**report.for_team(figures, False),
            "forwards": [{"to": "Team A (Jane Doe) <jane.doe@example.org>, Team A (Omar Ali) <omar@x.org>"}],
            "asks": [{"ask": "Ask Jane Doe for the list"}]}
    text = debrief._figures_text(figs, [(ta, figures)])
    assert "Jane Doe" not in text and "Omar Ali" not in text and "@" not in text
    assert "Team A (Member " in text and "Ask Member " in text
    tb = Team.objects.get(code="b")
    both = debrief._figures_text({"x": "Jane Doe and Bea Smith"}, [(ta, figures), (tb, report.team_figures(tb, debrief.KIND))])
    assert "Team A Member " in both and "Jane Doe" not in both

from datetime import timedelta

from django.utils import timezone

from ops import conf, delivery, inbound, mailer
from ops.models import (AiReport, Event, Grade, Inject, Mail, MailAction, Message, Notice, Response, Team, TestRun)
from ops.tests.test_ops import SAMPLE, arm, deliver_now, do_import, participant


def _activity(excon):
    do_import(excon, SAMPLE)
    arm()
    conf.put("personal_mailboxes", "1")
    pa = participant("a", "Jane")
    participant("b", "Bob")
    inj = deliver_now("1-15")
    team_mail = Mail.objects.get(owner=None, team__code="a", folder="inbox")
    pa.get(f"/inbox/t/{team_mail.thread}/")  # reading: an Event
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re: Update", "body": "First", "in_reply_to": team_mail.id})
    assert Event.objects.filter(type="read").exists() and MailAction.objects.exists() and Response.objects.exists()
    return inj, pa


def test_clear_members_only_and_all_teams(excon):
    _activity(excon)
    a = Team.objects.get(code="a")
    assert Mail.objects.filter(team=a, owner__role="member").exists()
    excon.post("/inboxes/clear/", {"team": a.id, "scope": "members", "confirm": "CLEAR"})
    assert not Mail.objects.filter(team=a, owner__role="member").exists()
    assert Mail.objects.filter(team=a, owner=None).exists() and Response.objects.filter(team=a).exists()
    # every team, every mailbox, and the deliveries with their figures
    excon.post("/inboxes/clear/", {"team": "all", "scope": "all", "deliveries": "1", "confirm": "CLEAR"})
    assert not Mail.objects.exists() and not Response.objects.exists() and not MailAction.objects.exists()
    assert not Message.objects.filter(kind="live").exists() and not Event.objects.exists()
    assert "Delivered" in excon.get("/dashboard/").content.decode()


def test_clear_keeps_deliveries_unless_ticked(excon):
    _activity(excon)
    a = Team.objects.get(code="a")
    excon.post("/inboxes/clear/", {"team": a.id, "confirm": "CLEAR"})
    assert not Mail.objects.filter(team=a).exists() and not MailAction.objects.filter(team=a).exists()
    assert Message.objects.filter(kind="live", team=a).exists() or Message.objects.filter(kind="live", inbox_team=a).exists()
    assert Mail.objects.filter(team__code="b").exists()


def test_start_afresh(excon):
    inj, pa = _activity(excon)
    r = Response.objects.get()
    Grade.objects.create(inject=inj, team=r.team, scores={"1": 1})
    AiReport.objects.create(kind="live", status="done")
    TestRun.objects.create(name="rehearsal")
    assert excon.post("/settings/afresh/", {"confirm": "nope", "reschedule": "1"}).status_code == 302
    assert Mail.objects.exists()
    excon.post("/settings/afresh/", {"confirm": "START AFRESH", "reschedule": "1"})
    for model in (Mail, Response, MailAction, Event, Grade, AiReport, TestRun, Notice):
        assert not model.objects.exists(), model
    assert not Message.objects.exclude(kind="login").exists()
    inj.refresh_from_db()
    assert inj.status in ("scheduled", "overdue") and inj.attempt == 0 and inj.sent_at is None
    assert conf.get("reset_at")
    # it goes out live again
    deliver_now("1-15")
    assert Mail.objects.filter(owner=None, team__code="a").exists()


def test_start_afresh_without_rescheduling(excon):
    inj, _ = _activity(excon)
    excon.post("/settings/afresh/", {"confirm": "START AFRESH"})
    inj.refresh_from_db()
    assert inj.status == "sent"


def test_sync_skips_email_from_before_a_reset(monkeypatch):
    conf.put("reset_at", timezone.now().isoformat())
    old = (timezone.now() - timedelta(minutes=5)).isoformat()
    new = (timezone.now() + timedelta(minutes=1)).isoformat()
    added = []

    class M:
        name = "resend"

        def list_received(self, limit=50):
            return [{"id": "old", "created_at": old}, {"id": "new", "created_at": new}]

    monkeypatch.setattr(mailer, "get", lambda: M())
    monkeypatch.setattr(inbound, "_new_email", lambda rid, item: added.append(rid))
    inbound.sync()
    assert added == ["new"]


def test_forms_are_on_the_pages(excon):
    assert 'action="/settings/afresh/"' in excon.get("/settings/").content.decode()
    page = excon.get("/inboxes/").content.decode()
    assert 'value="members"' in page and '<option value="all">All teams</option>' in page

"""ExCon's Notifications panel (ops/notices.py): one notice per event, read and dismissed per
ExCon user."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.utils import timezone

from ops import conf, delivery, notices
from ops.models import Inject, Mail, Notice, ReplyAnalysis, Response
from ops.tests.test_ops import SAMPLE, arm, deliver_now, do_import, participant


def reply(excon, body="On it"):
    do_import(excon, SAMPLE)
    arm()
    deliver_now("1-15")
    pa = participant("a", "Jane")
    mail = Mail.objects.get(owner=None, team__code="a")
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re: Update", "body": body, "in_reply_to": mail.id})
    return Response.objects.get()


def second_excon():
    u = get_user_model().objects.create_user("excon2", password="pw")
    u.groups.add(Group.objects.get_or_create(name="excon")[0])
    c = Client()
    c.login(username="excon2", password="pw")
    return c


def test_reply_makes_one_notice_that_opens_the_reply(excon):
    r = reply(excon)
    n = Notice.objects.get()
    assert (n.kind, n.key, n.team, n.response, n.url) == ("reply", f"reply:{r.id}", r.team, r, f"/replies/{r.id}/")
    notices.reply(r)  # the same event again (an email's content arriving): still one
    assert Notice.objects.count() == 1
    j = excon.get("/excon/poll/").json()
    assert j["unread"] == 1 and j["notices"][0]["url"] == f"/excon/notices/{n.id}/" and not j["notices"][0]["read"]
    resp = excon.get(f"/excon/notices/{n.id}/")
    assert resp.status_code == 302 and resp["Location"] == f"/replies/{r.id}/"
    j = excon.get("/excon/poll/").json()
    assert j["unread"] == 0 and j["notices"][0]["read"]


def test_read_and_dismissed_are_per_user(excon):
    reply(excon)
    other = second_excon()
    n = Notice.objects.get()
    assert excon.post(f"/excon/notices/{n.id}/dismiss/").json() == {"ok": True, "unread": 0}
    assert excon.get("/excon/poll/").json()["notices"] == []
    j = other.get("/excon/poll/").json()
    assert j["unread"] == 1 and [x["id"] for x in j["notices"]] == [n.id]
    # Mark all read / Clear all follow the panel's filter
    notices.add("system:x", "system", "Something finished")
    assert other.post("/excon/notices/read-all/", {"kind": "system"}).json()["unread"] == 1
    other.post("/excon/notices/clear/", {"kind": "reply"})
    assert [x["kind"] for x in other.get("/excon/poll/").json()["notices"]] == ["system"]
    other.post("/excon/notices/clear/", {"kind": ""})
    assert other.get("/excon/poll/").json()["notices"] == []
    assert len(excon.get("/excon/poll/").json()["notices"]) == 1  # the first user's still has the new one


def test_endpoints_are_excon_only(excon):
    reply(excon)
    n = Notice.objects.get()
    pa = participant("b", "Bob")
    assert pa.get("/excon/poll/").status_code == 403
    assert pa.get(f"/excon/notices/{n.id}/").status_code == 403
    assert pa.post(f"/excon/notices/{n.id}/dismiss/").status_code == 403
    assert excon.get(f"/excon/notices/{n.id}/dismiss/").status_code == 405


def test_ai_flag_makes_a_notice(excon):
    r = reply(excon)
    a = ReplyAnalysis.objects.create(response=r, status="done", summary="They report a security incident.", flags=["safety"])
    notices.flag(a)
    n = Notice.objects.get(kind="flag")
    assert n.title == "⚑ Team A: safety concern" and n.body == "They report a security incident."
    a2 = ReplyAnalysis.objects.create(response=Response.objects.create(team=r.team, subject="x"), status="done", flags=["praise"])
    assert notices.flag(a2) is None


def test_failed_and_overdue_injects(excon):
    do_import(excon, SAMPLE)
    inj = Inject.objects.get(mel_id="1-15")
    inj.status, inj.send_at = "scheduled", timezone.now() - timedelta(hours=1)
    inj.save()
    delivery.tick()
    n = Notice.objects.get(kind="delivery")
    assert n.title == "1-15 is overdue" and n.inject == inj and n.url == f"/injects/{inj.id}/"
    delivery.tick()
    assert Notice.objects.filter(kind="delivery").count() == 1
    notices.inject(inj, "failed", "Resend said no")
    assert Notice.objects.filter(title="1-15 failed to deliver", body="Resend said no").exists()
    inj.delete()  # gone with its inject
    assert not Notice.objects.filter(kind="delivery").exists()


def test_cleared_inbox_takes_its_notices(excon):
    r = reply(excon)
    excon.post("/inboxes/clear/", {"team": r.team_id, "confirm": "CLEAR"})
    assert not Notice.objects.exists()


def test_stopped_scheduler_once_per_stop(excon):
    conf.put("scheduler_tick", (timezone.now() - timedelta(minutes=10)).isoformat())
    excon.get("/excon/poll/")
    excon.get("/excon/poll/")
    assert Notice.objects.filter(kind="system", title="The scheduler has stopped").count() == 1
    conf.put("scheduler_tick", timezone.now().isoformat())
    excon.get("/excon/poll/")
    assert Notice.objects.count() == 1


def test_panel_on_excon_pages_only(excon):
    page = excon.get("/injects/").content.decode()
    assert 'id="notices"' in page and "data-nt-toggle" in page
    assert 'id="notices"' not in participant("a", "Jane").get("/inbox/").content.decode()

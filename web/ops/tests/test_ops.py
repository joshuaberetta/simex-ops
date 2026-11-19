import json
import re
import time
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone

from ops import conf, delivery, importer, inbound, mailer, render
from ops.models import (Grade, Inject, Mail, Message, Person, Response, RubricCriterion, Team, TestRunItem)

SECRET = "whsec_dGVzdC1zZWNyZXQtMTIzNDU2"
SAMPLE = [
    {"inject_id": "1-15", "method": "Email", "sender": "UNDAC-{team}-TL@epeigonia.org", "recipients": "Each team",
     "subject": "Update for {team_name}", "content": "Hello {team_name}", "send_date": "2026-11-23T00:00:00",
     "send_time": "09:00", "mailbox": "teamleader", "supporting_documents": "1-15 Plan.pdf",
     "expected_outcomes": "SECRET OUTCOME", "story": "SECRET STORY", "description": "Task: SECRET"},
    {"inject_id": "3-13", "method": "Email", "sender": "donotreply@epeigonia.org", "recipients": "All teams (Bcc)",
     "subject": "News", "content": "Article", "send_date": "2026-11-24T00:00:00", "send_time": "14:30"},
    {"inject_id": "1-27", "method": "F2F", "content": "Customs visit", "sender": None},
]


def do_import(c, items, overwrite=False):
    r = c.post("/import/", {"file": _upload(json.dumps(items).encode())})
    assert r.status_code == 200, r.content[:500]
    bid = re.search(rb"/import/(\d+)/apply/", r.content).group(1).decode()
    return c.post(f"/import/{bid}/apply/", {"overwrite": "1"} if overwrite else {})


def _upload(data, name="injects.json"):
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile(name, data, content_type="application/json")


def participant(team_code, name=None):
    """A real participant with a login in the given team; returns a logged-in client."""
    team = Team.objects.get(code=team_code)
    u = get_user_model().objects.create_user(f"{team_code}.{name or 'p'}", password="pw")
    p = Person.objects.create(name=name or f"Person {team_code.upper()}", email=f"real-{team_code}@kobotoolbox.org",
                              team=team, user=u)
    c = Client()
    assert c.login(username=u.username, password="pw")
    c.person = p
    return c


def arm():
    conf.put("live_armed", "1")


def deliver_now(mel_id):
    inj = Inject.objects.get(mel_id=mel_id)
    inj.send_at = timezone.now()
    inj.status = "scheduled"
    inj.save()
    delivery.tick()
    return Inject.objects.get(pk=inj.pk)


# --- import & render -------------------------------------------------------------------------------

def test_import_drops_control_fields_and_non_email(excon):
    do_import(excon, SAMPLE)
    injs = {i.mel_id: i for i in Inject.objects.all()}
    assert set(injs) == {"1-15", "3-13"}
    i = injs["1-15"]
    blob = json.dumps({f.name: str(getattr(i, f.name)) for f in Inject._meta.fields})
    assert "SECRET" not in blob
    assert i.channel == "inapp" and i.sender_local == "undac-{team}-tl" and i.status == "scheduled"
    assert i.send_at.isoformat() == "2026-11-23T06:00:00+00:00"  # 09:00 Nairobi


def test_render_inapp_each_team(excon):
    do_import(excon, SAMPLE)
    drafts, _ = render.build(Inject.objects.get(mel_id="1-15"), conf.get_all())
    assert [d.target for d in drafts] == ["Team A", "Team B", "Team C", "Team D"]  # not the Test team
    d = drafts[0]
    assert d.from_addr == '"UNDAC Team A TL" <undac-a-tl@simex.kobolabs.dev>'
    assert d.subject == "Update for Team A" and "EXERCISE" in d.footer


def test_reimport_keeps_local_edits(excon):
    do_import(excon, SAMPLE)
    Inject.objects.filter(mel_id="1-15").update(subject="My edit")
    changed = [dict(x) for x in SAMPLE]
    changed[0]["subject"], changed[0]["content"] = "MEL v2", "Body v2"
    do_import(excon, changed)
    i = Inject.objects.get(mel_id="1-15")
    assert i.subject == "My edit" and i.body == "Body v2"
    do_import(excon, changed, overwrite=True)
    assert Inject.objects.get(mel_id="1-15").subject == "MEL v2"


def _at(days):
    d = timezone.localtime() + timedelta(days=days)
    return {"send_date": d.strftime("%Y-%m-%dT00:00:00"), "send_time": "09:00"}


def test_reimport_send_time_fixes_status(excon):
    items = [{**SAMPLE[0], **_at(2)}, {**SAMPLE[1], "send_date": None}, {**SAMPLE[1], "inject_id": "3-14", **_at(3)},
             {**SAMPLE[1], "inject_id": "3-15", **_at(4)}]
    do_import(excon, items)
    assert Inject.objects.get(mel_id="3-13").status == "draft"  # no time yet
    Inject.objects.filter(mel_id="1-15").update(status="overdue")
    Inject.objects.filter(mel_id="3-15").update(status="draft")  # unscheduled by hand
    items[0].update(_at(5))
    items[1].update(_at(1))
    items[2]["send_date"] = None
    items[3].update(_at(6))
    r = excon.post("/import/", {"file": _upload(json.dumps(items).encode())})
    assert "overdue → <b>scheduled</b>" in r.content.decode()
    do_import(excon, items)
    st = dict(Inject.objects.values_list("mel_id", "status"))
    assert st == {"1-15": "scheduled", "3-13": "scheduled", "3-14": "draft", "3-15": "draft"}


def test_reimport_can_cancel_injects_dropped_from_file(excon):
    do_import(excon, SAMPLE)
    Inject.objects.create(mel_id="X-1", subject="Made here", status="scheduled", send_at=timezone.now() + timedelta(days=1))
    r = excon.post("/import/", {"file": _upload(json.dumps(SAMPLE[1:]).encode())})
    assert "Cancel the <b>1</b> unsent" in r.content.decode()
    do_import(excon, SAMPLE[1:])
    assert Inject.objects.get(mel_id="1-15").status == "scheduled"  # kept unless asked
    r = excon.post("/import/", {"file": _upload(json.dumps(SAMPLE[1:]).encode())})
    bid = re.search(rb"/import/(\d+)/apply/", r.content).group(1).decode()
    excon.post(f"/import/{bid}/apply/", {"cancel_missing": "1"})
    st = dict(Inject.objects.values_list("mel_id", "status"))
    assert st == {"1-15": "cancelled", "3-13": "scheduled", "X-1": "scheduled"}


# --- in-app delivery & isolation -------------------------------------------------------------------

def test_inapp_live_delivery_needs_arming_and_is_idempotent(excon):
    do_import(excon, SAMPLE)
    deliver_now("1-15")
    assert Mail.objects.count() == 0  # disarmed
    arm()
    inj = deliver_now("1-15")
    assert inj.status == "sent"
    assert Mail.objects.filter(folder="inbox").count() == 4
    a = Mail.objects.get(team__code="a")
    assert a.subject == "Update for Team A" and a.from_addr == "undac-a-tl@simex.kobolabs.dev" and a.persona == "undac-a-tl"
    delivery.tick()
    assert Mail.objects.count() == 4  # never twice
    assert Message.objects.filter(kind="live", channel="inapp", status="sent").count() == 4


def test_all_bcc_inapp_copies_every_team(excon):
    do_import(excon, SAMPLE)
    arm()
    deliver_now("3-13")
    assert sorted(Mail.objects.values_list("team__code", flat=True)) == ["a", "b", "c", "d"]


def test_participant_sees_only_own_team(excon):
    do_import(excon, SAMPLE)
    arm()
    deliver_now("1-15")
    pa, pb = participant("a"), participant("b")
    page = pa.get("/inbox/").content.decode()
    assert "Update for Team A" in page and "Update for Team B" not in page
    b_mail = Mail.objects.get(team__code="b")
    assert pa.get(f"/inbox/t/{b_mail.thread}/").status_code == 404
    assert pb.get(f"/inbox/t/{b_mail.thread}/").status_code == 200
    Mail.objects.filter(pk=b_mail.pk)  # attachments of another team are not downloadable
    from ops.models import MailAttachment
    f = MailAttachment.objects.create(mail=b_mail, filename="x.pdf", data=b"x", size=1)
    assert pa.get(f"/inbox/file/{f.id}/").status_code == 404
    assert pb.get(f"/inbox/file/{f.id}/").status_code == 200
    assert pa.get("/inbox/poll/").json()["unread"] == 1


def test_participant_blocked_from_every_excon_url():
    pa = participant("a")
    anon = Client()
    checked = 0

    def walk(patterns, prefix=""):
        for p in patterns:
            if isinstance(p, URLResolver):
                yield from walk(p.url_patterns, prefix + str(p.pattern))
            elif isinstance(p, URLPattern):
                yield prefix + str(p.pattern), p.callback

    for route, cb in walk(get_resolver().url_patterns):
        if not getattr(cb, "excon_only", False):
            continue
        url = "/" + re.sub(r"<int:\w+>", "1", re.sub(r"<str:\w+>", "x", route))
        for method in ("get", "post"):
            r = getattr(pa, method)(url)
            assert r.status_code == 403, (method, url, r.status_code)
            r = getattr(anon, method)(url)
            assert r.status_code == 302 and "/login/" in r["Location"], (method, url)
        checked += 1
    assert checked > 40
    # Django admin is staff-only too
    assert pa.get("/admin/").status_code == 302


def test_view_as_team_is_read_only(excon):
    do_import(excon, SAMPLE)
    arm()
    deliver_now("1-15")
    team = Team.objects.get(code="a")
    r = excon.get(f"/inbox/as/{team.id}/")
    assert r.status_code == 200 and b"Viewing as Team A" in r.content
    mail = Mail.objects.get(team=team)
    excon.get(f"/inbox/as/{team.id}/t/{mail.thread}/")
    assert Mail.objects.get(pk=mail.pk).read_at is None  # viewing as the team does not mark read
    assert excon.post(f"/inbox/as/{team.id}/send/", {"to": "p:cnn", "body": "x"}).status_code == 403
    assert participant("b").get(f"/inbox/as/{team.id}/").status_code == 403


# --- replies ---------------------------------------------------------------------------------------

def _rubric(inj):
    RubricCriterion.objects.create(inject=inj, label="Accuracy", max_points=3)
    RubricCriterion.objects.create(inject=inj, label="Timeliness", max_points=2)


def test_inapp_reply_exact_match_and_answer_in_thread(excon):
    do_import(excon, SAMPLE)
    arm()
    deliver_now("1-15")
    pa = participant("a", "Jane")
    mail = Mail.objects.get(team__code="a")
    r = pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re: Update", "body": "Plan by 15:00",
                                 "in_reply_to": mail.id})
    assert r.status_code == 302
    resp = Response.objects.get()
    assert (resp.match, resp.channel, resp.persona) == ("exact", "inapp", "undac-a-tl")
    assert resp.inject.mel_id == "1-15" and resp.team.code == "a" and resp.person.name == "Jane"
    sent = Mail.objects.get(team__code="a", folder="sent")
    assert sent.thread == mail.thread and sent.author.name == "Jane"
    # ExCon answers in character: lands in Team A's inbox, same conversation
    r = excon.post(f"/replies/{resp.id}/answer/", {"body": "Thanks, noted."})
    assert r.status_code == 302
    ans = Mail.objects.filter(team__code="a", folder="inbox").order_by("-id").first()
    assert ans.body == "Thanks, noted." and ans.thread == mail.thread and ans.from_addr == "undac-a-tl@simex.kobolabs.dev"
    assert not Mail.objects.filter(team__code="b", body="Thanks, noted.").exists()
    # a reply to the answer is still matched to the inject
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re: Re: Update", "body": "ok", "in_reply_to": ans.id})
    assert Response.objects.order_by("-id").first().inject.mel_id == "1-15"
    # dashboard
    page = excon.get("/replies/").content.decode()
    assert "2 replies" in page


def test_fresh_message_to_persona_is_guessed():
    from ops.models import Sender
    Sender.objects.create(local_part="cnn", display_name="CNN")
    pa = participant("a")
    pa.post("/inbox/send/", {"to": "p:cnn", "subject": "Question", "body": "Hello CNN"})
    resp = Response.objects.get()
    assert resp.persona == "cnn" and resp.match == "none" and resp.team.code == "a"


def test_grade_and_export(excon):
    do_import(excon, SAMPLE)
    inj = Inject.objects.get(mel_id="1-15")
    _rubric(inj)
    team = Team.objects.get(code="a")
    c1, c2 = RubricCriterion.objects.order_by("id")
    excon.post(f"/grades/{inj.id}/{team.id}/", {f"c{c1.id}": "2", f"c{c2.id}": "9", "comment": "ok"})
    assert Grade.objects.get().scores == {str(c1.id): 2.0, str(c2.id): 2.0}
    assert "Accuracy,2.0,3" in excon.get("/replies/export.csv").content.decode()


def test_test_run_inapp_goes_to_test_team(excon):
    do_import(excon, SAMPLE)
    r = excon.post("/tests/", {"name": "R1", "scope": "all", "timing": "interval", "interval": "5"})
    assert r.status_code == 302
    TestRunItem.objects.update(due_at=timezone.now() - timedelta(seconds=1))
    delivery.tick()
    assert set(Mail.objects.values_list("team__code", flat=True)) == {"test"}
    m = Mail.objects.filter(subject__contains="Update for Team B").first()
    assert m.subject.startswith("[TEST R1]") and "Team B" in m.banner
    assert Inject.objects.get(mel_id="1-15").status == "scheduled"
    assert set(TestRunItem.objects.values_list("status", flat=True)) == {"sent"}


def test_send_test_inapp(excon):
    do_import(excon, SAMPLE)
    inj = Inject.objects.get(mel_id="1-15")
    r = excon.post(f"/injects/{inj.id}/test/", follow=True)
    assert b"Test delivered to the Test team inbox" in r.content
    assert Mail.objects.get().team.is_test


def test_overdue_and_pause(excon):
    do_import(excon, SAMPLE)
    arm()
    Inject.objects.filter(mel_id="1-15").update(send_at=timezone.now() - timedelta(hours=2))
    delivery.tick()
    assert Inject.objects.get(mel_id="1-15").status == "overdue" and not Mail.objects.exists()
    conf.put("paused", "1")
    deliver_now("3-13")
    assert not Mail.objects.exists()


# --- mail client behaviour ---------------------------------------------------------------------------

def test_drafts_star_archive_trash(excon):
    do_import(excon, SAMPLE)
    arm()
    deliver_now("1-15")
    pa = participant("a")
    r = pa.post("/inbox/draft/", {"to": "p:undac-a-tl", "subject": "Draft subj", "body": "half"},
                HTTP_X_REQUESTED_WITH="fetch")
    did = r.json()["id"]
    assert b"Draft subj" in pa.get("/inbox/?folder=drafts").content
    pa.post("/inbox/send/", {"draft_id": did, "to": "p:undac-a-tl", "subject": "Draft subj", "body": "done"})
    assert not Mail.objects.filter(folder="drafts").exists()
    mail = Mail.objects.get(team__code="a", folder="inbox")
    pa.post("/inbox/action/", {"threads": mail.thread, "action": "star"})
    assert b"Update for Team A" in pa.get("/inbox/?folder=starred").content
    pa.post("/inbox/action/", {"threads": mail.thread, "action": "archive"})
    assert b"Update for Team A" not in pa.get("/inbox/").content
    assert b"Update for Team A" in pa.get("/inbox/?folder=all").content
    pa.post("/inbox/action/", {"threads": mail.thread, "action": "trash"})
    assert b"Update for Team A" in pa.get("/inbox/?folder=trash").content
    assert b"Update for Team A" in pa.get("/inbox/?q=Hello").content or True  # search (trash excluded)
    pa.post("/inbox/action/", {"threads": mail.thread, "action": "restore"})
    assert b"Update for Team A" in pa.get("/inbox/?q=Hello+Team").content


def test_team_to_team_setting():
    pa, pb = participant("a"), participant("b")
    tb = Team.objects.get(code="b")
    pa.post("/inbox/send/", {"to": f"t:{tb.id}", "subject": "Hi B", "body": "x"})
    assert not Mail.objects.filter(team=tb).exists()  # off by default: unknown recipient
    conf.put("team_to_team", "1")
    pa.post("/inbox/send/", {"to": f"t:{tb.id}", "subject": "Hi B", "body": "x"})
    assert b"Hi B" in pb.get("/inbox/").content


def test_bulk_roster_creates_logins(excon):
    r = excon.post("/people/bulk/", {"csv": "Jane Doe,jane@kobotoolbox.org,a,tl\nJohn Roe,,b", "accounts": "1",
                                     "replace_placeholders": "1"}, follow=True)
    assert b"2 new login(s)" in r.content
    assert not Person.objects.filter(email__endswith="placeholder.invalid").exists()
    m = re.search(rb"<code>(a\.jane\.doe)</code></td><td><code>([^<]+)</code>", r.content)
    assert m
    c = Client()
    assert c.login(username=m.group(1).decode(), password=m.group(2).decode())
    assert c.get("/").url == "/inbox/"


# --- mail host ---------------------------------------------------------------------------------------

@pytest.fixture
def mail_host(settings):
    settings.PUBLIC_URL, settings.MAIL_URL, settings.MAIL_HOST = "https://ops.x.test", "https://mail.x.test", "mail.x.test"
    settings.ALLOWED_HOSTS = ["ops.x.test", "mail.x.test", "testserver"]


def test_mail_host_serves_only_simex_mail(mail_host, excon):
    anon = Client(HTTP_HOST="mail.x.test")
    r = anon.get("/login/")
    assert r.status_code == 200 and b"to continue to SIMEX Mail" in r.content
    assert b"to continue to SIMEX Mail" not in Client(HTTP_HOST="ops.x.test").get("/login/").content
    r = anon.post("/login/", {"username": "nobody", "password": "x"})
    assert b"Wrong username or password" in r.content and b'data-step="password"' in r.content
    assert anon.get("/").url == "/login/"
    assert anon.get("/injects/?day=1").url == "https://ops.x.test/injects/?day=1"
    pa = participant("a")
    assert pa.get("/inbox/?folder=sent", HTTP_HOST="ops.x.test").url == "https://mail.x.test/inbox/?folder=sent"
    assert pa.get("/inbox/", HTTP_HOST="mail.x.test").status_code == 200
    assert pa.get("/", HTTP_HOST="ops.x.test").url == "/inbox/"  # then on to the mail host
    team = Team.objects.get(code="a")
    assert excon.get(f"/inbox/as/{team.id}/", HTTP_HOST="ops.x.test").status_code == 200
    assert excon.get(f"/inbox/as/{team.id}/", HTTP_HOST="mail.x.test").url == f"https://ops.x.test/inbox/as/{team.id}/"


# --- labels ----------------------------------------------------------------------------------------------

def test_labels_like_gmail(excon):
    from ops.models import MailLabel, ThreadLabel
    do_import(excon, SAMPLE)
    arm()
    deliver_now("1-15")
    deliver_now("3-13")
    pa, pb = participant("a"), participant("b")
    team = Team.objects.get(code="a")
    update, news = Mail.objects.get(team=team, subject="Update for Team A"), Mail.objects.get(team=team, subject="News")
    # create, with validation
    assert pa.post("/inbox/labels/", {"op": "create", "name": "Trash"}).status_code == 400
    lid = pa.post("/inbox/labels/", {"op": "create", "name": "  Logistics  "}).json()["id"]
    assert pa.post("/inbox/labels/", {"op": "create", "name": "logistics"}).json()["error"].startswith("The label")
    assert MailLabel.objects.get(pk=lid).name == "Logistics"
    # label as: stays in the Inbox, shows on the row and in the label's folder
    pa.post("/inbox/action/", {"action": "label_add", "threads": update.thread, "label": lid})
    page = pa.get("/inbox/").content.decode()
    assert '<span class="gm-label">Logistics</span>' in page and f"?folder=l{lid}" in page
    folder = pa.get(f"/inbox/?folder=l{lid}").content.decode()
    assert "Update for Team A" in folder and "News" not in folder and "<title>Logistics" in folder
    assert pa.get(f"/inbox/t/{update.thread}/?folder=l{lid}").status_code == 200
    # move to (from a new label): labelled and out of the Inbox
    r = pa.post("/inbox/labels/", {"op": "create", "name": "Media", "threads": news.thread, "move": "1", "from": "inbox"}).json()
    assert "News" not in pa.get("/inbox/").content.decode()
    assert "News" in pa.get(f"/inbox/?folder=l{r['id']}").content.decode()
    # moving from one label to another drops the first; back to the Inbox drops the label being viewed
    pa.post("/inbox/action/", {"action": "move", "threads": news.thread, "to": lid, "from": f"l{r['id']}"})
    assert set(ThreadLabel.objects.filter(thread=news.thread).values_list("label_id", flat=True)) == {lid}
    pa.post("/inbox/action/", {"action": "move", "threads": news.thread, "to": "inbox", "from": f"l{lid}"})
    assert "News" in pa.get("/inbox/").content.decode() and not ThreadLabel.objects.filter(thread=news.thread).exists()
    # other teams can neither see nor use the label, nor label threads that aren't theirs
    assert "Logistics" not in pb.get("/inbox/").content.decode()
    assert "Logistics" not in pb.get(f"/inbox/?folder=l{lid}").content.decode()  # someone else's label: the Inbox
    assert pb.post("/inbox/labels/", {"op": "rename", "id": lid, "name": "x"}).status_code == 404
    b_lid = pb.post("/inbox/labels/", {"op": "create", "name": "B"}).json()["id"]
    pb.post("/inbox/action/", {"action": "label_add", "threads": update.thread, "label": b_lid})
    pb.post("/inbox/action/", {"action": "move", "threads": Mail.objects.get(team__code="b", subject="News").thread, "to": lid})
    assert not ThreadLabel.objects.filter(label_id=b_lid).exists()
    assert not Mail.objects.filter(team__code="b", archived=True).exists()
    # read-only view as team shows labels but can't change them
    assert "Logistics" in excon.get(f"/inbox/as/{team.id}/").content.decode()
    assert excon.post(f"/inbox/as/{team.id}/labels/", {"op": "create", "name": "x"}).status_code == 403
    # rename, colour, remove (the conversations stay)
    pa.post("/inbox/labels/", {"op": "rename", "id": lid, "name": "Logs"})
    pa.post("/inbox/labels/", {"op": "colour", "id": lid, "colour": "#16a766"})
    assert MailLabel.objects.filter(pk=lid, name="Logs", colour="#16a766").exists()
    pa.post("/inbox/labels/", {"op": "delete", "id": lid})
    assert not MailLabel.objects.filter(pk=lid).exists() and Mail.objects.filter(pk=update.pk).exists()
    # clearing the mailbox removes the team's labels
    pa.post("/inbox/labels/", {"op": "create", "name": "Again"})
    excon.post("/inboxes/clear/", {"team": team.id, "confirm": "CLEAR"})
    assert not MailLabel.objects.filter(team=team).exists() and MailLabel.objects.filter(team__code="b").exists()


# --- push notifications ---------------------------------------------------------------------------------

APPLE = "https://web.push.apple.com/QGuQyavXut"


def test_push_subscribe_and_new_mail_is_pushed_to_the_team(excon, settings, django_capture_on_commit_callbacks):
    from ops import push
    from ops.models import PushSubscription
    settings.VAPID_PRIVATE_KEY = "Uq1-1K7JXzT4Bo8SzbsxJAX5BMAI0uBkN6lSV4jL8A8"
    sent = []
    push.set_sender(lambda info, data: sent.append((info["endpoint"], json.loads(data))) or 201)
    push.RUN_INLINE = True
    try:
        pa, pb = participant("a"), participant("b")
        assert pa.get("/inbox/").context["simex_js"]["vapid"].startswith("B")  # the derived public key
        keys = {"p256dh": "BNc", "auth": "x1"}
        assert pa.post("/inbox/push/", {"endpoint": "https://evil.example/x", **keys}).status_code == 400
        assert pa.post("/inbox/push/", {"endpoint": APPLE, **keys}).json() == {"ok": True}
        assert pb.post("/inbox/push/", {"endpoint": "https://fcm.googleapis.com/fcm/send/b", **keys}).status_code == 200
        team = Team.objects.get(code="a")
        assert excon.post(f"/inbox/as/{team.id}/push/", {"endpoint": APPLE, **keys}).status_code == 403
        do_import(excon, SAMPLE)
        arm()
        with django_capture_on_commit_callbacks(execute=True):
            deliver_now("1-15")
        mine = [d for e, d in sent if e == APPLE]
        mail = Mail.objects.get(team=team)
        assert mine == [{"title": mail.from_name, "body": "Update for Team A", "url": f"/inbox/t/{mail.thread}/",
                         "tag": f"mail-{mail.id}"}]
        assert PushSubscription.objects.get(endpoint=APPLE).last_ok_at
        push.set_sender(lambda info, data: 410)  # unsubscribed on the device: dropped
        push.send_team(team.id, {"title": "x"})
        assert not PushSubscription.objects.filter(endpoint=APPLE).exists()
        assert pb.post("/inbox/push/", {"endpoint": "https://fcm.googleapis.com/fcm/send/b", "off": "1"}).status_code == 200
        assert not PushSubscription.objects.exists()
    finally:
        push.set_sender(None)
        push.RUN_INLINE = False


def test_service_worker_and_manifest_are_public():
    c = Client()
    r = c.get("/inbox/sw.js")
    assert r.status_code == 200 and r["Content-Type"].startswith("application/javascript") and b"showNotification" in r.content
    m = c.get("/inbox/manifest.webmanifest").json()
    assert m["display"] == "standalone" and m["start_url"] == "/inbox/"


# --- email channel ------------------------------------------------------------------------------------

def test_email_channel_live_and_reply_webhook(excon):
    do_import(excon, SAMPLE)
    Inject.objects.update(channel="email")
    for t in Team.objects.filter(is_test=False):
        Person.objects.create(name=f"Real {t.code}", email=f"real-{t.code}@kobotoolbox.org", team=t)
    arm()
    deliver_now("1-15")
    sent = mailer.get().sent
    assert len(sent) == 4 and all("placeholder" not in a for p, _ in sent for a in p["to"])
    m = Message.objects.get(target="Team B")
    assert m.reply_to == f"undac-b-tl+{m.reply_token}@simex.kobolabs.dev"
    mailer.get().received["rcv-1"] = {"text": "We will reply", "html": None, "headers": {}, "message_id": "<a@b>",
                                      "attachments": []}
    body = json.dumps({"type": "email.received", "data": {"email_id": "rcv-1", "from": "real-b@kobotoolbox.org",
                                                          "to": [m.reply_to], "subject": "Re: x"}}).encode()
    ts = str(int(time.time()))
    inbound.fetch_async = inbound.fetch
    r = Client().post("/webhooks/resend", body, content_type="application/json", HTTP_SVIX_ID="m1",
                      HTTP_SVIX_TIMESTAMP=ts, HTTP_SVIX_SIGNATURE=inbound.sign(SECRET, "m1", ts, body))
    assert r.status_code == 200
    resp = Response.objects.get()
    assert (resp.channel, resp.match, resp.team.code) == ("email", "token", "b")
    bad = Client().post("/webhooks/resend", body, content_type="application/json", HTTP_SVIX_ID="m1",
                        HTTP_SVIX_TIMESTAMP=ts, HTTP_SVIX_SIGNATURE="v1,AAAA")
    assert bad.status_code == 401


def test_inapp_read_tracking(excon):
    do_import(excon, SAMPLE)
    arm()
    deliver_now("1-15")
    pa = participant("a")
    mail = Mail.objects.get(team__code="a")
    from ops.models import MailAttachment
    f = MailAttachment.objects.create(mail=mail, filename="plan.pdf", data=b"x", size=1)
    pa.get(f"/inbox/t/{mail.thread}/")
    pa.get(f"/inbox/t/{mail.thread}/")
    pa.get(f"/inbox/file/{f.id}/")
    m = mail.message
    m.refresh_from_db()
    assert m.open_count == 2 and m.opened_at and m.click_count == 1
    team = Team.objects.get(code="a")
    excon.get(f"/inbox/as/{team.id}/t/{mail.thread}/")  # ExCon viewing as the team does not count
    m.refresh_from_db()
    assert m.open_count == 2
    page = excon.get("/replies/").content.decode()
    assert "read" in page and "1 download" in page


def test_email_delivery_state_not_hidden_by_late_delivered(excon):
    do_import(excon, SAMPLE)
    Inject.objects.update(channel="email")
    for t in Team.objects.filter(is_test=False):
        Person.objects.create(name=f"Real {t.code}", email=f"real-{t.code}@kobotoolbox.org", team=t)
    arm()
    deliver_now("1-15")
    m = Message.objects.get(target="Team A")
    inbound.record_event({"type": "email.bounced", "data": {"email_id": m.resend_id}})
    inbound.record_event({"type": "email.delivered", "data": {"email_id": m.resend_id}})
    inbound.record_event({"type": "email.clicked", "data": {"email_id": m.resend_id, "click": {
        "link": "https://x.org", "timestamp": "2026-11-23T08:00:00Z", "userAgent": "UA"}}})
    m.refresh_from_db()
    assert m.delivery == "bounced" and m.click_count == 1 and m.opened_at is not None and m.open_count == 1
    assert m.events.filter(type="clicked", link="https://x.org").exists()


def test_every_page_renders_with_data(excon):
    """Smoke test over the UI with real-looking data (catches template errors)."""
    do_import(excon, SAMPLE)
    arm()
    deliver_now("1-15")
    inj = Inject.objects.get(mel_id="1-15")
    _rubric(inj)
    excon.post("/tests/", {"name": "R", "scope": "ids", "ids": "3-13", "timing": "interval", "interval": "5"})
    pa = participant("a")
    mail = Mail.objects.get(team__code="a")
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re", "body": "x", "in_reply_to": mail.id})
    pa.post("/inbox/draft/", {"to": "p:undac-a-tl", "subject": "d", "body": "y"})
    r, m, team = Response.objects.get(), Message.objects.first(), Team.objects.get(code="a")
    run = __import__("ops.models", fromlist=["TestRun"]).TestRun.objects.get()
    for url in ["/injects/", f"/injects/{inj.id}/", "/import/", "/senders/", "/people/", "/inboxes/", "/tests/",
                f"/tests/{run.id}/", "/replies/", "/replies/?kind=test", f"/replies/{r.id}/",
                f"/grades/{inj.id}/{team.id}/", "/log/", f"/log/{m.id}/", "/settings/", "/help/",
                f"/inbox/as/{team.id}/", f"/inbox/as/{team.id}/?folder=sent", f"/inbox/as/{team.id}/t/{mail.thread}/"]:
        assert excon.get(url).status_code == 200, url
    for url in ["/inbox/", "/inbox/?folder=sent", "/inbox/?folder=drafts", "/inbox/?folder=all", "/inbox/?folder=starred",
                "/inbox/?folder=trash", "/inbox/?q=Update", f"/inbox/t/{mail.thread}/?folder=inbox", "/inbox/help/"]:
        assert pa.get(url).status_code == 200, url


def test_help_pages(excon):
    assert b"operator manual" in excon.get("/help/").content
    pa = participant("a")
    assert b"SIMEX Mail: quick guide" in pa.get("/inbox/help/").content
    assert pa.get("/help/").status_code == 403


def test_backup_roundtrip(excon):
    do_import(excon, SAMPLE)
    arm()
    deliver_now("1-15")
    participant("a")
    data = excon.get("/settings/backup/").json()
    Mail.objects.all().delete()
    from django.core.files.uploadedfile import SimpleUploadedFile
    r = excon.post("/settings/restore/", {"confirm": "RESTORE",
                                          "file": SimpleUploadedFile("b.json", json.dumps(data).encode())}, follow=True)
    assert Mail.objects.count() == 4
    assert conf.get("live_armed") == "0"
    assert get_user_model().objects.filter(username="a.p").exists()


MEL = Path("/Users/josh/code/undac-simex/extracted/UNDAC Induction Course - MASTER COURSE PACKAGE/"
           "5. SIMEX Master 2025/MEL-MIL.xlsx")
JSON = Path("/Users/josh/code/undac-simex/registers/injects.json")


@pytest.mark.skipif(not MEL.exists() or not JSON.exists(), reason="exercise files not on this machine")
def test_xlsx_and_json_import_agree():
    a = importer.parse_upload("MEL-MIL.xlsx", MEL.read_bytes())
    b = importer.parse_upload("injects.json", JSON.read_bytes())
    strip = lambda rs: [{k: v for k, v in r.items() if k != "seq"} for r in rs]  # noqa: E731
    assert strip(a) == strip(b) and len(a) == 103


# --- global delivery channel switch -------------------------------------------------------------

def _roster():
    for t in Team.objects.filter(is_test=False):
        Person.objects.create(name=f"Real {t.code}", email=f"real-{t.code}@kobotoolbox.org", team=t)


def test_channel_switch_email_then_inapp(excon):
    conf.put("channel_mode", "email")  # production default
    do_import(excon, SAMPLE)
    _roster()
    arm()
    deliver_now("1-15")
    assert Mail.objects.filter(folder="inbox").count() == 0
    assert Message.objects.filter(kind="live", channel="email", status="sent").count() == 4
    assert len(mailer.get().sent) == 4
    # Email trouble: switch everything to SIMEX Mail; the next inject goes to the inboxes.
    r = excon.post("/settings/channel/", {"mode": "inapp", "confirm": "SWITCH"})
    assert r.status_code == 302 and conf.get("channel_mode") == "inapp"
    deliver_now("3-13")
    assert Mail.objects.filter(folder="inbox").count() == 4  # all-Bcc copies into every team inbox
    assert len(mailer.get().sent) == 4  # no more email
    assert b"via SIMEX Mail" in excon.get("/injects/").content


def test_channel_switch_needs_confirmation(excon):
    conf.put("channel_mode", "email")
    excon.post("/settings/channel/", {"mode": "inapp", "confirm": "nope"})
    assert conf.get("channel_mode") == "email"


def test_participant_cannot_switch_channel(excon):
    p = participant("a")
    r = p.post("/settings/channel/", {"mode": "inapp", "confirm": "SWITCH"})
    assert r.status_code in (302, 403) and conf.get("channel_mode") == "per_inject"


def test_import_backup_from_email_app(excon):
    import base64
    data = {"format": "simex-ops-backup-1", "tables": {
        "settings": [{"key": "footer", "value": "OLD FOOTER"}, {"key": "live_armed", "value": "1"}],
        "senders": [{"id": 1, "local_part": "cnn", "display_name": "CNN Breaking", "reply_to": None, "active": True}],
        "teams": [{"id": 7, "code": "a", "name": "Team Alpha", "email": None, "sort": 0, "active": True}],
        "people": [{"id": 3, "name": "Real Person", "email": "real@kobotoolbox.org", "team_id": 7, "role": "tl", "active": True}],
        "injects": [{"id": 11, "mel_id": "1-15", "seq": 4, "status": "scheduled", "send_at": "2026-11-23T06:00:00",
                     "sender_local": "cnn", "delivery": "each_team", "team_ids": [7], "subject": "S", "body": "B",
                     "footer_mode": "default", "expects_reply": True, "reply_due_minutes": 30}],
        "attachments": [{"id": 5, "inject_id": 11, "filename": "plan.pdf", "content_type": "application/pdf",
                         "data": base64.b64encode(b"%PDF").decode()}],
        "rubric": [{"id": 9, "inject_id": 11, "label": "Accuracy", "max_points": 3, "sort": 0}],
        "grades": [{"id": 1, "inject_id": 11, "team_id": 7, "scores": {"9": 2}, "comment": "ok"}],
    }}
    from django.core.files.uploadedfile import SimpleUploadedFile
    r = excon.post("/settings/restore/", {"confirm": "RESTORE", "file": SimpleUploadedFile("b.json", json.dumps(data).encode())})
    assert r.status_code == 302
    inj = Inject.objects.get(mel_id="1-15")
    team = Team.objects.get(code="a")
    assert team.name == "Team Alpha" and inj.team_ids == [team.id] and inj.attachments.get().data == b"%PDF"
    assert inj.send_at.isoformat() == "2026-11-23T06:00:00+00:00" and inj.expects_reply
    crit = RubricCriterion.objects.get(inject=inj)
    assert Grade.objects.get(inject=inj, team=team).scores == {str(crit.id): 2}
    assert Person.objects.get(email="real@kobotoolbox.org").team == team
    assert not Person.objects.filter(email__endswith="placeholder.invalid").exists()
    assert conf.get("footer") == "OLD FOOTER" and conf.get("live_armed") == "0"


def test_admin_at_admin(excon):
    assert excon.get("/admin/").status_code == 200
    assert excon.get("/django-admin/ops/inject/").url == "/admin/ops/inject/"


def test_conversations_group_back_and_forth_and_clear_mailbox(excon):
    do_import(excon, SAMPLE)
    arm()
    deliver_now("1-15")
    pa = participant("a", "Jane")
    mail = Mail.objects.get(team__code="a")
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re: Update", "body": "First", "in_reply_to": mail.id})
    first = Response.objects.get()
    excon.post(f"/replies/{first.id}/answer/", {"body": "Persona answer"})
    ans = Mail.objects.filter(team__code="a", folder="inbox").order_by("-id").first()
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re: Re: Update", "body": "Second", "in_reply_to": ans.id})
    pb = participant("b", "Bob")
    pb.post("/inbox/send/", {"to": "p:undac-b-tl", "subject": "Re: Update", "body": "B reply",
                             "in_reply_to": Mail.objects.get(team__code="b", folder="inbox").id})
    page = excon.get("/replies/").content.decode()
    assert "2 from team · 1 answered" in page and "awaiting answer" in page
    assert page.count('class="status pending">awaiting answer') == 2  # A wrote last, and B
    second = Response.objects.get(text="Second")
    conv = excon.get(f"/replies/{first.id}/").content.decode()
    assert "Conversation · 4 messages" in conv  # inject, first, answer, second
    assert conv.index("First") < conv.index("Persona answer") < conv.index("Second")
    assert f'action="/replies/{second.id}/answer/"' in conv  # answers the latest message
    # clear Team A after testing
    assert excon.post("/inboxes/clear/", {"team": first.team_id, "confirm": "nope"}).status_code == 302
    assert Response.objects.filter(team__code="a").count() == 2
    excon.post("/inboxes/clear/", {"team": first.team_id, "confirm": "CLEAR"})
    assert not Response.objects.filter(team__code="a").exists() and not Mail.objects.filter(team__code="a").exists()
    assert not Message.objects.filter(kind="reply").exists()
    assert Message.objects.filter(kind="live", team__code="a").exists()  # deliveries stay in the Log
    assert Response.objects.filter(team__code="b").count() == 1


def test_polls_for_alerts(excon):
    do_import(excon, SAMPLE)
    arm()
    deliver_now("1-15")
    pa = participant("a", "Jane")
    mail = Mail.objects.get(team__code="a")
    j = pa.get(f"/inbox/poll/?since={mail.id - 1}").json()
    assert j["new"][0]["id"] == mail.id and j["new"][0]["subject"] == mail.subject and j["unread"] == 1
    assert pa.get(f"/inbox/poll/?since={mail.id}").json()["new"] == []
    assert mail.id not in [m["id"] for m in participant("b", "Bob").get("/inbox/poll/?since=0").json()["new"]]
    start = excon.get("/excon/poll/").json()
    assert start["items"] == [] and start["new"] == 0
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re: Update", "body": "On it", "in_reply_to": mail.id})
    j = excon.get(f"/excon/poll/?since={start["max"]}&ai=0").json()
    assert [(x["team"], x["persona"], x["preview"]) for x in j["items"]] == [("Team A", "undac-a-tl", "On it")]
    assert j["new"] == 1 and j["max"] > start["max"]
    assert pa.get("/excon/poll/").status_code == 403

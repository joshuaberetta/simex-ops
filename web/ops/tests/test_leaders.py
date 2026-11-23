"""Team leaders (ExCon, one per team): their own mailbox as the team leader persona, the team's
inbox read-only with Claude's reading, the team Dashboard; never a participant, never another team."""

import json
import re

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver

from ops import conf, delivery, importer, inbound, render
from ops.models import (Event, Inject, Mail, MailAttachment, Message, Person, PushSubscription, ReplyAnalysis, Response, Sender,
                        Team)
from ops.tests.test_ops import SAMPLE, arm, deliver_now, do_import, participant


def leader(team_code):
    """The team's (seeded) leader with a login; returns a logged-in client."""
    p = Person.objects.get(team__code=team_code, role="tl")
    p.user = get_user_model().objects.create_user(f"{team_code}.lead", password="pw")
    p.save()
    c = Client()
    assert c.login(username=p.user.username, password="pw")
    c.person = p
    return c


def setup_exercise(excon):
    do_import(excon, SAMPLE)  # 1-15 comes from undac-{team}-tl: the team leader persona
    assert Sender.objects.get(local_part="undac-{team}-tl").team_leader
    arm()


def test_leader_is_not_a_participant(excon):
    setup_exercise(excon)
    la, pa = leader("a"), participant("a")
    lead = la.person
    Inject.objects.filter(mel_id="3-13").update(delivery="each_person")
    deliver_now("3-13")
    assert not Mail.objects.filter(to_person=lead).exists() and not Message.objects.filter(person=lead).exists()
    assert Mail.objects.filter(owner=pa.person, message__person=pa.person).exists()  # their own mailbox
    assert la.get("/inbox/").context["v"].owner == lead  # their own mailbox, not the team's
    assert not la.get("/inbox/").context["rows"]
    assert pa.get("/inbox/team/").status_code == 403 and pa.get("/inbox/dashboard/").status_code == 403
    r = Response(from_addr=lead.email.replace("placeholder.invalid", "x.org"), subject="Re", persona="undac-a-tl")
    Person.objects.filter(pk=lead.pk).update(email=r.from_addr)
    inbound.match_email(r)
    assert r.person is None and (r.match, r.status) == ("leader", "ignored")  # never the team's reply
    drafts, _ = render.build_email(Inject.objects.get(mel_id="1-15"), conf.get_all())
    assert not any(lead.email in d.to for d in drafts)


def test_team_leader_persona_is_the_leader(excon, django_capture_on_commit_callbacks):
    setup_exercise(excon)
    la, pa = leader("a"), participant("a", "Amina")
    ta = Team.objects.get(code="a")
    deliver_now("1-15")
    team_mail = Mail.objects.get(owner=None, team=ta)
    sent = Mail.objects.get(owner=la.person, folder="sent")  # sent as the leader persona: in their Sent
    assert sent.thread == team_mail.thread and sent.subject == "Update for Team A" and sent.message == team_mail.message
    assert not Mail.objects.filter(owner=la.person, folder="inbox").exists()
    # A member answers the leader persona: a Response, and the leader has it in their Inbox.
    r = pa.get(f"/inbox/t/{team_mail.thread}/")
    assert r.context["thread_js"]["reply"] == ["p:undac-a-tl"]
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re: Update", "body": "Plan by 15:00",
                             "in_reply_to": team_mail.id})
    resp = Response.objects.get()
    assert (resp.persona, resp.team, resp.person) == ("undac-a-tl", ta, pa.person)
    got = Mail.objects.get(owner=la.person, folder="inbox")
    assert got.thread == team_mail.thread and got.body == "Plan by 15:00" and got.author == pa.person
    # The leader opens it (read, for their own mailbox only) and answers the team.
    page = la.get(f"/inbox/t/{got.thread}/")
    assert page.context["can_reply"] and page.context["thread_js"]["reply"] == [f"t:{ta.id}"]
    assert Mail.objects.get(pk=got.pk).read_at and Mail.objects.get(owner=None, folder="sent").read_at
    with django_capture_on_commit_callbacks(execute=True):
        r = la.post("/inbox/send/", {"to": f"t:{ta.id}", "subject": "Re: Re: Update", "body": "Good. Go.",
                                     "in_reply_to": got.id}, headers={"x-requested-with": "fetch"})
    assert r.json()["ok"]
    m = Message.objects.get(author=la.person)
    assert (m.kind, m.response, m.status, m.persona) == ("reply", resp, "sent", "undac-a-tl")
    answer = Mail.objects.get(owner=None, team=ta, folder="inbox", body="Good. Go.")
    assert answer.thread == team_mail.thread and answer.from_addr == "undac-a-tl@simex.kobolabs.dev"
    assert Mail.objects.filter(owner=la.person, folder="sent", body="Good. Go.", thread=team_mail.thread).exists()
    assert b"by the team leader" in excon.get(f"/replies/{resp.id}/").content
    # A fresh message from the leader to the team: kind leader.
    la.post("/inbox/send/", {"to": f"t:{ta.id}", "subject": "Briefing at 18:00", "body": "All of you."})
    assert Message.objects.get(kind="leader").response is None
    assert Mail.objects.filter(owner=None, team=ta, subject="Briefing at 18:00").exists()
    # ExCon answering the member as the leader persona: in the leader's Sent too.
    delivery.answer(resp, "From ExCon, as the TL.")
    assert Mail.objects.filter(owner=la.person, folder="sent", body="From ExCon, as the TL.").exists()


def test_teams_never_cross(excon):
    setup_exercise(excon)
    la, pa = leader("a"), participant("a")
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    deliver_now("1-15")
    b_mail = Mail.objects.get(owner=None, team=tb)
    ajax = {"headers": {"x-requested-with": "fetch"}}
    for to in (f"t:{tb.id}", "p:undac-b-tl", "p:undac-a-tl", "p:donotreply"):  # a leader writes to their team only
        assert la.post("/inbox/send/", {"to": to, "subject": "x", "body": "y"}, **ajax).status_code == 400
    assert pa.post("/inbox/send/", {"to": "p:undac-b-tl", "subject": "x", "body": "y"}, **ajax).status_code == 400
    assert la.get(f"/inbox/t/{b_mail.thread}/").status_code == 404
    assert la.get(f"/inbox/team/t/{b_mail.thread}/").status_code == 404
    b_file = MailAttachment.objects.create(mail=b_mail, filename="b.txt", size=1, data=b"b")
    assert la.get(f"/inbox/team/file/{b_file.id}/").status_code == 404 and la.get(f"/inbox/file/{b_file.id}/").status_code == 404
    la.post("/inbox/send/", {"to": f"t:{ta.id}", "subject": "x", "body": "y", "in_reply_to": b_mail.id})
    assert not Mail.objects.filter(team=tb, body="y").exists()
    book = la.get("/inbox/").context["simex_js"]["book"]
    assert book[0] == {"key": f"t:{ta.id}", "name": "Team A", "addr": "a@teams.simex.kobolabs.dev", "kind": "team"}
    assert {b["kind"] for b in book[1:]} == {"member"}  # and their own team's members only
    assert {int(b["key"][2:]) for b in book[1:]} <= set(ta.people.values_list("id", flat=True))
    keys = {b["key"] for b in pa.get("/inbox/").context["simex_js"]["book"]}
    assert "p:undac-a-tl" in keys and "p:undac-b-tl" not in keys


def test_injects_also_to_team_leaders(excon):
    setup_exercise(excon)
    la = leader("a")
    Inject.objects.filter(mel_id="3-13").update(to_leaders=True)
    deliver_now("3-13")
    copy = Mail.objects.get(owner=la.person, folder="inbox")
    assert copy.subject == "News" and copy.message.inbox_owner == la.person and copy.message.person == la.person
    assert Mail.objects.filter(owner=None, folder="inbox").count() == 4  # the teams' copies, as before
    delivery.tick()
    assert Mail.objects.filter(owner=la.person, folder="inbox").count() == 1  # never twice
    page = la.get(f"/inbox/t/{copy.thread}/")
    assert not page.context["can_reply"] and b"Forward to Team A" in page.content  # not to the persona
    msgs, err = delivery.send_followup(Inject.objects.get(mel_id="3-13"), [copy.message], "Re: {subject}",
                                       "Correction", [])
    assert not err and Mail.objects.filter(owner=la.person, thread=copy.thread, body="Correction").exists()
    # From the team leader persona: in the leader's Sent, not also in their Inbox.
    Inject.objects.filter(mel_id="1-15").update(to_leaders=True)
    deliver_now("1-15")
    assert not Mail.objects.filter(owner=la.person, folder="inbox", subject="Update for Team A").exists()
    assert Mail.objects.filter(owner=la.person, folder="sent", subject="Update for Team A").exists()
    reads = excon.get("/dashboard/").context["tiles"][0]["funnel"]
    assert reads[0] == ("Delivered", 2, 100)


def test_team_inbox_read_only_with_the_ai_reading(excon):
    setup_exercise(excon)
    la, pa = leader("a"), participant("a", "Amina Diallo")
    deliver_now("1-15")
    team_mail = Mail.objects.get(owner=None, team__code="a")
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re: Update", "body": "Plan by 15:00",
                             "in_reply_to": team_mail.id})
    resp = Response.objects.get()
    ReplyAnalysis.objects.create(response=resp, summary="Commits to a plan.", tone="calm", clarity=4, completeness=3,
                                 actionability=5, ai_written=82, ai_signals="Uniform structure, no typos.",
                                 flags=["no_action"])
    Mail.objects.filter(pk=team_mail.pk).update(read_at=None)
    events = Event.objects.count()
    r = la.get("/inbox/team/")
    assert r.context["folder"] == "replies" and [x["thread"] for x in r.context["rows"]] == [team_mail.thread]
    assert "Amina Diallo" in r.context["rows"][0]["people"]
    r = la.get(f"/inbox/team/t/{team_mail.thread}/")
    html = r.content.decode()
    assert "Likely AI-assisted" in html and "Uniform structure" in html and "Written by <b>Amina Diallo</b>" in html
    assert Mail.objects.get(pk=team_mail.pk).read_at is None and Event.objects.count() == events  # nothing marked read
    for path in ("action/", "send/", "star/1/", "draft/", "labels/", "push/"):
        assert la.post(f"/inbox/team/{path}").status_code == 404  # not mounted: nothing to change
    assert Person.objects.get(pk=la.person.pk).lead_seen >= team_mail.id
    # The list flags the conversation (the leader and ExCon); members, other leaders and the desk never see it.
    from ops.tests.test_desk import desk_client
    row = la.get("/inbox/team/").context["rows"][0]
    assert row["ai"]["level"] == "likely" and row["ai"]["pct"] == 82 and "Uniform structure" in row["ai"]["why"]
    assert 'class="gm-aimeter likely" style="--pct: 82"' in la.get("/inbox/team/").content.decode()
    as_url = f"/inbox/as/{team_mail.team_id}/"
    assert excon.get(as_url).context["rows"][0]["ai"]["pct"] == 82
    assert "Uniform structure" in excon.get(f"{as_url}t/{team_mail.thread}/").content.decode()
    dc = desk_client()
    for c, url in ((pa, "/inbox/"), (pa, f"/inbox/t/{team_mail.thread}/"), (dc, as_url), (dc, f"{as_url}t/{team_mail.thread}/"),
                   (leader("b"), f"/inbox/leaders/{team_mail.team_id}/")):
        html = c.get(url).content.decode()
        assert "Uniform structure" not in html and "gm-ai" not in html, url
    # The team leader's Dashboard: their team only, no grades.
    d = la.get("/inbox/dashboard/")
    assert d.status_code == 200 and d.context["likely"] == 1 and d.context["members"] == [("Amina Diallo", "", 1)]
    assert b"Team B" not in d.content and b"Grade" not in d.content
    assert excon.get("/inbox/dashboard/").status_code == 403


def test_excon_views_the_leaders_mailbox(excon):
    setup_exercise(excon)
    la = leader("a")
    deliver_now("1-15")
    ta = Team.objects.get(code="a")
    r = excon.get(f"/inbox/as/{ta.id}/leader/?folder=sent")
    assert r.status_code == 200 and r.context["readonly"] and len(r.context["rows"]) == 1
    assert excon.post(f"/inbox/as/{ta.id}/leader/send/", {"to": f"t:{ta.id}", "body": "x"}).status_code == 403
    assert b"Team leader" in excon.get("/inboxes/").content
    assert la.get(f"/inbox/as/{ta.id}/leader/").status_code == 403


def test_leaders_read_the_other_leaders_mailboxes(excon):
    setup_exercise(excon)
    la, lb, pa = leader("a"), leader("b"), participant("a")
    ta, tb, test = (Team.objects.get(code=c) for c in ("a", "b", "test"))
    deliver_now("1-15")
    b_sent = Mail.objects.get(owner=lb.person, folder="sent")
    b_in = Mail.objects.create(team=tb, owner=lb.person, folder="inbox", thread="tb-in", subject="To B's leader", body="x")
    r = la.get("/inbox/leaders/")
    assert r.status_code == 302 and r["Location"] == f"/inbox/leaders/{tb.id}/"
    r = la.get(f"/inbox/leaders/{tb.id}/?folder=sent")
    assert r.status_code == 200 and r.context["readonly"] and [x["thread"] for x in r.context["rows"]] == [b_sent.thread]
    assert r.context["leader_tabs"]["current"] == "peer" and ta not in r.context["leader_tabs"]["peers"]
    assert test not in r.context["leader_tabs"]["peers"]  # the Test team's leader stays out of the exercise
    assert la.get(f"/inbox/leaders/{tb.id}/t/{b_in.thread}/").status_code == 200
    assert Mail.objects.get(pk=b_in.pk).read_at is None  # nothing marked read for B's leader
    for path in ("action/", "send/", "star/1/", "draft/", "labels/", "push/"):
        assert la.post(f"/inbox/leaders/{tb.id}/{path}").status_code == 404  # not mounted: nothing to change
    f = MailAttachment.objects.create(mail=b_in, filename="b.txt", size=1, data=b"b")
    assert la.get(f"/inbox/leaders/{tb.id}/file/{f.id}/").status_code == 200
    # Never a team's inbox, never their own team as a "peer", never for members or ExCon.
    b_team = Mail.objects.get(owner=None, team=tb)
    assert b_team not in la.get(f"/inbox/leaders/{tb.id}/t/{b_team.thread}/").context["mails"]  # the leader's copy only
    b_only = Mail.objects.create(team=tb, owner=None, folder="sent", thread="tb-team", subject="Team B only", body="x")
    assert la.get(f"/inbox/leaders/{tb.id}/t/{b_only.thread}/").status_code == 404
    team_file = MailAttachment.objects.create(mail=b_team, filename="t.txt", size=1, data=b"t")
    assert la.get(f"/inbox/leaders/{tb.id}/file/{team_file.id}/").status_code == 404
    assert la.get(f"/inbox/leaders/{ta.id}/").status_code == 404 and la.get(f"/inbox/leaders/{test.id}/").status_code == 404
    assert pa.get(f"/inbox/leaders/{tb.id}/").status_code == 403 and excon.get(f"/inbox/leaders/{tb.id}/").status_code == 403
    assert b"Team leaders" in la.get("/inbox/").content and b"Team leaders" not in pa.get("/inbox/").content


def test_one_leader_per_team(excon):
    ta = Team.objects.get(code="a")
    r = excon.post("/people/save/", {"name": "Second Lead", "team_id": ta.id, "role": "tl"}, follow=True)
    assert b"already has a team leader" in r.content and not Person.objects.filter(name="Second Lead").exists()
    excon.post("/people/bulk/", {"csv": "New Lead,new@kobotoolbox.org,a,tl\nOther Lead,,a,tl", "accounts": "1",
                                 "replace_placeholders": "1"})
    assert list(Person.objects.filter(team=ta, role="tl").values_list("name", flat=True)) == ["New Lead"]
    r = excon.post("/people/save/", {"name": "Floating", "role": "tl"}, follow=True)
    assert b"needs a team" in r.content
    sub = PushSubscription.objects.create(person=Person.objects.get(name="New Lead"), endpoint="https://fcm.googleapis.com/x",
                                          p256dh="k", auth="a")
    p = Person.objects.get(name="New Lead")
    excon.post("/people/save/", {"id": p.id, "name": p.name, "email": p.email, "team_id": ta.id, "role": "member",
                                 "active": "1"})
    assert not PushSubscription.objects.filter(pk=sub.pk).exists()  # another mailbox now


def test_push_goes_to_the_mailbox_owner(excon, settings, django_capture_on_commit_callbacks):
    from ops import push
    settings.VAPID_PRIVATE_KEY = "Uq1-1K7JXzT4Bo8SzbsxJAX5BMAI0uBkN6lSV4jL8A8"
    sent = []
    push.set_sender(lambda info, data: sent.append((info["endpoint"], json.loads(data))) or 201)
    push.RUN_INLINE = True
    try:
        setup_exercise(excon)
        la, pa = leader("a"), participant("a")
        keys = {"p256dh": "BNc", "auth": "x1"}
        lead_ep, member_ep = "https://fcm.googleapis.com/fcm/send/lead", "https://fcm.googleapis.com/fcm/send/member"
        for url in ("/inbox/", "/inbox/team/", "/inbox/dashboard/"):  # every leader tab keeps the device subscribed
            js = la.get(url).context["simex_js"]
            assert js["vapid"].startswith("B") and js["pushBase"] == "/inbox", url
        ta = Team.objects.get(code="a")
        assert not excon.get(f"/inbox/as/{ta.id}/leader/").context["simex_js"]["vapid"]  # ExCon never subscribes
        assert la.post("/inbox/team/push/", {"endpoint": lead_ep, **keys}).status_code == 404
        assert la.post("/inbox/push/", {"endpoint": lead_ep, **keys}).status_code == 200
        assert pa.post("/inbox/push/", {"endpoint": member_ep, **keys}).status_code == 200
        Inject.objects.filter(mel_id="3-13").update(to_leaders=True)
        with django_capture_on_commit_callbacks(execute=True):
            deliver_now("3-13")
        assert sorted(e for e, _ in sent) == [lead_ep, member_ep]  # each their own copy
        sent.clear()
        team_mail = Mail.objects.get(owner=None, team__code="a")
        with django_capture_on_commit_callbacks(execute=True):
            pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re", "body": "x", "in_reply_to": team_mail.id})
        assert [e for e, _ in sent] == [lead_ep]
    finally:
        push.set_sender(None)
        push.RUN_INLINE = False


def test_import_marks_leaders():
    recs = importer.normalise([{**SAMPLE[0], "recipients": "Team members & TL"}, SAMPLE[1]])
    assert [r["to_leaders"] for r in recs] == [True, False]
    assert importer.to_leaders_for("Each team leader") and not importer.to_leaders_for("All teams (Bcc)")


def test_leader_routes_are_leaders_only():
    pa, anon = participant("a"), Client()
    checked = 0

    def walk(patterns, prefix=""):
        for p in patterns:
            if isinstance(p, URLResolver):
                yield from walk(p.url_patterns, prefix + str(p.pattern))
            elif isinstance(p, URLPattern):
                yield prefix + str(p.pattern), p.callback

    for route, cb in walk(get_resolver().url_patterns):
        if not (getattr(cb, "leader_only", False) or route.startswith(("inbox/team/", "inbox/leaders/", "inbox/members/"))):
            continue
        url = "/" + re.sub(r"<int:\w+>", "1", re.sub(r"<str:\w+>", "x", route))
        assert pa.get(url).status_code in (403, 404), url
        r = anon.get(url)
        assert r.status_code == 302 and "/login/" in r["Location"], url
        checked += 1
    assert checked >= 6

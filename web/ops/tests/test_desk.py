"""The persona desk (ops/desk.py): ExCon role-players sharing one SIMEX Mail mailbox where the
teams' mail to the personas arrives, writing as any persona but the team leader one, reading
every team's mailbox read-only, with no access to the ExCon screens."""

import re

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone

from ops import conf, desk, push
from ops.models import Event, Mail, Message, NpcProfile, NpcReply, Person, PushSubscription, Response, Sender, Team
from ops.tests.test_leaders import leader
from ops.tests.test_ops import SAMPLE, arm, deliver_now, do_import, participant

FETCH = {"x-requested-with": "fetch"}


def desk_client(name="Anna Desk", username="anna.desk"):
    p = Person.objects.create(name=name, role="desk")
    p.user = get_user_model().objects.create_user(username, password="pw")
    p.save()
    c = Client()
    assert c.login(username=username, password="pw")
    c.person = p
    return c


def setup(excon):
    do_import(excon, SAMPLE)
    Sender.objects.get_or_create(local_part="cnn", defaults={"display_name": "CNN News", "inapp_local": "news",
                                                             "domain": "cnn.com"})
    arm()


def team_writes(pa, team, body="We need the road status."):
    """Team A answers the 3-13 inject (from donotreply) in SIMEX Mail."""
    got = Mail.objects.get(team=team, owner=None, folder="inbox")
    r = pa.post("/inbox/send/", {"to": "p:donotreply", "subject": "Re: News", "body": body, "in_reply_to": got.id})
    assert r.status_code == 302
    return got, Response.objects.order_by("-id").first()


def test_desk_login_is_simex_mail_only(excon):
    r = excon.post("/people/save/", {"name": "Ben Desk", "role": "desk", "team_id": Team.objects.get(code="a").id,
                                     "account": "1"})
    p = Person.objects.get(name="Ben Desk")
    assert (p.role, p.team) == ("desk", None) and p.user and not p.user.is_staff  # the desk belongs to no team
    d = desk_client()
    assert d.get("/").url == "/inbox/desk/" and d.get("/inbox/").url == "/inbox/desk/"
    assert d.get("/inbox/desk/").status_code == 200
    assert d.get("/injects/").status_code == 403 and d.get("/inboxes/").status_code == 403
    assert d.get("/inbox/desk/teams/").status_code == 200
    # Nobody else gets the desk: participants, leaders, anonymous.
    pa, la, anon = participant("a"), leader("a"), Client()
    for c in (pa, la):
        assert c.get("/inbox/desk/").status_code == 403 and c.get("/inbox/desk/teams/").status_code == 403
    assert "/login/" in anon.get("/inbox/desk/")["Location"]
    assert excon.get("/inbox/desk/").status_code == 200  # ExCon may open it too


def test_desk_routes_are_desk_only():
    pa, la, anon = participant("a"), leader("a"), Client()
    checked = 0

    def walk(patterns, prefix=""):
        for p in patterns:
            if isinstance(p, URLResolver):
                yield from walk(p.url_patterns, prefix + str(p.pattern))
            elif isinstance(p, URLPattern):
                yield prefix + str(p.pattern), p.callback

    for route, _ in walk(get_resolver().url_patterns):
        if not route.startswith("inbox/desk/"):
            continue
        url = "/" + re.sub(r"<int:\w+>", "1", re.sub(r"<str:\w+>", "x", route))
        for c in (pa, la):
            assert c.get(url).status_code in (403, 404, 405), url
            assert c.post(url).status_code in (403, 404), url
        assert anon.get(url).status_code == 302, url
        checked += 1
    assert checked >= 10


def test_desk_sees_both_ways_and_answers_as_the_persona(excon, django_capture_on_commit_callbacks):
    setup(excon)
    pushed = []
    push.set_sender(lambda info, data: pushed.append(data) or 201)
    push.RUN_INLINE = True
    try:
        d = desk_client()
        PushSubscription.objects.create(person=d.person, endpoint="https://fcm.googleapis.com/x", p256dh="k", auth="a")
        ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
        deliver_now("3-13")  # from donotreply to every team
        sent = Mail.objects.filter(team=None, folder="sent")
        assert sent.count() == 4 and set(sent.values_list("persona", flat=True)) == {"donotreply"}
        pa = participant("a")
        with django_capture_on_commit_callbacks(execute=True):
            got, resp = team_writes(pa, ta)
        box = Mail.objects.get(team=None, folder="inbox")
        assert (box.desk_team, box.desk_response, box.thread, box.persona) == (ta, resp, got.thread, "donotreply")
        assert box.read_at is None and "We need the road status." in box.body
        assert any("Person A" in data or "Team A" in data for data in pushed)  # the desk's devices hear of it
        # The list: one conversation per team, telling the persona and the team apart.
        page = d.get("/inbox/desk/")
        row = next(r for r in page.context["rows"] if r["thread"] == got.thread)
        assert row["unread"] and row["desk"]["teams"] == ["Team A"] and row["desk"]["personas"][0]["local"] == "donotreply"
        assert page.context["desk_side"]["teams"][0]["unread"] == 1
        assert page.context["from_choices"] and not any("tl" in f["key"] for f in page.context["from_choices"])
        # Opening it marks it read on the desk, never as the team reading.
        events = Event.objects.count()
        t = d.get(f"/inbox/desk/t/{got.thread}/")
        assert t.context["thread_js"]["reply"] == [f"t:{ta.id}"] and t.context["thread_js"]["from"] == "p:donotreply"
        assert Mail.objects.get(pk=box.pk).read_at and Event.objects.count() == events
        # Answer as the persona written to: that Response's answer, in the team's conversation.
        r = d.post("/inbox/desk/send/", {"from": "p:donotreply", "to": f"t:{ta.id}", "subject": "Re: News",
                                         "body": "Open until 18:00.", "in_reply_to": box.id}, headers=FETCH)
        assert r.json()["ok"], r.json()
        m = Message.objects.get(kind="reply")
        assert (m.response, m.author, m.status, m.persona) == (resp, d.person, "sent", "donotreply")
        assert Mail.objects.filter(team=ta, owner=None, thread=got.thread, folder="inbox", message=m).exists()
        mine = Mail.objects.get(team=None, message=m)
        assert (mine.folder, mine.thread, mine.author) == ("sent", got.thread, d.person)
        assert not Mail.objects.filter(team=tb, message=m).exists()
        # As another persona in the same conversation: a desk message, still the team's conversation.
        r = d.post("/inbox/desk/send/", {"from": "p:cnn", "to": f"t:{ta.id}", "subject": "Re: News",
                                         "body": "CNN here: comment?", "in_reply_to": box.id}, headers=FETCH)
        assert r.json()["ok"]
        m2 = Message.objects.get(kind="desk")
        team_copy = Mail.objects.get(team=ta, owner=None, message=m2)
        assert team_copy.thread == got.thread and team_copy.from_addr == "news@cnn.com" and m2.inject is None
        page = d.get(f"/inbox/desk/t/{got.thread}/")
        chips = page.context["desk_chips"]
        assert [p["local"] for p in chips["personas"]] == ["donotreply", "cnn"] and chips["teams"] == ["Team A"]
        assert any(x.desk_by == "Sent by Anna Desk" for x in page.context["mails"])
        # The team answers the desk's message: matched to it like any delivery.
        pa.post("/inbox/send/", {"to": "p:cnn", "subject": "Re: News", "body": "No comment.", "in_reply_to": team_copy.id})
        r2 = Response.objects.order_by("-id").first()
        assert (r2.message, r2.kind, r2.persona) == (m2, "live", "cnn")
        assert Mail.objects.filter(team=None, desk_response=r2, thread=got.thread).exists()
        # Filters: one persona's conversations, one team's.
        cnn = Sender.objects.get(local_part="cnn")
        assert [r["thread"] for r in d.get(f"/inbox/desk/?folder=a{cnn.id}").context["rows"]] == [got.thread]
        assert len(d.get(f"/inbox/desk/?folder=t{tb.id}").context["rows"]) == 1
    finally:
        push.set_sender(None)
        push.RUN_INLINE = False


def test_desk_writes_to_several_teams_armed_or_not(excon):
    setup(excon)
    d = desk_client()
    ta, tb, test = Team.objects.get(code="a"), Team.objects.get(code="b"), Team.objects.get(code="test")
    conf.put("live_armed", "0")
    r = d.post("/inbox/desk/send/", {"from": "p:cnn", "to": f"t:{ta.id},t:{test.id}", "subject": "Breaking",
                                     "body": "A statement?"}, headers=FETCH)
    j = r.json()
    assert j["ok"] and "not armed" in j["warning"]  # the Test team only: live sending isn't armed
    assert list(Message.objects.filter(kind="desk").values_list("inbox_team__code", flat=True)) == ["test"]
    arm()
    r = d.post("/inbox/desk/send/", {"from": "p:cnn", "to": f"t:{ta.id}", "cc": f"t:{tb.id}", "subject": "Breaking",
                                     "body": "A statement?"}, headers=FETCH)
    assert r.json()["ok"] and not r.json()["warning"]
    assert Mail.objects.filter(owner=None, folder="inbox", team__in=[ta, tb], subject="Breaking").count() == 2
    assert Mail.objects.filter(team=None, folder="sent", subject="Breaking").count() == 3
    # Never as the team leader persona, nor while paused, nor to no team.
    r = d.post("/inbox/desk/send/", {"from": "p:undac-{team}-tl", "to": f"t:{ta.id}", "body": "x"}, headers=FETCH)
    assert r.status_code == 400 and "persona" in r.json()["error"]
    conf.put("paused", "1")
    r = d.post("/inbox/desk/send/", {"from": "p:cnn", "to": f"t:{ta.id}", "body": "x"}, headers=FETCH)
    assert r.status_code == 400 and "paused" in r.json()["error"]


def test_team_leader_persona_stays_off_the_desk(excon):
    setup(excon)
    deliver_now("1-15")  # from the team leader persona
    assert not Mail.objects.filter(team=None).exists()
    pa = participant("a")
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Hi", "body": "For our leader"})
    assert not Mail.objects.filter(team=None).exists()


def test_desk_reads_every_team_read_only(excon):
    setup(excon)
    deliver_now("3-13")
    d, ta = desk_client(), Team.objects.get(code="a")
    leader("a")
    mail = Mail.objects.get(team=ta, owner=None)
    r = d.get(f"/inbox/as/{ta.id}/")
    assert r.status_code == 200 and r.context["desk_tabs"] and r.context["back_url"] == "/inbox/desk/teams/"
    d.get(f"/inbox/as/{ta.id}/t/{mail.thread}/")
    assert Mail.objects.get(pk=mail.pk).read_at is None
    assert d.get(f"/inbox/as/{ta.id}/leader/").status_code == 200
    assert d.post(f"/inbox/as/{ta.id}/send/", {"to": "p:cnn", "body": "x"}).status_code == 403
    page = d.get("/inbox/desk/teams/")
    assert [t.name for t in page.context["teams"]][:1] == ["Team A"] and page.context["teams"][0].lead


def test_desk_answer_skips_the_ai_persona(excon):
    setup(excon)
    conf.put("npc_enabled", "1")
    conf.put("npc_scope", "all")
    NpcProfile.objects.create(sender=Sender.objects.get(local_part="donotreply"), enabled=True, timing="custom",
                              min_minutes=30, max_minutes=30)
    deliver_now("3-13")
    pa = participant("a")
    _, resp = team_writes(pa, Team.objects.get(code="a"))
    job = NpcReply.objects.get()
    assert job.status == "queued"
    d = desk_client()
    box = Mail.objects.get(team=None, folder="inbox")
    page = d.get("/inbox/desk/")
    side = {p["key"]: p["ai"] for p in page.context["desk_side"]["personas"]}
    assert side[f"a{Sender.objects.get(local_part='donotreply').id}"] and not side[f"a{Sender.objects.get(local_part='cnn').id}"]
    d.post("/inbox/desk/send/", {"from": "p:donotreply", "to": f"t:{resp.team_id}", "body": "Human answer.",
                                 "in_reply_to": box.id}, headers=FETCH)
    job.refresh_from_db()
    assert job.status == "skipped" and "Anna Desk" in job.note


def test_desk_by_email(excon):
    setup(excon)
    ta = Team.objects.get(code="a")
    pa = participant("a")
    m0 = Message.objects.create(kind="live", channel="email", idem_key="x1", from_addr='"News" <donotreply@x>',
                                persona="donotreply", team=ta, target="Team A", subject="News", status="sent",
                                to=["real-a@kobotoolbox.org"], sent_at=timezone.now())
    desk.on_delivered(m0)
    r = Response.objects.create(channel="email", from_addr=pa.person.email, from_name="Person A", subject="Re: News",
                                text="Road?\n\nOn Mon, News wrote:\n> old", persona="donotreply", team=ta,
                                person=pa.person, message=m0, fetched=True, match="token")
    box = desk.on_response(r)
    assert box.thread == Mail.objects.get(team=None, message=m0).thread and box.body == "Road?"
    d = desk_client()
    j = d.post("/inbox/desk/send/", {"from": "p:donotreply", "to": f"t:{ta.id}", "body": "Open.",
                                     "in_reply_to": box.id}, headers=FETCH).json()
    assert j["ok"], j
    m = Message.objects.get(kind="reply")
    assert m.channel == "email" and m.status == "sent" and m.to == [pa.person.email]
    assert Mail.objects.get(team=None, message=m).thread == box.thread


def test_presence():
    a, b = desk_client(), desk_client("Ben Desk", "ben.desk")
    Mail.objects.create(team=None, owner=None, folder="inbox", thread="t1", subject="x")
    assert a.post("/inbox/desk/presence/", {"thread": "t1"}).json()["others"] == []
    assert b.post("/inbox/desk/presence/", {"thread": "t1", "replying": "1"}).json()["others"] == [
        {"name": "Anna Desk", "replying": False}]
    assert a.post("/inbox/desk/presence/", {"thread": "t1"}).json()["others"] == [{"name": "Ben Desk", "replying": True}]
    assert a.post("/inbox/desk/presence/", {"thread": "nope"}).status_code == 404


def test_desk_writes_to_one_member(excon):
    setup(excon)
    ta = Team.objects.get(code="a")
    pa = participant("a")
    d = desk_client()
    book = d.get("/inbox/desk/").context["simex_js"]["book"]
    entry = next(b for b in book if b["key"] == f"m:{pa.person.id}")
    assert entry["name"] == "Team A (Person A)" and entry["kind"] == "member"
    j = d.post("/inbox/desk/send/", {"from": "p:cnn", "to": f"m:{pa.person.id}", "subject": "A word",
                                     "body": "Just you: a comment?"}, headers=FETCH).json()
    assert j["ok"], j
    m = Message.objects.get(kind="desk")
    assert (m.inbox_owner, m.person, m.target) == (pa.person, pa.person, "Team A (Person A)")
    got = Mail.objects.get(team=ta, owner=pa.person, folder="inbox", message=m)
    assert got.to_label == "Team A (Person A)"
    assert not Mail.objects.filter(team=ta, owner=None, message=m).exists()  # not the team's inbox
    assert Mail.objects.filter(team=None, folder="sent", message=m, desk_team=ta).exists()
    # They answer from their own mailbox, as "Team A (Person A)"; the desk has it and answers in kind.
    r = pa.post("/inbox/me/send/", {"to": "p:cnn", "from": "me", "subject": "Re: A word", "body": "No comment.",
                                    "in_reply_to": got.id})
    assert r.status_code == 302
    resp = Response.objects.get(persona="cnn")
    assert (resp.person, resp.message) == (pa.person, m)
    box = Mail.objects.get(team=None, folder="inbox", desk_response=resp)
    assert box.from_name == "Team A (Person A)" and box.thread == got.thread
    j = d.post("/inbox/desk/send/", {"from": "p:cnn", "to": f"m:{pa.person.id}", "subject": "Re: A word",
                                     "body": "Understood.", "in_reply_to": box.id}, headers=FETCH).json()
    assert j["ok"], j
    ans = Message.objects.get(kind="reply")
    assert ans.response == resp and ans.inbox_owner == pa.person
    assert Mail.objects.filter(team=ta, owner=pa.person, folder="inbox", message=ans, thread=got.thread).exists()
    assert not Mail.objects.filter(team=ta, owner=None, folder="inbox", message=ans).exists()


def test_desk_emails_one_member(excon):
    setup(excon)
    conf.put("channel_mode", "email")
    pa = participant("a")
    participant("a", "other")
    d = desk_client()
    book = d.get("/inbox/desk/").context["simex_js"]["book"]
    assert next(b for b in book if b["key"] == f"m:{pa.person.id}")["addr"] == pa.person.email
    j = d.post("/inbox/desk/send/", {"from": "p:cnn", "to": f"m:{pa.person.id}", "subject": "A word",
                                     "body": "Just you."}, headers=FETCH).json()
    assert j["ok"], j
    m = Message.objects.get(kind="desk")
    assert m.channel == "email" and m.to == [pa.person.email] and m.person == pa.person

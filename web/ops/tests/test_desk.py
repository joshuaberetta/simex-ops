"""The persona desk (ops/desk.py): ExCon role-players sharing one SIMEX Mail mailbox where the
teams' mail to the personas arrives, writing as any persona but the team leader one, reading
every team's mailbox read-only, with no access to the ExCon screens."""

import re

from django.conf import settings
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
        # Each row opens in a new tab too, so the desk works several conversations beside the list.
        assert f'href="/inbox/desk/t/{got.thread}/?folder=inbox" target="_blank" rel="noopener"' in page.content.decode()
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
    assert f'href="/inbox/as/{ta.id}/t/{mail.thread}/?folder=inbox" target="_blank"' in r.content.decode()  # read-only: still a new tab
    assert 'data-row-action' not in r.content.decode()
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


def test_excon_compose_tab(excon):
    setup(excon)
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    conf.put("personal_mailboxes", "1")
    member = Person.objects.participants().filter(team=tb, active=True).first()
    page = excon.get("/compose/")
    assert page.status_code == 200 and page.context["tab"] == "compose"
    assert "p:undac-{team}-tl" not in [p["key"] for p in page.context["personas"]]
    group = next(g for g in page.context["by_team"] if g["team"] == tb)
    assert f"m:{member.id}" in [e["key"] for e in group["members"]]
    r = excon.post("/compose/", {"from": "p:cnn", "to": [f"t:{ta.id}", f"m:{member.id}"], "subject": "Breaking",
                                 "body": "A statement?"})
    assert r.status_code == 302 and r.url == "/compose/"
    msgs = Message.objects.filter(kind="desk")
    assert msgs.count() == 2 and all(m.author is None and m.inject is None and m.status == "sent" for m in msgs)
    assert Mail.objects.filter(team=ta, owner=None, folder="inbox", subject="Breaking").exists()
    assert Mail.objects.filter(team=tb, owner=member, folder="inbox", subject="Breaking").exists()
    assert not Mail.objects.filter(team=tb, owner=None, folder="inbox", subject="Breaking").exists()
    assert Mail.objects.filter(team=None, folder="sent", subject="Breaking").count() == 2
    assert len(excon.get("/compose/").context["recent"]) == 2
    # Never as the team leader persona, with no recipient, or while paused; and ExCon only.
    for data in ({"from": "p:undac-{team}-tl", "to": f"t:{ta.id}", "body": "x"}, {"from": "p:cnn", "body": "x"}):
        r = excon.post("/compose/", data)
        assert r.status_code == 200 and Message.objects.filter(kind="desk").count() == 2
    conf.put("paused", "1")
    excon.post("/compose/", {"from": "p:cnn", "to": f"t:{ta.id}", "body": "x"})
    assert Message.objects.filter(kind="desk").count() == 2
    assert desk_client().get("/compose/").status_code == 403 and participant("a").get("/compose/").status_code == 403


def test_compose_and_desk_write_to_team_leaders(excon):
    setup(excon)
    ta = Team.objects.get(code="a")
    lead = Person.objects.get(team=ta, role="tl")
    key = f"l:{lead.id}"
    group = next(g for g in excon.get("/compose/").context["by_team"] if g["team"] == ta)
    entry = next(e for e in group["leaders"] if e["key"] == key)
    assert entry["name"] == f"Team A leader ({lead.name})" and entry["addr"].startswith("undac-a-tl")
    r = excon.post("/compose/", {"from": "p:cnn", "to": key, "subject": "For the TL", "body": "Only you."})
    assert r.status_code == 302
    m = Message.objects.get(kind="desk")
    assert m.status == "sent" and m.inbox_owner == lead and m.person == lead
    assert Mail.objects.filter(team=ta, owner=lead, folder="inbox", subject="For the TL").exists()
    assert not Mail.objects.filter(team=ta, owner=None, subject="For the TL").exists()  # not the team's
    assert Mail.objects.filter(team=None, folder="sent", subject="For the TL", desk_team=ta).exists()
    la = leader("a")
    assert "For the TL" in la.get("/inbox/").content.decode()
    # The persona desk's book (and so its typing completion) lists every leader and member.
    d = desk_client()
    book = d.get("/inbox/desk/").context["simex_js"]["book"]
    keys = {b["key"] for b in book}
    assert key in keys and next(b for b in book if b["key"] == key)["kind"] == "tl"
    assert all(f"m:{p.id}" in keys for p in Person.objects.participants().filter(active=True, team__active=True))
    j = d.post("/inbox/desk/send/", {"from": "p:cnn", "to": key, "subject": "Again", "body": "Hi."},
               headers=FETCH).json()
    assert j["ok"], j
    assert Mail.objects.filter(team=ta, owner=lead, folder="inbox", subject="Again").exists()
    # Not a leader any more: no longer in the book, and a stale key goes nowhere.
    lead.active = False
    lead.save()
    assert key not in {b["key"] for b in d.get("/inbox/desk/").context["simex_js"]["book"]}


def test_withdraw_a_delivery_sent_by_mistake(excon):
    from ops import delivery
    from ops.tests.test_ops import deliver_now
    setup(excon)
    conf.put("personal_mailboxes", "1")
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    pa = participant("a")
    lead = Person.objects.get(team=ta, role="tl")
    from ops.models import Inject
    Inject.objects.filter(mel_id="3-13").update(to_leaders=True)
    deliver_now("3-13")
    m = Message.objects.get(kind="live", inbox_team=ta, inbox_owner=None)
    assert Mail.objects.filter(team=ta, owner=pa.person, message=m).exists()  # the member's copy
    assert Mail.objects.filter(team=ta, owner=lead, folder="inbox").exists()  # the leader's (its own Message)
    assert Mail.objects.filter(team=None, desk_team=ta, message=m).exists()  # the desk's
    got, r = team_writes(pa, ta)  # Team A already answered it
    page = excon.get(f"/log/{m.id}/")
    assert [c["team"] for c in page.context["copies"]] == [ta]
    assert excon.post(f"/log/{m.id}/withdraw/", {"team": ta.id, "confirm": "no"}).status_code == 302
    assert Mail.objects.filter(message=m).exists()  # not confirmed: nothing goes
    excon.post(f"/log/{m.id}/withdraw/", {"team": ta.id, "confirm": "withdraw"})
    assert not Mail.objects.filter(message=m).exists()
    assert not Mail.objects.filter(team=ta, owner=lead, subject="News").exists()  # the leader's copy went too
    m.refresh_from_db()
    assert m.status == "withdrawn" and Message.objects.filter(inbox_owner=lead).get().status == "withdrawn"
    assert Mail.objects.filter(team=tb, owner=None, folder="inbox", subject="News").exists()  # other teams keep it
    r.refresh_from_db()
    assert r.mail is not None and Mail.objects.filter(pk=r.mail_id).exists()  # the team's reply stays
    assert pa.get("/inbox/").status_code == 200 and pa.get(f"/inbox/t/{r.mail.thread}/").status_code == 200
    assert delivery.withdraw(m, [ta.id]) == 0
    assert "Withdrawn from SIMEX Mail" in excon.get(f"/log/{m.id}/").content.decode()


def test_desk_has_no_default_persona(excon):
    """New desk mail starts with no From: the desk picks the persona every time."""
    setup(excon)
    dc = desk_client()
    r = dc.get("/inbox/desk/")
    assert r.context["from_default"] == ""
    html = r.content.decode()
    assert '<option value="">Choose the persona it is from…</option>' in html and "data-from required" in html
    assert "selected data-colour" not in html  # no persona preselected
    team = Team.objects.get(code="a")
    r = dc.post("/inbox/desk/send/", {"to": f"t:{team.id}", "subject": "Hello", "body": "Hi"}, headers=FETCH)
    assert r.json()["error"] == "Choose the persona it goes from."
    assert not Message.objects.filter(kind="desk").exists()


def test_desk_copies_other_personas(excon):
    """A desk message can be addressed to other personas too (To or Cc, beside a team): the team
    sees them on it and reply all reaches them; they get no mail of their own."""
    setup(excon)
    ta = Team.objects.get(code="a")
    d = desk_client()
    keys = {b["key"] for b in d.get("/inbox/desk/").context["simex_js"]["book"]}
    assert {"p:donotreply", "p:cnn"} <= keys and "p:undac-{team}-tl" not in keys
    r = d.post("/inbox/desk/send/", {"from": "p:cnn", "to": f"t:{ta.id},p:donotreply", "cc": "p:cnn",
                                     "subject": "Breaking", "body": "A statement?"}, headers=FETCH)
    assert r.json()["ok"], r.json()
    m = Message.objects.get(kind="desk")
    got = Mail.objects.get(team=ta, owner=None, folder="inbox", message=m)
    assert "<donotreply@" in got.to_label and got.cc_label == ""  # the persona writing isn't copied to itself
    assert got.recipients == {"to": ["p:donotreply"], "cc": []}
    assert "<donotreply@" in Mail.objects.get(team=None, folder="sent", message=m).to_label
    # persona mail: the desk's Inbox has it for donotreply (no Sent copy of its own: the team's is it),
    # in the team's conversation, and nobody waits on it
    pp = Mail.objects.get(team=None, folder="inbox")
    assert pp.persona == "donotreply" and pp.message.kind == "persona" and pp.thread == got.thread
    assert pp.recipients["from"] == "p:cnn" and f"t:{ta.id}" in pp.recipients["cc"]
    assert not desk.states([pp.thread])[pp.thread]["waiting"]
    pp.delete()
    # Cc, then the team's reply all reaches both personas: one desk copy.
    d.post("/inbox/desk/send/", {"from": "p:cnn", "to": f"t:{ta.id}", "cc": "p:donotreply", "subject": "Again",
                                 "body": "?"}, headers=FETCH)
    got = Mail.objects.get(team=ta, owner=None, folder="inbox", subject="Again")
    assert "<donotreply@" in got.cc_label
    pa = participant("a")
    t = pa.get(f"/inbox/t/{got.thread}/").context["thread_js"]
    assert set(t["replyAll"]) == {"p:cnn", "p:donotreply"}
    pa.post("/inbox/send/", {"to": ",".join(t["replyAll"]), "subject": "Re: Again", "body": "Both.", "in_reply_to": got.id})
    assert Mail.objects.filter(team=None, folder="inbox", subject="Re: Again").count() == 1
    # Personas alone: persona mail, on the desk only.
    r = d.post("/inbox/desk/send/", {"from": "p:cnn", "to": "p:donotreply", "subject": "Alone", "body": "x"}, headers=FETCH)
    assert r.json()["ok"], r.json()
    assert Mail.objects.filter(team=None, subject="Alone").count() == 2 and not Mail.objects.filter(team__isnull=False, subject="Alone").exists()
    # ExCon's Compose: Cc other personas.
    excon.post("/compose/", {"from": "p:cnn", "to": f"t:{ta.id}", "cc": "p:donotreply", "subject": "Compose",
                             "body": "x"})
    assert "<donotreply@" in Mail.objects.get(team=ta, owner=None, folder="inbox", subject="Compose").cc_label


def test_desk_email_copies_other_personas(excon):
    setup(excon)
    conf.put("channel_mode", "email")
    ta = Team.objects.get(code="a")
    ta.email = "team-a@kobotoolbox.org"
    ta.save()
    d = desk_client()
    j = d.post("/inbox/desk/send/", {"from": "p:cnn", "to": f"t:{ta.id}", "cc": "p:donotreply", "subject": "Breaking",
                                     "body": "A statement?"}, headers=FETCH).json()
    assert j["ok"], j
    m = Message.objects.get(kind="desk")
    assert m.channel == "email" and m.to == ["team-a@kobotoolbox.org"] and m.cc == [f"donotreply@{settings.MAIL_DOMAIN}"]



def test_desk_all_teams_sends_each_team_its_own(excon):
    """The composer's "All teams" (desk_groups): a chip for every team but the Test team, and
    one message each, none naming another team."""
    import json
    setup(excon)
    d = desk_client()
    teams = list(Team.objects.filter(active=True, is_test=False).order_by("name"))
    cfg = json.loads(re.search(r'id="simex-config"[^>]*>(.*?)</script>', d.get("/inbox/desk/").content.decode(), re.S)[1])
    group = cfg["book"][0]
    assert group["key"] == "g:teams" and group["members"] == [f"t:{t.id}" for t in teams]
    assert "g:teams" not in json.dumps(participant("a").get("/inbox/").context["simex_js"]["book"])
    r = d.post("/inbox/desk/send/", {"from": "p:cnn", "to": ",".join(group["members"]), "subject": "Field visit",
                                     "body": "Hi Team, I hope it went well."}, headers=FETCH)
    assert r.json()["ok"] and not r.json()["warning"]
    got = Mail.objects.filter(owner=None, folder="inbox", team__in=teams, subject="Field visit")
    assert sorted(m.team_id for m in got) == sorted(t.id for t in teams)
    for m in got:
        others = [t.name for t in teams if t.id != m.team_id]
        assert not any(n in (m.to_label or "") + (m.cc_label or "") for n in others)
    assert Message.objects.filter(kind="desk", subject="Field visit").count() == len(teams)

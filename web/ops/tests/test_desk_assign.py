"""The persona desk's conversations by who has them (Assign to, Assigned to me, Following, the
Dashboard's breakdown) and each person's own notices (DeskNotice, the bell)."""

import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from ops import desk, desk_stats, push
from ops.models import DeskConversation, DeskNotice, DeskWatch, Mail, PushSubscription, Team
from ops.tests.test_desk import FETCH, desk_client, setup, team_writes
from ops.tests.test_desk_queue import answer, rows
from ops.tests.test_ops import deliver_now, participant


def again(pa, team, body="And tomorrow?"):
    """The team writes again in its conversation with donotreply."""
    copy = Mail.objects.filter(team=team, owner=None, folder="inbox").order_by("-id").first()
    assert pa.post("/inbox/send/", {"to": "p:donotreply", "subject": "Re: News", "body": body,
                                    "in_reply_to": copy.id}).status_code == 302


def titles(c):
    return [n.title for n in DeskNotice.objects.filter(user=c.person.user).order_by("id")]


def start():
    deliver_now("3-13")
    ta = Team.objects.get(code="a")
    pa = participant("a")
    got, _ = team_writes(pa, ta)
    return ta, pa, got, desk_client(), desk_client("Ben Desk", "ben.desk")


def test_assign_to_someone_on_the_desk(excon):
    setup(excon)
    ta, pa, got, d, e = start()
    ben = e.person.user
    assert {a["name"] for a in desk.assignees()} >= {"Anna Desk", "Ben Desk"}
    page = d.get(f"/inbox/desk/t/{got.thread}/")
    assert b"Assign to" in page.content and page.context["desk_assignees"]
    r = d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "assign", "assignee": ben.id,
                                     "next": f"/inbox/desk/t/{got.thread}/"})
    assert r.status_code == 302
    c = DeskConversation.objects.get(thread=got.thread)
    assert (c.owner, c.owner_name, c.assigned_by) == (ben, "Ben Desk", "Anna Desk")
    assert titles(e) == ["Anna Desk gave you a conversation"] and titles(d) == []
    st = rows(e)[1][got.thread]
    assert st["mine"] and st["assigned_by"] == "Anna Desk"
    assert b"Anna Desk gave it to you" in e.get(f"/inbox/desk/t/{got.thread}/").content
    # Assigned to me: Ben's, not Anna's; the side bar counts the one needing attention.
    page, st = rows(e, "mine")
    assert list(st) == [got.thread] and page.context["folder_name"] == "Assigned to me"
    assert page.context["desk_side"]["mine"]["mine"] == {"n": 1, "attention": 1}
    assert page.context["sections"]["attention"]["n"] == 1
    assert rows(d, "mine")[1] == {} and rows(d, f"o{ben.id}")[1].keys() == {got.thread}
    assert got.thread not in rows(d, "o0")[1]  # nobody's: the rest (the inject to the other teams)
    # Taken over: Ben hears of it, once.
    d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "take"}, headers=FETCH)
    d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "take"}, headers=FETCH)
    assert titles(e) == ["Anna Desk gave you a conversation", "Anna Desk took it over"]
    assert DeskConversation.objects.get(thread=got.thread).assigned_by == ""
    # Nobody to give it to.
    r = d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "assign", "assignee": "999"}, headers=FETCH)
    assert r.status_code == 400
    assert d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "assign", "next": "/inbox/desk/"}).status_code == 302
    # ExCon staff can be given one too.
    staff = get_user_model().objects.get(username="undac")
    d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "assign", "assignee": staff.id}, headers=FETCH)
    assert DeskConversation.objects.get(thread=got.thread).owner == staff
    assert DeskNotice.objects.filter(user=staff, kind="assign").count() == 1
    d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "release"}, headers=FETCH)
    assert got.thread in rows(d, "o0")[1]


def test_who_hears_what_happens_in_a_conversation(excon):
    setup(excon)
    ta, pa, got, d, e = start()
    box = Mail.objects.get(team=None, folder="inbox")
    answer(d, box, ta)  # Anna answers: involved from now on, and not told of her own answer
    assert DeskWatch.objects.filter(thread=got.thread, user=d.person.user, reason="answered").exists()
    assert titles(d) == []
    assert rows(d, "watching")[1].keys() == {got.thread}
    again(pa, ta)  # nobody has it: everyone involved hears
    assert titles(d) == ["Team A wrote: Re: News"] and titles(e) == []
    n = DeskNotice.objects.get(user=d.person.user)
    assert n.url == f"/inbox/desk/t/{got.thread}/" and "And tomorrow?" in n.body and n.kind == "reply"
    # Ben takes it: now only he hears, unless Anna follows it on purpose.
    e.post("/inbox/desk/mark/", {"threads": got.thread, "act": "take"}, headers=FETCH)
    again(pa, ta, "Any news?")
    assert titles(d) == ["Team A wrote: Re: News"] and titles(e) == ["Team A wrote: Re: News"]
    d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "watch"}, headers=FETCH)
    assert b"Unfollow" in d.get(f"/inbox/desk/t/{got.thread}/").content
    again(pa, ta, "Hello?")
    assert len(titles(d)) == 2 and len(titles(e)) == 2
    # Ben answers: Anna hears, Ben doesn't.
    answer(e, Mail.objects.filter(team=None, folder="inbox").order_by("-id").first(), ta, "Tomorrow at 9.")
    assert titles(d)[-1].startswith("Ben Desk answered Team A as") and len(titles(e)) == 2
    # Anna stops following: nothing more for her, even after answering again.
    d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "unwatch"}, headers=FETCH)
    answer(d, Mail.objects.filter(team=None, folder="inbox").order_by("-id").first(), ta, "Also at 10.")
    assert DeskWatch.objects.get(thread=got.thread, user=d.person.user).off
    again(pa, ta, "Thanks")
    assert len(titles(d)) == 3 and len(titles(e)) == 4  # Ben: told of Anna's answer and the team's message
    assert rows(d, "watching")[1] == {}


def test_follow_up_due_is_told_once(excon, django_capture_on_commit_callbacks):
    setup(excon)
    ta, pa, got, d, e = start()
    pushed = []
    push.set_sender(lambda info, data: pushed.append(data) or 201)
    push.RUN_INLINE = True
    try:
        PushSubscription.objects.create(person=d.person, endpoint="https://fcm.googleapis.com/x", p256dh="k", auth="a")
        d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "follow", "minutes": "30", "note": "Convoy ETA"})
        assert rows(d, "watching")[1].keys() == {got.thread}
        assert desk.tick_follow_ups() == 0
        later = timezone.now() + timedelta(minutes=31)
        with django_capture_on_commit_callbacks(execute=True):
            assert desk.tick_follow_ups(later) == 1
        assert desk.tick_follow_ups(later) == 0
        n = DeskNotice.objects.get(user=d.person.user, kind="follow_up")
        assert n.title == "Follow up: Team A: News" and n.body == "Convoy ETA"
        assert pushed and json.loads(pushed[-1])["title"] == n.title
        # Changed to later again: told again when that comes.
        d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "follow", "minutes": "60"})
        desk.tick(timezone.now() + timedelta(minutes=61))
        assert DeskNotice.objects.filter(user=d.person.user, kind="follow_up").count() == 2
        # "Follow up now" is no news to whoever set it.
        d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "follow", "minutes": "0"})
        assert desk.tick_follow_ups(timezone.now() + timedelta(hours=3)) == 0
    finally:
        push.set_sender(None)
        push.RUN_INLINE = False


def test_owner_hears_of_a_team_waiting_too_long(excon):
    setup(excon)
    ta, pa, got, d, e = start()
    e.post("/inbox/desk/mark/", {"threads": got.thread, "act": "take"}, headers=FETCH)
    Mail.objects.filter(team=None, folder="inbox").update(created_at=timezone.now() - timedelta(minutes=25))
    desk.tick()
    desk.tick()
    assert titles(e) == ["Team A has waited 20 min"] and titles(d) == []


def test_the_bell(excon):
    setup(excon)
    ta, pa, got, d, e = start()
    d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "assign", "assignee": e.person.user.id}, headers=FETCH)
    page = e.get("/inbox/desk/")
    assert b"data-dn" in page.content and page.context["desk_tabs"]["bell"]["unread"] == 1
    assert e.get("/inbox/desk/dashboard/").context["desk_tabs"]["bell"]["unread"] == 1
    assert e.get("/inbox/desk/teams/").context["desk_tabs"]["bell"]["unread"] == 1
    j = e.get("/inbox/desk/poll/", headers=FETCH).json()
    assert j["dn"]["unread"] == 1 and j["dn"]["latest"] > 0
    assert d.get("/inbox/desk/poll/", headers=FETCH).json()["dn"] == {"unread": 0, "latest": 0}
    items = e.get("/inbox/desk/notices/").json()["items"]
    assert [i["title"] for i in items] == ["Anna Desk gave you a conversation"] and not items[0]["read"]
    assert d.get("/inbox/desk/notices/").json()["items"] == []  # one's own only
    r = d.post("/inbox/desk/notices/mark/", {"act": "dismiss", "ids": items[0]["id"]}, headers=FETCH)
    assert r.json()["unread"] == 0 and DeskNotice.objects.get().dismissed_at is None  # not Anna's to dismiss
    assert e.post("/inbox/desk/notices/mark/", {"act": "read", "ids": items[0]["id"]}, headers=FETCH).json()["unread"] == 0
    assert e.get("/inbox/desk/notices/").json()["items"][0]["read"]
    e.post("/inbox/desk/notices/mark/", {"act": "dismiss", "all": "1"}, headers=FETCH)
    assert e.get("/inbox/desk/notices/").json()["items"] == []
    assert participant("b").get("/inbox/desk/notices/").status_code == 403
    assert excon.get("/inbox/desk/notices/").status_code == 200


def test_dashboard_by_who_has_them(excon):
    setup(excon)
    deliver_now("3-13")
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    got_a, _ = team_writes(participant("a"), ta, "Where is the convoy?")
    got_b, _ = team_writes(participant("b"), tb, "Road status?")
    team_writes(participant("c"), Team.objects.get(code="c"), "Anyone?")
    d, e = desk_client(), desk_client("Ben Desk", "ben.desk")
    desk_client("Cleo Desk", "cleo.desk")
    d.post("/inbox/desk/mark/", {"threads": [got_a.thread, got_b.thread], "act": "assign", "assignee": e.person.user.id})
    answer(e, Mail.objects.get(team=None, folder="inbox", desk_team=tb), tb)
    s = desk_stats.compute("live")
    by = {r["name"]: r for r in s["assigned"]}
    ben = by["Ben Desk"]
    assert (ben["n"], ben["attention"], ben["waiting"], ben["answered"]) == (2, 1, 1, 1)
    assert ben["convs"][0]["thread"] == got_a.thread and ben["convs"][0]["team"] == ta
    assert ben["convs"][0]["assigned_by"] == "Anna Desk"
    assert by["Cleo Desk"]["n"] == 0 and by["Anna Desk"]["n"] == 0  # everyone on the desk, with none too
    assert s["assigned"][0]["name"] == "Ben Desk" and s["assigned"][-1]["uid"] == 0
    assert s["assigned"][-1]["n"] == 1 and s["assigned"][-1]["attention"] == 1  # Team C's: nobody has it
    page = d.get("/inbox/desk/dashboard/")
    assert b"Who has which conversations" in page.content and f"?folder=o{e.person.user.id}".encode() in page.content


def test_cleared_with_the_conversations(excon):
    setup(excon)
    ta, pa, got, d, e = start()
    d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "assign", "assignee": e.person.user.id})
    d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "watch"})
    assert DeskWatch.objects.count() == 2 and DeskNotice.objects.count() == 1
    from ops import reset
    reset.clear_team(ta)
    assert not DeskWatch.objects.exists() and not DeskNotice.objects.exists()
    desk.watch("x", d.person.user, "follow")
    desk.notify([d.person.user], "k", "assign", "A title")
    reset.start_afresh()
    assert not DeskWatch.objects.exists() and not DeskNotice.objects.exists()

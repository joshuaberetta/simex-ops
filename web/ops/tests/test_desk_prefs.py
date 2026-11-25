"""The persona desk's gear: each login's own settings (DeskPref). Only my conversations: the
bell, the pushes and the page's new-mail alerts only for conversations they have or follow, and
anything given to them."""

import json

from ops import desk, push
from ops.models import DeskNotice, DeskPref, Mail, PushSubscription
from ops.tests.test_desk import FETCH, setup
from ops.tests.test_desk_assign import again, start, titles
from ops.tests.test_desk_queue import answer


def subscribe(c, n):
    PushSubscription.objects.create(person=c.person, endpoint=f"https://fcm.googleapis.com/{n}", p256dh="k", auth="a")


def test_the_gear_saves_ones_own_settings(excon):
    setup(excon)
    ta, pa, got, d, e = start()
    page = e.get("/inbox/desk/")
    assert b"data-dp" in page.content and not page.context["desk_tabs"]["prefs"].only_mine
    r = e.post("/inbox/desk/prefs/", {"only_mine": "1"}, headers=FETCH)
    assert r.json() == {"ok": True, "only_mine": True}
    assert DeskPref.objects.get(user=e.person.user).only_mine and not desk.prefs_of(d.person.user).only_mine
    assert e.get("/inbox/desk/dashboard/").context["desk_tabs"]["prefs"].only_mine
    assert b"data-dp-only-mine checked" in e.get("/inbox/desk/").content
    assert e.post("/inbox/desk/prefs/", {}, headers=FETCH).json()["only_mine"] is False
    assert excon.post("/inbox/desk/prefs/", {"only_mine": "1"}, headers=FETCH).json()["only_mine"]  # ExCon on the desk too
    from ops.tests.test_ops import participant
    assert participant("b").post("/inbox/desk/prefs/", {"only_mine": "1"}).status_code == 403


def test_only_my_conversations(excon, django_capture_on_commit_callbacks):
    setup(excon)
    ta, pa, got, d, e = start()
    anna, ben = d.person.user, e.person.user
    pushed = []
    push.set_sender(lambda info, data: pushed.append((info["endpoint"], json.loads(data))) or 201)
    push.RUN_INLINE = True
    try:
        subscribe(d, "anna")
        subscribe(e, "ben")
        e.post("/inbox/desk/prefs/", {"only_mine": "1"}, headers=FETCH)
        box = Mail.objects.get(team=None, folder="inbox")
        answer(d, box, ta)
        answer(e, Mail.objects.get(team=None, folder="inbox"), ta, "Later.")  # both involved
        e.post("/inbox/desk/mark/", {"threads": got.thread, "act": "unwatch"}, headers=FETCH)  # Ben stops following
        # A new message in a conversation nobody has and Ben doesn't follow: Anna hears, Ben doesn't.
        pushed.clear()
        with django_capture_on_commit_callbacks(execute=True):
            again(pa, ta)
        assert {ep for ep, _ in pushed} == {"https://fcm.googleapis.com/anna"}
        assert titles(d)[-1] == "Team A wrote: Re: News" and titles(e) == []
        assert e.get("/inbox/desk/poll/?since=0", headers=FETCH).json()["new"] == []
        assert d.get("/inbox/desk/poll/?since=0", headers=FETCH).json()["new"]
        # Given to Ben: he hears of that (it is about him), and from then on of what happens in it.
        d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "assign", "assignee": ben.id}, headers=FETCH)
        assert titles(e) == ["Anna Desk gave you a conversation"]  # (not told of Anna's answers before)
        pushed.clear()
        with django_capture_on_commit_callbacks(execute=True):
            again(pa, ta, "Any news?")
        assert "https://fcm.googleapis.com/ben" in {ep for ep, _ in pushed}
        assert titles(e)[-1] == "Team A wrote: Re: News"
        assert e.get("/inbox/desk/poll/?since=0", headers=FETCH).json()["new"]
        # Anything else on the desk, e.g. a persona's meeting nobody has: not for Ben.
        assert desk.notify([anna, ben], "cal-x:1", "calendar", "Team A invites CNN", thread="") == 1
        assert not DeskNotice.objects.filter(user=ben, key="cal-x:1").exists()
        # Turned off again: everything, as before.
        e.post("/inbox/desk/prefs/", {"only_mine": "0"}, headers=FETCH)
        assert desk.notify([ben], "cal-x:2", "calendar", "Team A invites CNN") == 1
    finally:
        push.set_sender(None)
        push.RUN_INLINE = False

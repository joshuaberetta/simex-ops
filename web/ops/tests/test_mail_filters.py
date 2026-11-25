"""Filter views of SIMEX Mail lists (ops/mail_filters.py): quick filters in every mailbox, more
on the persona desk; carried through paging, search and the thread view's neighbours."""

from datetime import timedelta

from ops.models import DeskConversation, Mail, MailAction, Sender, Team
from ops.tests.test_desk import FETCH, desk_client, setup, team_writes
from ops.tests.test_ops import deliver_now, participant


def threads(page):
    return [r["thread"] for r in page.context["rows"]]


def test_unread_and_other_quick_filters_in_a_team_inbox(excon):
    setup(excon)
    deliver_now("3-13")
    ta = Team.objects.get(code="a")
    pa = participant("a")
    got = Mail.objects.get(team=ta, owner=None, folder="inbox")
    page = pa.get("/inbox/?folder=inbox")
    assert threads(page) == [got.thread]
    chips = {c["key"]: c for c in page.context["filters"]["chips"]}
    assert set(chips) == {"unread", "starred", "files", "unanswered"} and chips["unread"]["n"] == 1
    assert b'class="gm-filterbar"' in page.content and page.context["filters"]["desk"] is None  # no desk panel
    assert threads(pa.get("/inbox/?folder=inbox&f=unread")) == [got.thread]
    assert threads(pa.get("/inbox/?folder=inbox&f=starred")) == []
    assert threads(pa.get("/inbox/?folder=inbox&f=unanswered")) == [got.thread]
    # Read it: out of Unread; the filter is kept on the row links and the thread's back link.
    t = pa.get(f"/inbox/t/{got.thread}/?folder=inbox&f=unread")
    assert t.status_code == 200 and t.context["fq"] == "&f=unread"
    assert f'/inbox/?folder=inbox&amp;f=unread" title="Back'.encode() in t.content
    page = pa.get("/inbox/?folder=inbox&f=unread")
    assert threads(page) == [] and b"No conversations match these filters" in page.content
    # Answered: out of Not replied. Unknown filter values are ignored.
    team_writes(pa, ta)
    assert threads(pa.get("/inbox/?folder=inbox&f=unanswered")) == []
    assert threads(pa.get("/inbox/?folder=inbox&f=bogus")) == [got.thread]
    # A participant's filter is a MailAction, once per change.
    pa.get("/inbox/?folder=inbox&f=starred")
    pa.get("/inbox/?folder=inbox&f=starred")
    acts = MailAction.objects.filter(action="filter", person=pa.person)
    assert acts.count() == 6 and acts.last().detail["filters"] == {"f": ["starred"]}
    # The search box keeps the filter on.
    page = pa.get("/inbox/?folder=inbox&f=unanswered")
    assert b'<input type="hidden" name="f" value="unanswered">' in page.content


def test_filters_never_cross_teams(excon):
    setup(excon)
    deliver_now("3-13")
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    pa = participant("a")
    page = pa.get(f"/inbox/?folder=inbox&f=unread&t={tb.id}&p=1&st=attention")  # desk-only ones ignored here
    assert threads(page) == [Mail.objects.get(team=ta, owner=None, folder="inbox").thread]
    assert page.context["fq"] == "&f=unread"


def test_the_desk_filters(excon):
    setup(excon)
    deliver_now("3-13")
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    pa, pb = participant("a"), participant("b")
    got_a, _ = team_writes(pa, ta, "Where is the convoy?")
    got_b, _ = team_writes(pb, tb, "Thanks.")
    d = desk_client()
    page = d.get("/inbox/desk/?folder=inbox")
    bar = page.context["filters"]["desk"]
    assert bar and {p["name"] for p in bar["presets"]} >= {"Needs attention", "Mine", "Nobody has it"}
    assert b"More filters" in page.content and b'id="gm-filter"' in page.content
    both = set(threads(page))
    assert {got_a.thread, got_b.thread} <= both
    # By team.
    assert threads(d.get(f"/inbox/desk/?folder=inbox&t={ta.id}")) == [got_a.thread]
    assert set(threads(d.get(f"/inbox/desk/?folder=inbox&t={ta.id}&t={tb.id}"))) == {got_a.thread, got_b.thread}
    # By persona (a persona the desk hasn't heard from: nothing).
    donot = Sender.objects.get(local_part="donotreply")
    assert set(threads(d.get(f"/inbox/desk/?folder=inbox&p={donot.id}"))) == {got_a.thread, got_b.thread}
    cnn = Sender.objects.get(local_part="cnn")
    assert threads(d.get(f"/inbox/desk/?folder=inbox&p={cnn.id}")) == []
    # Where it stands, and who has it.
    assert set(threads(d.get("/inbox/desk/?folder=inbox&st=attention"))) == {got_a.thread, got_b.thread}
    d.post("/inbox/desk/mark/", {"threads": got_a.thread, "act": "take"}, headers=FETCH)
    assert threads(d.get("/inbox/desk/?folder=inbox&own=me")) == [got_a.thread]
    assert threads(d.get("/inbox/desk/?folder=inbox&own=none&st=attention")) == [got_b.thread]
    assert threads(d.get(f"/inbox/desk/?folder=inbox&own={d.person.user_id}")) == [got_a.thread]
    d.post("/inbox/desk/mark/", {"threads": got_b.thread, "act": "done"})
    assert threads(d.get("/inbox/desk/?folder=inbox&st=done")) == [got_b.thread]
    assert threads(d.get("/inbox/desk/?folder=inbox&st=attention")) == [got_a.thread]
    # Waiting longer than: none yet; then an hour ago.
    assert threads(d.get("/inbox/desk/?folder=inbox&wait=30")) == []
    Mail.objects.filter(team=None).update(created_at=Mail.objects.get(pk=got_a.pk).created_at - timedelta(hours=2))
    assert threads(d.get("/inbox/desk/?folder=inbox&wait=60")) == [got_a.thread]
    assert threads(d.get("/inbox/desk/?folder=inbox&recent=15")) == []
    # Active filters are removable tags; the thread's neighbours follow the filter.
    page = d.get(f"/inbox/desk/?folder=inbox&t={ta.id}&kind=live")
    assert [t["name"] for t in page.context["filters"]["desk"]["tags"]] == [ta.name, "Live teams only"]
    t = d.get(f"/inbox/desk/t/{got_a.thread}/?folder=inbox&t={ta.id}")
    assert t.status_code == 200 and not t.context["next_t"] and not t.context["prev_t"]
    assert not MailAction.objects.filter(action="filter").exists()  # the desk's reading is never tracked
    assert DeskConversation.objects.filter(thread=got_a.thread, owner=d.person.user).exists()

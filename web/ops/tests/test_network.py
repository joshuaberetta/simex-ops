"""Dashboard → Network (ops/network.py): who writes to whom, worked out from the Messages, the
participants' Sent mail and email replies, and the page's data and poll (ExCon only)."""

from django.test import Client

from ops import conf, delivery, network
from ops.models import Inject, Message, Person, Team
from ops.tests.test_leaders import leader, setup_exercise
from ops.tests.test_ops import deliver_now, participant

AJAX = {"headers": {"x-requested-with": "fetch"}}


def send(c, base="/inbox", **data):
    r = c.post(f"{base}/send/", data, **AJAX)
    assert r.status_code == 200, r.content
    return r.json()


def edges(kind="live"):
    ss, _ = network.sends(kind)
    return [(e["f"], tuple(e["to"]), e["k"]) for e in ss]


def node(team_code, role="team", person=None):
    t = Team.objects.get(code=team_code)
    if role == "team":
        return f"t:{t.id}"
    if role == "leader":
        return f"l:{Person.objects.get(team=t, role='tl').id}"
    return f"m:{person.id}"


def test_injects_and_replies_are_lines(excon):
    setup_exercise(excon)
    pa = participant("a", "Jane")
    deliver_now("3-13")  # from donotreply to every team
    deliver_now("1-15")  # from the team leader persona: drawn from the team's leader
    got = edges()
    ta, tb = node("a"), node("b")
    assert ("p:donotreply", (ta,), "inject") in got and ("p:donotreply", (tb,), "inject") in got
    assert (node("a", "leader"), (ta,), "inject") in got
    assert not any(f.startswith("p:undac") for f, _, _ in got)  # the leader persona is its leader
    # Team A answers the news from its inbox.
    news = pa.person.team.mails.get(owner=None, folder="inbox", persona="donotreply")
    send(pa, to="p:donotreply", subject="Re: News", body="Noted", in_reply_to=news.id)
    assert (node("a", "member", pa.person), ("p:donotreply",), "participant") in edges()
    # An ExCon answer to it comes from the persona.
    g = network.graph()
    ids = {n["id"]: n for n in g["nodes"]}
    assert ids["p:donotreply"]["type"] == "persona" and ids[ta]["type"] == "team"
    assert ids[node("a", "member", pa.person)]["team"] == ta  # members fold into their team on the map


def test_own_mailbox_answer_counts_once_and_mates_are_lines(excon):
    setup_exercise(excon)
    conf.put("personal_mailboxes", "1")
    pa, po = participant("a", "Jane"), participant("a", "Omar")
    deliver_now("1-15")
    mine = pa.person.team.mails.get(owner=pa.person, folder="inbox")
    send(pa, base="/inbox/me", to="p:undac-a-tl", cc=f"m:{po.person.id}", subject="Re: Update", body="Plan",
         in_reply_to=mine.id)
    jane, omar, lead = node("a", "member", pa.person), node("a", "member", po.person), node("a", "leader")
    mine_sends = [e for e in edges() if e[0] == jane]
    assert mine_sends == [(jane, tuple(sorted([lead, omar])), "participant")]  # the team inbox's copy isn't another


def test_leader_copies_withdrawn_and_test_team_are_apart(excon):
    setup_exercise(excon)
    Inject.objects.filter(mel_id="3-13").update(to_leaders=True)
    deliver_now("3-13")
    got = edges()
    assert not any(to.startswith("l:") for _, tos, _ in got for to in tos)  # the leaders' copies aren't lines
    m = Message.objects.filter(kind="live", inbox_team__code="a", inbox_owner=None).first()
    delivery.withdraw(m, [m.inbox_team_id])
    assert ("p:donotreply", (node("a"),), "inject") not in edges()
    assert ("p:donotreply", (node("b"),), "inject") in edges()
    assert edges("test") == []  # the Test team's traffic is its own map
    test_team = delivery.test_team()
    assert all(f"t:{test_team.id}" not in tos for _, tos, _ in got)


def test_poll_gives_only_what_is_new(excon):
    setup_exercise(excon)
    deliver_now("3-13")
    first, cursor = network.sends("live")
    pa = participant("a", "Jane")
    news = pa.person.team.mails.get(owner=None, folder="inbox", persona="donotreply")
    send(pa, to="p:donotreply", subject="Re: News", body="Noted", in_reply_to=news.id)
    more, _ = network.sends("live", {k: str(v) for k, v in cursor.items()})
    new = [e for e in more if e["id"] not in {x["id"] for x in first}]
    assert [e["k"] for e in new] == ["participant"]


def test_network_pages_are_excon_only(excon):
    setup_exercise(excon)
    deliver_now("3-13")
    for url in ("/dashboard/network/", "/dashboard/network/?screen=1"):
        r = excon.get(url)
        assert r.status_code == 200 and b"Source code" in r.content and b"network.js" in r.content
    d = excon.get("/dashboard/network/data/").json()
    assert d["sends"] and {n["type"] for n in d["nodes"]} >= {"team", "persona", "leader"}
    p = excon.get("/dashboard/network/poll/?kind=live&" + "&".join(f"{k}={v}" for k, v in d["cursor"].items())).json()
    assert "sends" in p and "cursor" in p
    pa, la, anon = participant("a"), leader("a"), Client()
    for url in ("/dashboard/network/", "/dashboard/network/data/", "/dashboard/network/poll/"):
        assert pa.get(url).status_code == 403 and la.get(url).status_code == 403
        assert "/login/" in anon.get(url)["Location"]

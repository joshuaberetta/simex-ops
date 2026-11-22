"""The participants' address book grows with use, as in a webmail: empty at first, then whoever
wrote to the mailbox or was written to, latest first; anyone else they may write to is reached
by typing their exact address."""

from ops.models import Mail, Sender, Team
from ops.tests.test_leaders import leader, setup_exercise
from ops.tests.test_ops import deliver_now, participant
from ops.tests.test_personal import send


def book(c, base="/inbox"):
    return [b["key"] for b in c.get(f"{base}/").context["simex_js"]["book"]]


def test_book_starts_empty_and_grows_latest_first(excon):
    setup_exercise(excon)
    Sender.objects.create(local_part="cnn", display_name="CNN", domain="cnn.com")
    pa, po = participant("a", "Jane"), participant("a", "Omar")
    assert book(pa) == [] and book(pa, "/inbox/me") == []
    deliver_now("1-15")  # from the team leader persona
    assert book(pa) == ["p:undac-a-tl"]
    # Typing an exact address finds them; a part of one, a name or another team's leader doesn't.
    assert pa.get("/inbox/lookup/?q=cnn@cnn.com").json()["key"] == "p:cnn"
    for q in ("cnn", "CNN", "cnn@", "undac-b-tl@simex.kobolabs.dev"):
        assert pa.get(f"/inbox/lookup/?q={q}").status_code == 404
    send(pa, base="/inbox", to="p:cnn", subject="Question", body="Hello")
    assert book(pa) == ["p:cnn", "p:undac-a-tl"]  # latest first
    assert book(pa, "/inbox/me") == ["p:cnn", "p:undac-a-tl"]  # a member's own: theirs and the team's
    # A teammate writing personally: in their book, not in the team inbox's.
    send(po, base="/inbox/me", **{"from": "me"}, to=f"m:{pa.person.id}", subject="Lunch", body="?")
    assert book(pa, "/inbox/me")[0] == f"m:{po.person.id}"
    assert f"m:{po.person.id}" not in book(po)  # Omar's team inbox never had it
    assert book(po, "/inbox/me")[0] == f"m:{pa.person.id}"


def test_leaders_and_readonly_views_keep_their_book(excon):
    setup_exercise(excon)
    participant("a", "Jane")
    la = leader("a")
    ta = Team.objects.get(code="a")
    assert book(la)[0] == f"t:{ta.id}"  # a leader writes to their team: always there
    assert excon.get(f"/inbox/as/{ta.id}/lookup/?q=a@teams.simex.kobolabs.dev").status_code == 404  # read-only
    assert not Mail.objects.filter(team=ta).exists()

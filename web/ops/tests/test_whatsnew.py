"""What's new (ops/whatsnew.py): each note reaches the logins it concerns, in the notifications
drawer of their pages, new until they have seen it once."""

from ops import whatsnew
from ops.models import WhatsNewSeen
from ops.tests.test_desk import FETCH, desk_client
from ops.tests.test_ops import participant

NOTES = [{"key": "n-mail", "date": whatsnew.ENTRIES[0]["date"], "who": whatsnew.MAIL, "title": "Mail", "body": "**x**"},
         {"key": "n-excon", "date": whatsnew.ENTRIES[0]["date"], "who": {whatsnew.EXCON}, "title": "ExCon", "body": "y"}]


def test_entries_are_well_formed():
    keys = [e["key"] for e in whatsnew.ENTRIES]
    assert len(keys) == len(set(keys))
    assert all(e["who"] <= whatsnew.EVERYONE and e["title"] and e["body"] for e in whatsnew.ENTRIES)
    assert [e["date"] for e in whatsnew.ENTRIES] == sorted((e["date"] for e in whatsnew.ENTRIES), reverse=True)


def test_each_login_gets_its_own_notes(excon):
    pa = participant("a")
    d = desk_client()
    got = whatsnew.for_user(pa.person.user, NOTES)
    assert [n["key"] for n in got["notes"]] == ["n-mail"] and got["new"] == 1
    assert [n["key"] for n in whatsnew.for_user(d.person.user, NOTES)["notes"]] == ["n-mail"]
    from django.contrib.auth import get_user_model
    staff = get_user_model().objects.get(is_staff=True)
    assert [n["key"] for n in whatsnew.for_user(staff, NOTES)["notes"]] == ["n-excon"]


def test_a_star_until_the_drawer_is_opened_then_the_notes_are_seen(excon):
    pa = participant("a")
    page = pa.get("/inbox/").content.decode()
    assert 'data-wn-bell' in page and 'data-wn-new="4"' in page and "data-wn-dot" in page and "Formatting and pictures in your messages" in page
    r = pa.post("/inbox/whatsnew/seen/", {"key": ["2026-11-list-filters", "2026-11-picture-size", "2026-11-rich-mail", "2026-11-whats-new", "nonsense"]}, headers=FETCH)
    assert r.json()["ok"]
    assert set(WhatsNewSeen.objects.values_list("key", flat=True)) == {"2026-11-list-filters", "2026-11-picture-size", "2026-11-rich-mail",
                                                                     "2026-11-whats-new"}
    page = pa.get("/inbox/").content.decode()
    assert 'data-wn-new="0"' in page and "Formatting and pictures" in page  # still listed, no longer new
    assert "data-wn-dot" not in page  # no star
    # ExCon's panel has it too, and only signed-in logins may mark.
    desk = desk_client().get("/inbox/desk/").content.decode()
    # The desk: What's new is its own button, apart from the notifications bell (their marks never overlap).
    assert 'data-wn-toggle="[data-wn-toggle]"' in desk and "data-wn-bell" in desk and "data-dn-toggle" in desk
    assert 'data-wn-toggle="[data-dn-toggle]"' not in desk
    assert 'data-wn-toggle="[data-nt-toggle]"' in excon.get("/injects/").content.decode()
    from django.test import Client
    assert Client().post("/whatsnew/seen/", {"key": "2026-11-rich-mail"}).status_code == 302


def test_participants_never_get_the_desk_or_excon_notes(excon):
    pa = participant("a")
    mine = {n["key"] for n in whatsnew.for_user(pa.person.user)["notes"]}
    for e in whatsnew.ENTRIES:
        if not e["who"] & {whatsnew.PARTICIPANT}:
            assert e["key"] not in mine, e["key"]
    desk_only = next(e["title"] for e in whatsnew.ENTRIES if e["who"] <= {whatsnew.DESK, whatsnew.EXCON})
    page = pa.get("/inbox/").content.decode()
    assert desk_only not in page and "See the injects coming up" not in page and "Copy other personas" not in page
    assert desk_only in desk_client().get("/inbox/desk/").content.decode()

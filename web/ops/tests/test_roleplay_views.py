"""Role play screens: ExCon's tab (views_roleplay.py) and the role players' pages (views_rp.py),
who reaches which, and what each sees."""

import datetime
import re

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone

from ops import roleplay
from ops.models import Person, RolePlay, Team
from ops.tests.test_leaders import leader
from ops.tests.test_ops import participant
from ops.tests.test_roleplay import TZ, do_import, teams4, workbook


def rp_client(name):
    p = Person.objects.get(name=name)
    p.user = get_user_model().objects.create_user(name.lower().replace(" ", "."), password="pw")
    p.save()
    c = Client()
    assert c.login(username=p.user.username, password="pw")
    c.person = p
    return c


def test_excon_imports_runs_the_board_and_prints(excon):
    teams4()
    assert excon.get("/roleplay/").status_code == 200
    r = excon.post("/roleplay/import/", {"file": SimpleUploadedFile("MEL.xlsx", workbook())})
    assert r.status_code == 302
    r = excon.get("/roleplay/import/")
    assert r.status_code == 200 and b"Kenyan RP 1 (new)" in r.content and b"14:40" in r.content
    r = excon.post("/roleplay/import/apply/", {"day-monday": "2026-11-23", "day-tuesday": "2026-11-24"})
    assert r.status_code == 302 and RolePlay.objects.count() == 7
    r = excon.get("/roleplay/?day=2026-11-23")
    assert r.status_code == 200 and b"9-04" in r.content and b"Peter" in r.content
    s = RolePlay.objects.get(key="9-03").slots.first()
    excon.post(f"/roleplay/slot/{s.id}/status/", {"status": "ready"})
    s.refresh_from_db()
    assert s.status == "ready" and s.status_by.startswith("ExCon")
    play = s.play
    excon.post(f"/roleplay/{play.id}/", {"title": "Immigration", "location": "Arrivals", "script": "Ask for ID",
                                         "players": [p.id for p in play.players.all()]})
    play.refresh_from_db()
    assert play.location == "Arrivals" and play.script == "Ask for ID" and play.players.count() == 2
    excon.post("/roleplay/slip/", {"day": "2026-11-23", "from": "18:00", "minutes": "10"})
    assert timezone.localtime(RolePlay.objects.get(key="9-04").slots.first().start).strftime("%H:%M") == "18:10"
    r = excon.get(f"/roleplay/print/?player={Person.objects.get(name='Peter').id}")
    assert r.status_code == 200 and b"you are the coordinator" in r.content and b"greet the team" not in r.content
    assert excon.get("/roleplay/players/").status_code == 200
    assert excon.get(f"/roleplay/{play.id}/").status_code == 200
    r = excon.post("/people/save/", {"name": "Zoe Actor", "role": "rp", "team_id": Team.objects.get(code="a").id,
                                     "account": "1", "back": "/roleplay/players/"})
    assert r.url == "/roleplay/players/"
    zoe = Person.objects.get(name="Zoe Actor")
    assert zoe.role == "rp" and zoe.team is None and zoe.user and not zoe.user.is_staff


def test_role_player_sees_only_their_own_role_plays():
    teams4()
    do_import()
    peter = rp_client("Peter")
    r = peter.get("/inbox/")
    assert r.status_code == 302 and r.url == "/inbox/rp/"
    assert peter.get("/").url == "/inbox/rp/"
    r = peter.get("/inbox/rp/")
    assert r.status_code == 200 and b"9-04" in r.content and b"9-06" in r.content and b"9-03" not in r.content
    mine, other = RolePlay.objects.get(key="9-04"), RolePlay.objects.get(key="9-03")
    assert b"you are the coordinator" in peter.get(f"/inbox/rp/play/{mine.id}/").content
    assert peter.get(f"/inbox/rp/play/{other.id}/").status_code == 404
    s = mine.slots.first()
    peter.post(f"/inbox/rp/slot/{s.id}/status/", {"status": "ready"})
    s.refresh_from_db()
    assert s.status == "ready" and s.status_by == "Peter"
    assert peter.post(f"/inbox/rp/slot/{other.slots.first().id}/status/", {"status": "done"}).status_code == 404
    assert peter.get("/roleplay/").status_code == 403 and peter.get("/injects/").status_code == 403
    assert peter.get("/inbox/desk/").status_code == 403


def test_leader_plays_only_their_own_team_and_gets_a_tab():
    teams4()
    la, lb = leader("a"), leader("b")
    Person.objects.filter(pk=la.person.pk).update(name="Lena Park")
    Person.objects.filter(pk=lb.person.pk).update(name="Omar Diallo")
    do_import()
    kick = RolePlay.objects.get(key="9-02")
    assert {p.name for p in kick.players.all()} == {"Lena Park", "Omar Diallo"}
    kick.slots.get().teams.set([Team.objects.get(code="b")])  # Team B's only
    assert b"My role plays" in la.get("/inbox/").content
    assert b"9-02" not in la.get("/inbox/rp/").content and b"9-02" in lb.get("/inbox/rp/").content
    assert leader("c").get("/inbox/rp/").status_code == 403  # no role plays: not for them


def test_participants_never_reach_role_play_pages():
    teams4()
    do_import()
    pa, anon = participant("a"), Client()
    play = RolePlay.objects.first()
    for url in ("/inbox/rp/", f"/inbox/rp/play/{play.id}/", "/inbox/rp/help/", "/roleplay/", f"/roleplay/{play.id}/"):
        assert pa.get(url).status_code == 403, url
        assert "/login/" in anon.get(url)["Location"], url
    walked = 0

    def walk(patterns, prefix=""):
        for p in patterns:
            if isinstance(p, URLResolver):
                yield from walk(p.url_patterns, prefix + str(p.pattern))
            elif isinstance(p, URLPattern):
                yield prefix + str(p.pattern), p.callback

    for route, cb in walk(get_resolver().url_patterns):
        if route.startswith("inbox/rp/"):
            assert getattr(cb, "rp_only", False), route
            url = "/" + re.sub(r"<int:\w+>", "1", route)
            assert pa.get(url).status_code == 403 and pa.post(url).status_code == 403, url
            walked += 1
        elif route.startswith("roleplay/"):
            assert getattr(cb, "excon_only", False), route
    assert walked >= 8


def test_qr_sign_in_opens_the_role_player_setup():
    from ops import signin
    teams4()
    do_import()
    peter = Person.objects.get(name="Peter")
    peter.user = get_user_model().objects.create_user("peter", password="pw")
    peter.save()
    assert signin.can_use(peter.user)
    r = Client().get(signin.path(peter.user))
    assert r.status_code == 302 and r.url == "/inbox/rp/setup/"


def test_start_afresh_undoes_progress_and_slips():
    from ops import reset
    teams4()
    do_import()
    rc = RolePlay.objects.get(key="9-04")
    roleplay.slip(datetime.datetime(2026, 11, 23, 17, 0, tzinfo=TZ), 30)
    roleplay.set_status(rc.slots.first(), "done")
    reset.start_afresh()
    starts = [timezone.localtime(s.start).strftime("%H:%M") for s in rc.slots.all()]
    assert starts == ["18:00", "18:20"] and not rc.slots.exclude(status="pending").exists()

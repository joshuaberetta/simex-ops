"""The simulation clock (ops/simclock.py): SIMEX Mail dates mail at the scenario's time, members
see only it, leaders, the persona desk and ExCon see both; the clock widget and its settings."""

from datetime import datetime, timedelta
from unittest import mock

from django.utils import timezone

from ops import conf, simclock
from ops.models import Inject, Mail, Team
from ops.tests.test_leaders import leader, setup_exercise
from ops.tests.test_ops import deliver_now, participant


def at(text):
    return timezone.make_aware(datetime.fromisoformat(text))


def test_clock_runs_at_its_speed():
    assert simclock.now() is None  # off by default
    simclock.set_clock(at("2035-05-20T09:00"), 4)
    t0 = simclock.now()
    assert abs((t0 - at("2035-05-20T09:00")).total_seconds()) < 5
    with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(minutes=10)):
        assert abs((simclock.now() - at("2035-05-20T09:40")).total_seconds()) < 30
    simclock.set_rate(0)  # stopped: keeps the time it reads
    with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(hours=3)):
        assert abs((simclock.now() - t0).total_seconds()) < 30
    conf.put("sim_enabled", "0")
    assert simclock.now() is None and simclock.js() is None
    assert simclock.parse("2035-05-20 09:00") == at("2035-05-20T09:00") and simclock.parse("soon") is None


def test_mail_dated_by_scenario_time_and_clock(excon):
    setup_exercise(excon)
    Inject.objects.filter(mel_id="1-15").update(scenario_dt="2035-05-21T07:30")
    deliver_now("1-15")  # clock off: no simulation time
    assert not Mail.objects.exclude(sim_at=None).exists()
    Mail.objects.all().delete()
    simclock.set_clock(at("2035-05-20T09:00"), 1)
    conf.put("sim_follow", "0")
    Inject.objects.filter(mel_id="1-15").update(status="scheduled")
    deliver_now("1-15")
    ta = Team.objects.get(code="a")
    mail = Mail.objects.get(team=ta, owner=None, folder="inbox")
    assert mail.sim_at == at("2035-05-21T07:30")  # the inject's own Scenario time
    assert all(m.sim_at == mail.sim_at for m in Mail.objects.filter(message=mail.message))  # every copy, the desk's too
    assert simclock.now() < at("2035-05-20T10:00")  # not following: the clock stays
    # A member's answer: the clock's time.
    pa = participant("a", "Amina")
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re", "body": "On it", "in_reply_to": mail.id})
    sent = Mail.objects.get(team=ta, owner=None, folder="sent")
    assert abs((sent.sim_at - at("2035-05-20T09:00")).total_seconds()) < 60
    # Following the injects: a later Scenario time moves the clock forward, never back.
    conf.put("sim_follow", "1")
    simclock.follow(Inject.objects.get(mel_id="1-15"))
    assert abs((simclock.now() - at("2035-05-21T07:30")).total_seconds()) < 60
    Inject.objects.filter(mel_id="1-15").update(scenario_dt="2035-05-19T07:30")
    simclock.follow(Inject.objects.get(mel_id="1-15"))
    assert simclock.now() > at("2035-05-21T07:00")


def test_members_see_simulation_time_leaders_see_both(excon):
    setup_exercise(excon)
    simclock.set_clock(at("2035-05-20T09:00"), 1)
    Inject.objects.filter(mel_id="1-15").update(scenario_dt="2035-05-20T08:15")
    deliver_now("1-15")
    mail = Mail.objects.get(team__code="a", owner=None, folder="inbox")
    real = timezone.localtime(mail.created_at).strftime("%b %-d, %Y, %H:%M")
    pa, la = participant("a", "Amina"), leader("a")
    page = pa.get(f"/inbox/t/{mail.thread}/").content.decode()
    assert "data-simclock" in page and "May 20, 2035, 08:15" in page and "Real time" not in page
    assert real not in page
    assert "08:15" in pa.get("/inbox/").content.decode()
    # The quoted date of a reply is the simulation one.
    assert "May 20, 2035 at 08:15" in pa.get(f"/inbox/t/{mail.thread}/").context["thread_js"]["msgs"][str(mail.id)]["date"]
    lead = la.get(f"/inbox/team/t/{mail.thread}/").content.decode()
    assert "Real time" in lead and "May 20, 2035, 08:15" in lead and real in lead
    desk = excon.get(f"/inbox/as/{mail.team_id}/t/{mail.thread}/").content.decode()
    assert "Real time" in desk and real in desk
    j = pa.get("/inbox/poll/").json()["clock"]
    assert j["rate"] == 1 and abs(j["sim"] / 1000 - at("2035-05-20T09:00").timestamp()) < 60
    conf.put("sim_enabled", "0")  # off: the real time again, no clock
    page = pa.get(f"/inbox/t/{mail.thread}/").content.decode()
    assert "data-simclock" not in page and pa.get("/inbox/poll/").json()["clock"] is None


def test_settings_sim(excon):
    r = excon.post("/settings/sim/", {"enabled": "1", "sim_time": "", "rate": "1"})
    assert not simclock.enabled() and "Give the simulation date" in excon.get(r.url).content.decode()
    excon.post("/settings/sim/", {"enabled": "1", "sim_time": "2035-05-20T09:00", "sim_shown": "", "rate": "2",
                                  "follow": "1"})
    assert simclock.enabled() and simclock.rate() == 2 and conf.flag("sim_follow")
    assert abs((simclock.now() - at("2035-05-20T09:00")).total_seconds()) < 30
    page = excon.get("/settings/").content.decode()
    assert 'value="2035-05-20T09:00"' in page and 'id="excon-sim"' in page  # the status bar's clock
    assert abs(excon.get("/excon/poll/").json()["clock"]["sim"] / 1000 - at("2035-05-20T09:00").timestamp()) < 60
    # Saved unchanged (the form's time is when the page was shown): the clock isn't set back.
    with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(minutes=30)):
        excon.post("/settings/sim/", {"enabled": "1", "sim_time": "2035-05-20T09:00", "sim_shown": "2035-05-20T09:00",
                                      "rate": "2", "follow": "1"})
        assert simclock.now() > at("2035-05-20T09:50")
    excon.post("/settings/sim/", {"sim_time": "2035-05-20T09:00", "sim_shown": "2035-05-20T09:00", "rate": "2"})
    assert not simclock.enabled() and not conf.flag("sim_follow")
    page = excon.get("/injects/").content.decode()
    assert 'id="excon-sim"' not in page and "Simulation clock off" in page and excon.get("/excon/poll/").json()["clock"] is None
    assert excon.post("/settings/sim/", {"enabled": "1", "rate": "fast"}).status_code == 302 and simclock.rate() == 2

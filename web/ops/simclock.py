"""The simulation clock: the scenario's date and time, which SIMEX Mail shows instead of the real
one (Settings → Simulation clock).

ExCon sets it ("it is now 20 May 2035, 09:00 in the scenario") and its speed (1 = real time, 4 =
four scenario minutes per real minute, 0 = stopped); with "follow the injects", a live inject
delivered with a Scenario time later than the clock moves the clock forward to it (never back:
the injects aren't always in scenario order). Each Mail is stamped with its simulation time when
it lands (Mail.sim_at: an inject's own Scenario time, else the clock's time then), so changing the
clock later never re-dates mail already in the mailboxes. Off, or for mail from before it was on,
SIMEX Mail shows the real time. Participants (members) see only the simulation time; team
leaders, the persona desk and ExCon see both.
"""

from datetime import datetime

from django.utils import timezone

from . import conf

INJECT_KINDS = ("live", "test")  # an inject's own delivery takes its Scenario time (not follow-ups)


def enabled():
    return conf.flag("sim_enabled") and bool(conf.get("sim_anchor"))


def parse(text):
    """A Scenario time or a datetime-local value ("2035-05-20T09:00", "2035-05-20 09:00",
    with or without seconds) -> an aware datetime in the exercise timezone, or None."""
    text = (text or "").strip().replace(" ", "T", 1)
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def rate():
    try:
        return max(0.0, min(1000.0, float(conf.get("sim_rate") or 1)))
    except ValueError:
        return 1.0


def _real_anchor():
    try:
        return datetime.fromisoformat(conf.get("sim_anchor_real"))
    except (TypeError, ValueError):
        return None


def now(at=None):
    """The simulation time at the real time at (default now), or None when the clock is off."""
    if not enabled():
        return None
    sim0, real0 = parse(conf.get("sim_anchor")), _real_anchor()
    if not sim0:
        return None
    if not real0:
        return sim0
    return sim0 + ((at or timezone.now()) - real0) * rate()


def _mapping():
    """(sim anchor, real anchor, speed) for placing a real time on the scenario's timeline and
    back (SIMEX Calendar), or None when the clock is off. A stopped clock (speed 0) maps at speed
    1 from where it stopped, so events keep their length and order."""
    if not enabled():
        return None
    sim0 = parse(conf.get("sim_anchor"))
    if not sim0:
        return None
    return sim0, _real_anchor() or timezone.now(), rate() or 1.0


def at(real):
    """The simulation time of a real (aware) time, past or future; None when the clock is off."""
    m = _mapping()
    if not m or real is None:
        return None
    sim0, real0, speed = m
    return sim0 + (real - real0) * speed


def real_at(sim):
    """The real time a simulation (aware) time falls at: the inverse of at(); sim itself when the
    clock is off."""
    m = _mapping()
    if not m or sim is None:
        return sim
    sim0, real0, speed = m
    return real0 + (sim - sim0) / speed


def set_clock(sim_dt, speed=None, enabled_=True):
    """The clock reads sim_dt from now on (and runs at speed)."""
    conf.put("sim_anchor", timezone.localtime(sim_dt).replace(tzinfo=None).isoformat(timespec="seconds"))
    conf.put("sim_anchor_real", timezone.now().isoformat())
    if speed is not None:
        conf.put("sim_rate", f"{max(0.0, min(1000.0, float(speed))):g}")
    conf.put("sim_enabled", "1" if enabled_ else "0")


def set_rate(speed):
    """A new speed from now on, the clock keeping the time it reads."""
    current = now()
    if current:
        set_clock(current, speed, enabled_=conf.flag("sim_enabled"))
    else:
        conf.put("sim_rate", f"{max(0.0, min(1000.0, float(speed))):g}")


def follow(inj):
    """A live inject was delivered: with "follow the injects", a Scenario time ahead of the
    clock moves it forward to it."""
    if not inj or not enabled() or not conf.flag("sim_follow"):
        return
    at, current = parse(inj.scenario_dt), now()
    if at and current and at > current:
        set_clock(at)


def stamp(mail):
    """The simulation time a Mail landing now gets (None: the clock is off)."""
    if not enabled():
        return None
    if mail.inject_id and mail.message_id and mail.message.kind in INJECT_KINDS and mail.inject.scenario_dt:
        at = parse(mail.inject.scenario_dt)
        if at:
            return at
    return now()


def js(current=None):
    """What the clock widgets need: the simulation time now (epoch ms) and its speed; None when off."""
    current = current or now()
    if not current:
        return None
    return {"sim": int(current.timestamp() * 1000), "rate": rate(), "tz": timezone.get_current_timezone_name()}

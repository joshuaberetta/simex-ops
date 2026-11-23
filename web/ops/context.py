from datetime import datetime

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from . import conf, mailer, simclock
from .access import is_excon, participant_of
from .models import Inject, Mail, Response


def source(request):
    return {"SOURCE_URL": settings.SOURCE_URL}


def status(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    if is_excon(user):
        cfg = conf.get_all()
        counts = dict(Inject.objects.values_list("status").annotate(n=Count("id")).values_list("status", "n"))
        tick = cfg.get("scheduler_tick")
        try:
            tick = datetime.fromisoformat(tick) if tick else None
        except ValueError:
            tick = None
        return {
            "excon": True, "cfg": cfg, "channel_mode": cfg.get("channel_mode", "email"), "mailer_name": mailer.get().name,
            "CHANNEL_MODES": conf.CHANNEL_MODES, "status_counts": counts, "MAIL_DOMAIN": settings.MAIL_DOMAIN,
            "MAIL_URL": settings.MAIL_URL or settings.PUBLIC_URL.rstrip("/"),
            "failed_count": counts.get("failed", 0) + counts.get("partial", 0),
            "next_inject": Inject.objects.filter(status="scheduled", send_at__isnull=False).order_by("send_at").first(),
            "new_responses": Response.objects.filter(status="new", kind="live").count(),
            "scheduler_tick": tick,
            "scheduler_age": int((timezone.now() - tick).total_seconds()) if tick else None,
        }
    person = participant_of(user)
    if person:
        return {"participant": person,
                "unread": Mail.objects.filter(team=person.team, owner=None, folder="inbox", read_at__isnull=True).count()}
    return {}


def sim(request):
    """The simulation clock (ops/simclock.py) for the clock widgets and the mail dates: both =
    the viewer sees the real time too (everyone but the teams' members)."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    now = simclock.now()
    if not now:
        return {"sim": {"on": False}}
    both = participant_of(user) is None
    return {"sim": {"on": True, "both": both, "now": now, "js": {**simclock.js(now), "both": both}}}

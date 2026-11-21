"""Read-only readiness check before a live exercise: settings, teams and logins, injects and the
scheduler, as a list of OK / CHECK / ERROR lines. Changes nothing. On the server:
    python manage.py preflight"""

from datetime import datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from ops import checks, conf
from ops.models import Inject, Person, PushSubscription, Sender, Team
from ops.render import PLACEHOLDER_DOMAIN, EMAIL


def _placeholder(addr):
    return not addr or not EMAIL.match(addr) or bool(PLACEHOLDER_DOMAIN.search(addr))


class Command(BaseCommand):
    help = "Read-only readiness check before a live exercise."

    def handle(self, *args, **opts):
        counts = {"OK": 0, "CHECK": 0, "ERROR": 0}

        def say(level, text):
            counts[level] += 1
            self.stdout.write(f"{level:5}  {text}")

        cfg, now = conf.get_all(), timezone.now()
        mode = cfg["channel_mode"]
        self.stdout.write("== Settings")
        say("OK", f"delivery channel: {conf.CHANNEL_MODES.get(mode, mode)}")
        say("OK" if cfg["live_armed"] == "1" else "CHECK", f"live sending {'armed' if cfg['live_armed'] == '1' else 'NOT armed'}")
        say("CHECK" if cfg["paused"] == "1" else "OK", f"sending {'PAUSED' if cfg['paused'] == '1' else 'not paused'}")
        tick = cfg.get("scheduler_tick")
        age = (now - datetime.fromisoformat(tick)).total_seconds() if tick else None
        say("OK" if age is not None and age < 120 else "ERROR",
            f"scheduler last ticked {int(age)} s ago" if age is not None else "scheduler has never ticked")
        email_used = mode == "email" or (mode == "per_inject" and Inject.objects.filter(channel="email").exists())
        if email_used:
            say("OK" if settings.MAILER == "resend" else "ERROR", f"mailer: {settings.MAILER}")
            say("OK" if settings.RESEND_WEBHOOK_SECRET else "ERROR",
                "Resend webhook secret " + ("set" if settings.RESEND_WEBHOOK_SECRET else "missing: replies only by the minute's catch-up"))
        say("OK" if settings.VAPID_PRIVATE_KEY else "CHECK", "push notifications " + ("on" if settings.VAPID_PRIVATE_KEY else "off (no VAPID key)"))
        say("OK", f"AI: provider {settings.AI_PROVIDER}, analysis {'on' if cfg['ai_enabled'] == '1' else 'off'}, "
                  f"AI personas {'on (' + cfg['npc_scope'] + ')' if cfg['npc_enabled'] == '1' else 'off'}")

        self.stdout.write("== Teams and people")
        teams = list(Team.objects.filter(active=True, is_test=False))
        if not teams:
            say("ERROR", "no active teams")
        for t in teams:
            people = list(t.people.filter(active=True).select_related("user"))
            members = [p for p in people if p.role == "member"]
            leaders = [p for p in people if p.role == "tl"]
            place = [p for p in people if _placeholder(p.email)]
            if people and all("(placeholder)" in p.name or (email_used and p in place) for p in people):
                say("ERROR", f"{t.name}: looks like a seeded placeholder team (deactivate or delete it)")
                continue
            if email_used and t.email and _placeholder(t.email):
                say("ERROR", f"{t.name}: team address '{t.email}' is a placeholder: its 'each team' emails fail "
                             "(clear it to send to the members instead)")
            if not members:
                say("ERROR", f"{t.name}: no members")
            no_login = [p.name for p in members if not (p.user_id and p.user.is_active)]
            if no_login and mode != "email":
                say("ERROR", f"{t.name}: {len(no_login)} member(s) without a SIMEX Mail login: {', '.join(no_login[:5])}")
            if place and email_used:
                say("CHECK", f"{t.name}: {len(place)} without a real email address: {', '.join(p.name for p in place[:5])}")
            if len(leaders) != 1:
                say("CHECK", f"{t.name}: {len(leaders)} team leaders (expected 1)")
            elif not (leaders[0].user_id and leaders[0].user.is_active):
                say("CHECK", f"{t.name}: leader {leaders[0].name} has no login")
            subs = PushSubscription.objects.filter(person__team=t).count()
            say("OK", f"{t.name}: {len(members)} members, {len(leaders)} leader, {subs} device(s) with push")
        stale = Person.objects.filter(active=True, team__active=True, user__isnull=False,
                                      user__last_login__lt=now - timedelta(days=13)).count()
        if stale:
            say("CHECK", f"{stale} login(s) last signed in over 13 days ago: their session (and any QR slip printed "
                         "then) expires after 14 days; reprint slips if needed")

        self.stdout.write("== Injects")
        by = {}
        for s in Inject.objects.values_list("status", flat=True):
            by[s] = by.get(s, 0) + 1
        say("OK", "status: " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())))
        grace = timedelta(minutes=float(cfg.get("grace_minutes") or 10))
        late = Inject.objects.filter(status="scheduled", send_at__lt=now - grace).count()
        if late:
            say("CHECK", f"{late} scheduled inject(s) are past their time and grace: they will be marked overdue, not sent")
        sched = Inject.objects.filter(status="scheduled", send_at__isnull=False).order_by("send_at")
        if sched.exists():
            first, last = sched.first().send_at, sched.last().send_at
            say("OK", f"scheduled: {sched.count()}, from {timezone.localtime(first):%a %d %b %H:%M} "
                      f"to {timezone.localtime(last):%a %d %b %H:%M} ({settings.TIME_ZONE})")
        files, senders = checks.files_by_inject(), set(Sender.objects.values_list("local_part", flat=True))
        for inj in Inject.objects.exclude(status__in=["sent", "cancelled", "draft"]).order_by("send_at", "seq"):
            for level, label, detail in checks.inject_flags(inj, files.get(inj.id, []), senders):
                say("ERROR" if level == "err" else "CHECK", f"{inj.mel_id}: {label}: {detail[:160]}")
        if not Sender.objects.filter(team_leader=True).exists():
            say("CHECK", "no team leader persona (Senders): leaders can't play it in SIMEX Mail")

        self.stdout.write(f"== {counts['ERROR']} error(s), {counts['CHECK']} to check, {counts['OK']} OK")

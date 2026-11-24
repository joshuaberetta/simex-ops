"""The scheduler loop: overdue marking, due live injects, test-run items, role players' reminders. Runs as its own
process (the DO worker component). Safe to run alongside the web process: rows are claimed
with conditional UPDATEs (and SELECT ... FOR UPDATE SKIP LOCKED on Postgres).

Claude analysis of replies (ops/ai.py) runs in a second thread of the same process, so a
slow model call never holds up an inject. AI personas' answers (ops/npc.py) are written there too,
before the analysis, as teams are waiting for them."""

import logging
import signal
import threading
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections, connection

from ops import ai, calendar, delivery, desk, inbound, npc, roleplay

log = logging.getLogger("simex.scheduler")
AI_IDLE_SECONDS = 15
INBOUND_SECONDS = 60


def ai_loop(stop):
    log.info("AI analysis loop started (provider %s)", ai.provider())
    while not stop["now"]:
        close_old_connections()
        busy = 0
        try:
            busy = npc.tick()
        except Exception:
            log.exception("AI persona tick failed")
        try:
            busy += ai.tick()
        except Exception:
            log.exception("AI tick failed")
        if not busy:
            time.sleep(AI_IDLE_SECONDS)
    connection.close()


def inbound_loop(stop):
    """Received email the webhook missed, and failed fetches, without anyone pressing Sync."""
    while not stop["now"]:
        close_old_connections()
        try:
            added, fetched = inbound.catch_up()
            if added:
                log.info("inbound catch-up: %s received email(s) the webhook missed", added)
        except Exception:
            log.exception("inbound catch-up failed")
        time.sleep(INBOUND_SECONDS)
    connection.close()


class Command(BaseCommand):
    help = "Run the inject scheduler loop (and the AI analysis loop)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one tick and exit.")
        parser.add_argument("--no-ai", action="store_true", help="Don't run the AI analysis loop.")

    def handle(self, *args, **opts):
        stop = {"now": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
        if opts["once"]:
            delivery.tick()
            roleplay.tick()
            desk.tick()
            calendar.tick()
            if not opts["no_ai"]:
                npc.tick()
                ai.tick()
            return
        if not opts["no_ai"] and ai.available():
            threading.Thread(target=ai_loop, args=(stop,), daemon=True, name="ai").start()
        threading.Thread(target=inbound_loop, args=(stop,), daemon=True, name="inbound").start()
        log.info("scheduler started (tick %ss)", settings.TICK_SECONDS)
        while not stop["now"]:
            close_old_connections()
            try:
                delivery.tick()
            except Exception:
                log.exception("scheduler tick failed")
            try:
                roleplay.tick()  # role players' reminders, no-show notices
            except Exception:
                log.exception("role-play tick failed")
            try:
                desk.tick()  # teams waiting too long on the persona desk: a notice
            except Exception:
                log.exception("desk tick failed")
            try:
                calendar.tick()  # SIMEX Calendar's reminders, persona meetings nobody has
            except Exception:
                log.exception("calendar tick failed")
            time.sleep(settings.TICK_SECONDS)

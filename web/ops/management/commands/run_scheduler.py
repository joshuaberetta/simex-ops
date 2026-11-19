"""The scheduler loop: overdue marking, due live injects, test-run items. Runs as its own
process (the DO worker component). Safe to run alongside the web process: rows are claimed
with conditional UPDATEs (and SELECT ... FOR UPDATE SKIP LOCKED on Postgres).

Claude analysis of replies (ops/ai.py) runs in a second thread of the same process, so a
slow model call never holds up an inject."""

import logging
import signal
import threading
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections, connection

from ops import ai, delivery

log = logging.getLogger("simex.scheduler")
AI_IDLE_SECONDS = 15


def ai_loop(stop):
    log.info("AI analysis loop started (provider %s)", ai.provider())
    while not stop["now"]:
        close_old_connections()
        busy = 0
        try:
            busy = ai.tick()
        except Exception:
            log.exception("AI tick failed")
        if not busy:
            time.sleep(AI_IDLE_SECONDS)
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
            if not opts["no_ai"]:
                ai.tick()
            return
        if not opts["no_ai"] and ai.available():
            threading.Thread(target=ai_loop, args=(stop,), daemon=True, name="ai").start()
        log.info("scheduler started (tick %ss)", settings.TICK_SECONDS)
        while not stop["now"]:
            close_old_connections()
            try:
                delivery.tick()
            except Exception:
                log.exception("scheduler tick failed")
            time.sleep(settings.TICK_SECONDS)

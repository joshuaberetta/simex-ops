"""The scheduler loop: overdue marking, due live injects, test-run items. Runs as its own
process (the DO worker component). Safe to run alongside the web process: rows are claimed
with conditional UPDATEs (and SELECT ... FOR UPDATE SKIP LOCKED on Postgres)."""

import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from ops import delivery

log = logging.getLogger("simex.scheduler")


class Command(BaseCommand):
    help = "Run the inject scheduler loop."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one tick and exit.")

    def handle(self, *args, **opts):
        stop = {"now": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
        log.info("scheduler started (tick %ss)", settings.TICK_SECONDS)
        while not stop["now"]:
            close_old_connections()
            try:
                delivery.tick()
            except Exception:
                log.exception("scheduler tick failed")
            if opts["once"]:
                break
            time.sleep(settings.TICK_SECONDS)

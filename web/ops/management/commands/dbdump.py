"""Runs the database dump that Settings → Database dump started (ops/dbdump.py): pg_dump into
the working directory, then the state and a Notice. Not meant to be run by hand."""

from django.core.management.base import BaseCommand

from ops import dbdump


class Command(BaseCommand):
    help = "Run the pending database dump started from Settings (internal)."

    def handle(self, *args, **opts):
        dbdump.run_job()

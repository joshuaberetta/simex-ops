"""Write the whole-database backup (the same JSON file as Settings → Download backup):
    python manage.py backup simex-backup.json      (or - for stdout)"""

import sys

from django.core.management.base import BaseCommand

from ops import backup


class Command(BaseCommand):
    help = "Write a backup (JSON, as Settings → Download backup) to a file."

    def add_arguments(self, p):
        p.add_argument("file", help="Output file, or - for stdout")

    def handle(self, *args, **opts):
        out = sys.stdout if opts["file"] == "-" else open(opts["file"], "w", encoding="utf-8")
        try:
            for chunk in backup.export_stream():
                out.write(chunk)
        finally:
            if out is not sys.stdout:
                out.close()
        if out is not sys.stdout:
            self.stderr.write(f"Backup written to {opts['file']}.")

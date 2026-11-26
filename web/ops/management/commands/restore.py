"""Restore a backup file (Settings → Download backup, or `manage.py backup`) from a terminal,
without uploading it through the web: it REPLACES everything in this database, logins
included, and leaves live sending disarmed. A backup from the first (email-only) app is merged
in instead, as on the Settings page.
    python manage.py restore simex-backup.json [--yes]"""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from ops import backup, legacy_import


class Command(BaseCommand):
    help = "Restore a backup file: replaces everything in this database."

    def add_arguments(self, p):
        p.add_argument("file")
        p.add_argument("--yes", action="store_true", help="Don't ask for confirmation")

    def handle(self, *args, **opts):
        db = connection.settings_dict
        where = f"{db.get('NAME')} on {db.get('HOST') or 'this machine'}"
        if not opts["yes"]:
            answer = input(f"This replaces everything in {where}, logins included. Type RESTORE: ")
            if answer.strip() != "RESTORE":
                raise CommandError("Not restored.")
        with open(opts["file"], "rb") as f:
            data = json.load(f)
        try:
            if data.get("format") == legacy_import.FORMAT:
                counts = legacy_import.import_backup(data)
            else:
                counts = backup.restore(data)
        except Exception as e:
            raise CommandError(f"Restore failed, nothing changed: {e}")
        for k, v in counts.items():
            self.stdout.write(f"{v:8d}  {k}")
        self.stdout.write(f"Restored into {where}. Live sending is disarmed.")

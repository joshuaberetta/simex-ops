import json
import sys
from datetime import timedelta

import pytest
from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from ops import backup, conf, dbdump
from ops.models import Audit, DeskChat, DeskChatTurn, Notice, Person, Team


@pytest.fixture
def dumps(tmp_path, monkeypatch):
    monkeypatch.setattr(dbdump, "DIR", tmp_path / "dumps")
    monkeypatch.setattr(dbdump, "RUN_INLINE", True)
    monkeypatch.setattr(dbdump, "available", lambda: (True, ""))
    monkeypatch.setattr(dbdump, "command", lambda out: [
        sys.executable, "-c", "import sys, os; open(sys.argv[1], 'wb').write(b'PGDMP' + os.environ.get('PGDATABASE', '').encode())",
        str(out)])
    return tmp_path / "dumps"


def test_dump_button_makes_a_file_to_download(excon, dumps):
    assert b"Make a database dump" in excon.get("/settings/").content
    r = excon.post("/settings/dump/", {"action": "start"})
    assert r.status_code == 302
    st = dbdump.status()
    assert st["state"] == "ready" and st["size"] > 5
    n = Notice.objects.get(key=f"dbdump:{st['name']}")
    assert n.title == "Database dump ready" and n.url == "/settings/#dump"
    page = excon.get("/settings/").content
    assert b"Download database dump" in page and st["name"].encode() in page
    r = excon.get("/settings/dump/download/")
    assert r.status_code == 200 and b"".join(r.streaming_content).startswith(b"PGDMP")
    assert st["name"] in r["Content-Disposition"]
    assert set(Audit.objects.values_list("action", flat=True)) >= {"dbdump_start", "dbdump_download"}
    # a new one replaces it; delete removes it
    excon.post("/settings/dump/", {"action": "delete"})
    assert dbdump.status()["state"] == "none"
    assert excon.get("/settings/dump/download/").status_code == 404


def test_dump_failure_is_shown(excon, dumps, monkeypatch):
    monkeypatch.setattr(dbdump, "command", lambda out: [
        sys.executable, "-c", "import sys; sys.stderr.write('pg_dump: error: connection refused'); sys.exit(1)"])
    excon.post("/settings/dump/", {"action": "start"})
    st = dbdump.status()
    assert st["state"] == "failed" and "connection refused" in st["error"]
    assert b"The last dump failed" in excon.get("/settings/").content
    assert Notice.objects.filter(title="Database dump failed").exists()


def test_dump_password_never_in_argv(dumps, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(dbdump, "settings", SimpleNamespace(DATABASES={"default": {
        "HOST": "db.example", "PORT": 25060, "USER": "simex_ops", "PASSWORD": "s3cret", "NAME": "simex_ops",
        "OPTIONS": {"sslmode": "require"}}}))
    env = dbdump._env()
    assert env["PGPASSWORD"] == "s3cret" and env["PGSSLMODE"] == "require" and env["PGPORT"] == "25060"
    assert not any("s3cret" in a for a in dbdump.command("x"))


def test_running_and_stale_dumps(dumps):
    dbdump._write({"state": "running", "name": "x.dump", "started_at": timezone.now().isoformat()})
    assert dbdump.status()["state"] == "running"
    with pytest.raises(RuntimeError):
        dbdump.start("undac")
    old = (timezone.now() - timedelta(minutes=30)).isoformat()
    dbdump._write({"state": "running", "name": "x.dump", "started_at": old})
    assert dbdump.status()["state"] == "failed"  # never finished: the process died
    dbdump._write({"state": "ready", "name": "x.dump", "finished_at": (timezone.now() - timedelta(hours=4)).isoformat()})
    (dumps / "x.dump").write_bytes(b"x")
    assert dbdump.status()["state"] == "none" and not (dumps / "x.dump").exists()  # kept 3 hours only


def test_dump_unavailable_on_sqlite(excon):
    ok, why = dbdump.available()
    assert not ok and "SQLite" in why
    assert b"Make a database dump" not in excon.get("/settings/").content
    excon.post("/settings/dump/", {"action": "start"})
    assert dbdump.status()["state"] in ("none", "failed", "ready")


def test_dump_views_are_excon_only(dumps):
    c = Client()
    assert c.post("/settings/dump/", {"action": "start"}).status_code in (302, 403)
    assert c.get("/settings/dump/download/").status_code in (302, 403)
    assert dbdump.status()["state"] == "none"


def test_backup_has_every_model():
    missing = [x.__name__ for x in apps.get_app_config("ops").get_models() if x not in backup.MODELS]
    assert missing == [], "add these to backup.MODELS (and reset.py if they are activity)"


def test_backup_and_restore_commands(excon, tmp_path):
    from django.contrib.auth import get_user_model
    u = get_user_model().objects.get(username="undac")
    team = Team.objects.filter(code="a").first()
    chat = DeskChat.objects.create(user=u, team=team, title="Who is Kobo?")
    DeskChatTurn.objects.create(chat=chat, question="Who is Kobo?", answer="A persona.")
    people = Person.objects.count()
    f = tmp_path / "b.json"
    call_command("backup", str(f))
    data = json.loads(f.read_text())
    assert data["format"] == backup.FORMAT
    DeskChat.objects.all().delete()
    Person.objects.all().delete()
    conf.put("live_armed", "1")
    call_command("restore", str(f), "--yes")
    assert DeskChatTurn.objects.get().answer == "A persona."  # chats come back now
    assert Person.objects.count() == people
    assert conf.get("live_armed") == "0"


def test_restore_failure_changes_nothing(excon):
    people = Person.objects.count()
    data = json.loads(b"".join(excon.get("/settings/backup/").streaming_content))
    data["objects"].append({"model": "ops.person", "pk": 999999, "fields": {"team": 424242, "name": "Ghost"}})
    r = excon.post("/settings/restore/", {"confirm": "RESTORE", "file": SimpleUploadedFile("b.json", json.dumps(data).encode())},
                   follow=True)
    assert b"Restore failed, nothing changed" in r.content
    assert Person.objects.count() == people

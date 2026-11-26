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
from ops.models import Audit, DeskChat, DeskChatTurn, Person, Team


@pytest.fixture
def dumps(monkeypatch):
    monkeypatch.setattr(dbdump, "available", lambda: (True, ""))
    monkeypatch.setattr(dbdump, "database_size", lambda: 50 * 1024 * 1024)
    monkeypatch.setattr(dbdump, "command", lambda: [
        sys.executable, "-c", "import sys, os; sys.stdout.buffer.write(b'PGDMP' + os.environ.get('PGAPPNAME', '').encode())"])


def test_dump_streams_to_the_browser(excon, dumps):
    page = excon.get("/settings/").content
    assert b"Download database dump" in page and b"Gentle: 2 MB/s, about 1 min" in page
    r = excon.post("/settings/dump/", {"rate": "2"})
    assert r.status_code == 200 and r["Content-Disposition"].startswith('attachment; filename="simex-ops-')
    assert dbdump.running()["rate"] == 2  # one at a time, while it streams
    assert b"Being downloaded now" in excon.get("/settings/").content
    r2 = excon.post("/settings/dump/", {"rate": "2"})
    assert r2.status_code == 302 and dbdump.running()
    assert b"".join(r.streaming_content) == b"PGDMPsimex-ops dump"
    assert dbdump.running() is None
    assert list(Audit.objects.order_by("id").values_list("action", flat=True))[-2:] == ["dbdump_start", "dbdump_done"]


def test_a_dump_that_cannot_start_is_an_error_not_an_empty_file(excon, dumps, monkeypatch):
    monkeypatch.setattr(dbdump, "command", lambda: [
        sys.executable, "-c", "import sys; sys.stderr.write('pg_dump: error: connection refused'); sys.exit(1)"])
    r = excon.post("/settings/dump/", {"rate": "5"}, follow=True)
    assert b"The database dump failed: pg_dump: error: connection refused" in r.content
    assert dbdump.running() is None


def test_cancelling_the_download_stops_pg_dump(excon, dumps, monkeypatch):
    monkeypatch.setattr(dbdump, "command", lambda: [
        sys.executable, "-c", "import sys, time\nwhile True:\n    sys.stdout.buffer.write(b'x' * 65536); sys.stdout.flush()"])
    r = excon.post("/settings/dump/", {"rate": "15"})
    it = iter(r.streaming_content)
    next(it), next(it)
    r.close()  # the browser went away
    assert dbdump.running() is None
    assert Audit.objects.filter(action="dbdump_stopped").exists()


def test_dump_password_never_in_argv(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(dbdump, "settings", SimpleNamespace(DATABASES={"default": {
        "HOST": "db.example", "PORT": 25060, "USER": "simex_ops", "PASSWORD": "s3cret", "NAME": "simex_ops",
        "OPTIONS": {"sslmode": "require"}}}))
    env = dbdump._env()
    assert env["PGPASSWORD"] == "s3cret" and env["PGSSLMODE"] == "require" and env["PGPORT"] == "25060"
    assert not any("s3cret" in a for a in dbdump.command())


def test_the_stream_is_throttled(monkeypatch):
    import io
    clock = {"t": 0.0}
    monkeypatch.setattr(dbdump.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(dbdump.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    seen = []
    out = b"".join(dbdump.throttled(io.BytesIO(b"x" * (12 * 1024 * 1024)), 2, seen.append))
    assert len(out) == 12 * 1024 * 1024
    assert 5.9 <= clock["t"] <= 6.1  # 12 MB at 2 MB/s
    assert seen


def test_a_dump_whose_download_died_frees_the_slot():
    dbdump._set({"by": "undac", "touched_at": (timezone.now() - timedelta(minutes=5)).isoformat()})
    assert dbdump.running() is None


def test_health_check_never_waits_on_the_database(client, django_assert_num_queries):
    with django_assert_num_queries(0):
        assert client.get("/healthz").json() == {"ok": True}
    assert "scheduler_tick" in client.get("/healthz?db=1").json()


def test_dump_unavailable_on_sqlite(excon):
    ok, why = dbdump.available()
    assert not ok and "SQLite" in why
    assert b"Download database dump" not in excon.get("/settings/").content
    r = excon.post("/settings/dump/", {"rate": "5"}, follow=True)
    assert b"SQLite" in r.content


def test_dump_view_is_excon_only(dumps):
    assert Client().post("/settings/dump/", {"rate": "5"}).status_code in (302, 403)
    assert dbdump.running() is None


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

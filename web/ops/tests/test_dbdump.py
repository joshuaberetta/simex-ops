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
    monkeypatch.setattr(dbdump, "command", lambda: [
        sys.executable, "-c", "import sys, os; sys.stdout.buffer.write(b'PGDMP' + os.environ.get('PGAPPNAME', '').encode())"])
    return tmp_path / "dumps"


def test_dump_button_makes_a_file_to_download(excon, dumps):
    page = excon.get("/settings/").content
    assert b"Make a database dump" in page and b"Gentle: 2 MB/s" in page
    r = excon.post("/settings/dump/", {"action": "start", "rate": "2"})
    assert r.status_code == 302
    st = dbdump.status()
    assert st["state"] == "ready" and st["rate"] == 2 and st["size"] > 5
    n = Notice.objects.get(key=f"dbdump:{st['name']}")
    assert n.title == "Database dump ready" and n.url == "/settings/#dump"
    page = excon.get("/settings/").content
    assert b"Download database dump" in page and st["name"].encode() in page
    r = excon.get("/settings/dump/download/")
    body = b"".join(r.streaming_content)
    assert r.status_code == 200 and body == b"PGDMPsimex-ops dump"
    assert st["name"] in r["Content-Disposition"]
    assert set(Audit.objects.values_list("action", flat=True)) >= {"dbdump_start", "dbdump_download"}
    assert excon.get("/settings/dump/status/").json()["state"] == "ready"
    excon.post("/settings/dump/", {"action": "delete"})
    assert dbdump.status()["state"] == "none"
    assert excon.get("/settings/dump/download/").status_code == 404


def test_dump_failure_is_shown(excon, dumps, monkeypatch):
    monkeypatch.setattr(dbdump, "command", lambda: [
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
    assert not any("s3cret" in a for a in dbdump.command())


def test_the_copy_is_throttled(monkeypatch):
    import io
    clock = {"t": 0.0}
    monkeypatch.setattr(dbdump.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(dbdump.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    seen = []
    n, stopped = dbdump._copy(io.BytesIO(b"x" * (6 * 1024 * 1024)), io.BytesIO(), 2,
                              lambda b: seen.append(b) or True)
    assert n == 6 * 1024 * 1024 and not stopped
    assert 2.9 <= clock["t"] <= 3.1  # 6 MB at 2 MB/s
    assert seen  # progress reported (and Cancel checked) on the way
    clock["t"] = 0.0
    n, stopped = dbdump._copy(io.BytesIO(b"x" * (20 * 1024 * 1024)), io.BytesIO(), 2, lambda b: False)
    assert stopped and n < 10 * 1024 * 1024  # Cancel stops it


def test_running_stale_and_restarted(dumps, monkeypatch):
    now = timezone.now()
    base = {"name": "x.dump", "host": dbdump._host(), "started_at": now.isoformat(), "touched_at": now.isoformat()}
    dbdump._write({**base, "state": "running"})
    assert dbdump.status()["state"] == "running"
    with pytest.raises(RuntimeError):
        dbdump.start("undac")
    old = (now - timedelta(minutes=30)).isoformat()
    dbdump._write({**base, "state": "running", "touched_at": old})
    assert dbdump.status()["state"] == "failed"  # never finished: the process died
    dbdump._write({**base, "state": "running", "host": "another-container"})
    st = dbdump.status()
    assert st["state"] == "failed" and "restarted" in st["error"]
    dumps.mkdir(parents=True, exist_ok=True)
    (dumps / "x.dump").write_bytes(b"x")
    dbdump._write({**base, "state": "ready", "host": "another-container", "finished_at": now.isoformat()})
    assert dbdump.path() is None and "removed" in dbdump.status()["error"]
    dbdump._write({**base, "state": "ready", "finished_at": (now - timedelta(hours=4)).isoformat()})
    assert dbdump.status()["state"] == "none" and not (dumps / "x.dump").exists()  # kept 3 hours only


def test_cancel(excon, dumps, monkeypatch):
    killed = []
    monkeypatch.setattr(dbdump.os, "killpg", lambda pid, sig: killed.append(pid))
    now = timezone.now().isoformat()
    dbdump._write({"name": "x.dump", "host": dbdump._host(), "state": "running", "pid": 4242,
                   "started_at": now, "touched_at": now, "rate": 5, "size": 1024})
    page = excon.get("/settings/").content
    assert b"Cancel the dump" in page and b"Make a database dump" not in page
    excon.post("/settings/dump/", {"action": "cancel"})
    assert killed == [4242]
    st = dbdump.status()
    assert st["state"] == "failed" and st["error"] == "Cancelled."
    assert b"The last dump was cancelled" in excon.get("/settings/").content


def test_health_check_never_waits_on_the_database(client, django_assert_num_queries):
    with django_assert_num_queries(0):
        assert client.get("/healthz").json() == {"ok": True}
    assert "scheduler_tick" in client.get("/healthz?db=1").json()


def test_dump_unavailable_on_sqlite(excon):
    ok, why = dbdump.available()
    assert not ok and "SQLite" in why
    assert b"Make a database dump" not in excon.get("/settings/").content
    excon.post("/settings/dump/", {"action": "start"})
    assert dbdump.status()["state"] == "none"


def test_dump_views_are_excon_only(dumps):
    c = Client()
    assert c.post("/settings/dump/", {"action": "start"}).status_code in (302, 403)
    assert c.get("/settings/dump/download/").status_code in (302, 403)
    assert c.get("/settings/dump/status/").status_code in (302, 403)
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

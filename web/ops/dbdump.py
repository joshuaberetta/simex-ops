"""A pg_dump of the whole database, started from Settings and downloaded when ready.

The app is already among the database's trusted sources, so nobody has to open the cluster's
firewall to their own address. The dump runs as its own process (`manage.py dbdump`, started
detached so a web worker's restart doesn't stop it) and writes to a working directory on the
container's disk; the page and the download read the state file there, so every gunicorn
worker sees the same job. Restore it locally with pg_restore (docs/DEBRIEF.md).

The file holds participants' mail, logins and password hashes: ExCon only, audited, removed
after KEEP or when the next one starts, and gone anyway with the container (a deploy)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

DIR = Path(os.environ.get("DBDUMP_DIR") or Path(tempfile.gettempdir()) / "simex-dbdump")
KEEP = timedelta(hours=3)  # a finished dump is deleted after this
STALE = timedelta(minutes=10)  # a running dump whose file hasn't grown for this long has died
RUN_INLINE = False  # tests: run the job in this process instead of a detached one
PG_DUMP = "pg_dump"


def _state_path():
    return DIR / "state.json"


def _read():
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return None


def _write(state):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / "state.json.tmp"
    tmp.write_text(json.dumps(state))
    os.replace(tmp, _state_path())  # atomic: a reader never sees half a file


def available():
    """(True, "") when this app can dump its database, else (False, why not)."""
    if connection.vendor != "postgresql":
        return False, "The database isn't PostgreSQL (a local SQLite copy): use Download backup."
    if not shutil.which(PG_DUMP):
        return False, "pg_dump isn't installed in this image."
    return True, ""


def _now():
    return timezone.now()


def _iso(dt):
    return dt.isoformat()


def _parse(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def status():
    """The current job: {"state": none | running | ready | failed, ...}. Clears out old files."""
    s = _read()
    if not s:
        return {"state": "none"}
    if s.get("state") == "running":
        part = DIR / (s["name"] + ".part")
        try:
            touched = datetime.fromtimestamp(part.stat().st_mtime, tz=dt_timezone.utc)
        except OSError:
            touched = _parse(s.get("started_at"))
        if touched and _now() - touched > STALE:
            s.update(state="failed", error="The dump stopped (the process ended without finishing).",
                     finished_at=_iso(_now()))
            _write(s)
        else:
            try:
                s["size"] = part.stat().st_size
            except OSError:
                s["size"] = 0
    if s.get("state") in ("ready", "failed"):
        done = _parse(s.get("finished_at"))
        if done and _now() - done > KEEP:
            delete()
            return {"state": "none"}
    return s


def path():
    """The finished dump's path, or None."""
    s = status()
    if s.get("state") != "ready":
        return None
    p = DIR / s["name"]
    return p if p.exists() else None


def free_bytes():
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(DIR).free
    except OSError:
        return None


def delete():
    """Remove the dump and its state (not while one is running)."""
    if DIR.exists():
        for f in DIR.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)


def start(user=""):
    """Start a dump unless one is running. Returns the state."""
    ok, why = available()
    if not ok:
        raise RuntimeError(why)
    DIR.mkdir(parents=True, exist_ok=True)
    lock = DIR / "start.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # two clicks at once start one
    except FileExistsError:
        raise RuntimeError("A dump is being started already.")
    try:
        if status().get("state") == "running":
            raise RuntimeError("A dump is running already.")
        for f in DIR.iterdir():  # the last dump goes: only one is ever kept
            if f.is_file() and f != lock:
                f.unlink(missing_ok=True)
        name = f"simex-ops-{timezone.localtime():%Y%m%d-%H%M%S}.dump"
        state = {"state": "running", "name": name, "by": str(user or ""), "started_at": _iso(_now()), "size": 0}
        _write(state)
        if RUN_INLINE:
            run_job()
        else:
            log = open(DIR / "job.log", "ab")
            p = subprocess.Popen([sys.executable, str(Path(settings.BASE_DIR) / "manage.py"), "dbdump"],
                                 stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
                                 cwd=str(settings.BASE_DIR))
            log.close()
            threading.Thread(target=p.wait, daemon=True).start()  # reap it, so it never lingers as a zombie
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)
    return status()


def _env():
    """libpq's environment for the app's database: the password never appears in argv."""
    db = settings.DATABASES["default"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    pairs = {"PGHOST": db.get("HOST"), "PGPORT": db.get("PORT"), "PGUSER": db.get("USER"),
             "PGPASSWORD": db.get("PASSWORD"), "PGDATABASE": db.get("NAME"),
             "PGSSLMODE": (db.get("OPTIONS") or {}).get("sslmode")}
    env.update({k: str(v) for k, v in pairs.items() if v})
    return env


def command(out):
    """The pg_dump call (tests replace this)."""
    return [PG_DUMP, "--format=custom", "--no-owner", "--no-acl", "--file", str(out)]


def run_job():
    """Dump into <name>.part, then rename it and record the result (manage.py dbdump)."""
    from . import notices
    s = _read()
    if not s or s.get("state") != "running":
        return
    part, final = DIR / (s["name"] + ".part"), DIR / s["name"]
    try:
        r = subprocess.run(command(part), env=_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=3 * 3600)
        err = r.stderr.decode("utf-8", "replace").strip()
        ok = r.returncode == 0 and part.exists()
    except Exception as e:  # noqa: BLE001: whatever it was, the page must say so
        ok, err = False, str(e)
    if ok:
        os.replace(part, final)
        s.update(state="ready", size=final.stat().st_size, finished_at=_iso(_now()))
        _write(s)
        notices.add(f"dbdump:{s['name']}", "system", "Database dump ready",
                    body=f"{s['name']}, {s['size'] / 1e6:.1f} MB: download it from Settings.", url="/settings/#dump")
    else:
        part.unlink(missing_ok=True)
        s.update(state="failed", error=(err or "pg_dump failed.")[-1500:], finished_at=_iso(_now()))
        _write(s)
        notices.add(f"dbdump:{s['name']}", "system", "Database dump failed", body=s["error"][:500],
                    url="/settings/#dump")

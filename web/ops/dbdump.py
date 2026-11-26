"""A pg_dump of the whole database, started from Settings and downloaded when ready.

The app is already among the database's trusted sources, so nobody has to open the cluster's
firewall. The dump runs as its own process (`manage.py dbdump`, detached, so a web worker's
restart doesn't stop it) in the web container, writing to DIR on its disk.

The database is small and shared: pg_dump at full speed (the attachments are most of it)
swamped it, every query of the app slowed to a crawl and the platform restarted the web
container. So the job reads pg_dump's output at a capped rate (`rate`, MB/s, chosen on the
card): pg_dump blocks on its full pipe, stops reading its connection, and the server's COPY
waits on the socket, using next to nothing while it does. Cancel stops it at once.

The job's state is a Setting (`dbdump_state`, JSON), so every gunicorn worker and the job
itself see the same one and it survives a restart of the container: `host` says which
container holds the file, and a dump on another one (the app restarted, a deploy) is shown
as interrupted or gone. Restore it locally with pg_restore (docs/DEBRIEF.md).

The file holds participants' mail, logins and password hashes: ExCon only, audited, removed
after KEEP or when the next one starts, and gone anyway with the container."""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

DIR = Path(os.environ.get("DBDUMP_DIR") or Path(tempfile.gettempdir()) / "simex-dbdump")
KEY = "dbdump_state"
KEEP = timedelta(hours=3)  # a finished dump is deleted after this
STALE = timedelta(minutes=10)  # a running dump that hasn't written for this long has died
RATES = [(2, "Gentle: 2 MB/s"), (5, "Normal: 5 MB/s"), (15, "Fast: 15 MB/s (busier database)")]
DEFAULT_RATE = 5
CHUNK = 256 * 1024
REPORT_EVERY = 3  # seconds between progress updates (and checks for Cancel)
RUN_INLINE = False  # tests: run the job in this process instead of a detached one
PG_DUMP = "pg_dump"


def _host():
    return socket.gethostname()


def _read():
    from .models import Setting
    v = Setting.objects.filter(key=KEY).values_list("value", flat=True).first()
    try:
        return json.loads(v) if v else None
    except ValueError:
        return None


def _write(state):
    from .models import Setting
    Setting.objects.update_or_create(key=KEY, defaults={"value": json.dumps(state)})


def _clear():
    from .models import Setting
    Setting.objects.filter(key=KEY).delete()


def available():
    """(True, "") when this app can dump its database, else (False, why not)."""
    if connection.vendor != "postgresql":
        return False, "The database isn't PostgreSQL (a local SQLite copy): use Download backup."
    if not shutil.which(PG_DUMP):
        return False, "pg_dump isn't installed in this image."
    return True, ""


def database_size():
    """Bytes the database takes (about what the dump will be), or None."""
    if connection.vendor != "postgresql":
        return None
    try:
        with connection.cursor() as c:
            c.execute("SELECT pg_database_size(current_database())")
            return c.fetchone()[0]
    except Exception:  # noqa: BLE001: a figure for the page only
        return None


def _parse(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _files(name):
    return DIR / (name + ".part"), DIR / name


def status():
    """The current job: {"state": none | running | ready | failed, ...}. Marks a dump that
    died (or lived on another container) failed, and clears out old ones."""
    s = _read()
    if not s or not s.get("name"):
        return {"state": "none"}
    part, final = _files(s["name"])
    elsewhere = s.get("host") != _host()
    if s["state"] in ("running", "cancelling"):
        if elsewhere:
            return _fail(s, "Interrupted: the app restarted (a deploy or a restart) while it ran. Make a new one.")
        touched = _parse(s.get("touched_at")) or _parse(s.get("started_at"))
        if touched and timezone.now() - touched > STALE:
            return _fail(s, "The dump stopped (its process ended without finishing).")
        if part.exists():
            s["size"] = max(s.get("size") or 0, part.stat().st_size)
    elif s["state"] == "ready" and (elsewhere or not final.exists()):
        return _fail(s, "The finished dump was removed when the app restarted (a deploy or a restart). Make a new one.")
    if s["state"] in ("ready", "failed"):
        done = _parse(s.get("finished_at"))
        if done and timezone.now() - done > KEEP:
            delete()
            return {"state": "none"}
    return s


def _fail(s, why):
    s.update(state="failed", error=why, finished_at=timezone.now().isoformat())
    _write(s)
    return s


def path():
    """The finished dump's path, or None."""
    s = status()
    if s.get("state") != "ready":
        return None
    p = _files(s["name"])[1]
    return p if p.exists() else None


def free_bytes():
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(DIR).free
    except OSError:
        return None


def delete():
    """Remove the dump's files and its state (not while one runs)."""
    if DIR.exists():
        for f in DIR.iterdir():
            if f.is_file() and f.name != "start.lock":
                f.unlink(missing_ok=True)
    _clear()


def cancel():
    """Stop the running dump: its process group (the job and its pg_dump, which closes its
    connection, so the server's COPY ends too) and the part file."""
    s = _read()
    if not s or s.get("state") not in ("running", "cancelling"):
        return status()
    pid = s.get("pid")
    if pid and s.get("host") == _host():
        try:
            os.killpg(pid, signal.SIGTERM)  # its own session (start_new_session): never ours
        except OSError:
            pass
    _files(s["name"])[0].unlink(missing_ok=True)
    s.update(state="failed", error="Cancelled.", finished_at=timezone.now().isoformat())
    _write(s)
    return s


def start(user="", rate=DEFAULT_RATE):
    """Start a dump unless one is running. Returns the state."""
    ok, why = available()
    if not ok:
        raise RuntimeError(why)
    rate = rate if rate in dict(RATES) else DEFAULT_RATE
    DIR.mkdir(parents=True, exist_ok=True)
    lock = DIR / "start.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # two clicks at once start one
    except FileExistsError:
        raise RuntimeError("A dump is being started already.")
    try:
        if status().get("state") in ("running", "cancelling"):
            raise RuntimeError("A dump is running already.")
        delete()  # only one is ever kept
        now = timezone.now()
        name = f"simex-ops-{timezone.localtime(now):%Y%m%d-%H%M%S}.dump"
        state = {"state": "running", "name": name, "by": str(user or ""), "started_at": now.isoformat(),
                 "touched_at": now.isoformat(), "size": 0, "rate": rate, "host": _host(),
                 "db_size": database_size()}
        _write(state)
        if RUN_INLINE:
            run_job()
        else:
            log = open(DIR / "job.log", "ab")
            p = subprocess.Popen([sys.executable, str(Path(settings.BASE_DIR) / "manage.py"), "dbdump"],
                                 stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
                                 cwd=str(settings.BASE_DIR))
            log.close()
            state["pid"] = p.pid  # its process group too (start_new_session), for Cancel
            _write(state)
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
             "PGSSLMODE": (db.get("OPTIONS") or {}).get("sslmode"),
             "PGAPPNAME": "simex-ops dump"}
    env.update({k: str(v) for k, v in pairs.items() if v})
    return env


def command():
    """The pg_dump call, writing to stdout (tests replace this)."""
    return [PG_DUMP, "--format=custom", "--no-owner", "--no-acl", "--lock-wait-timeout=60000"]


def _copy(src, dst, rate_mb, on_progress):
    """src -> dst at no more than rate_mb MB/s; on_progress(bytes) every REPORT_EVERY seconds,
    returning False to stop. Returns (bytes, stopped)."""
    limit = rate_mb * 1024 * 1024
    t0 = last = time.monotonic()
    n = 0
    while True:
        chunk = src.read(CHUNK)
        if not chunk:
            return n, False
        dst.write(chunk)
        n += len(chunk)
        ahead = n / limit - (time.monotonic() - t0)
        if ahead > 0:
            time.sleep(ahead)  # pg_dump's pipe fills, it stops reading, the server's COPY waits
        if time.monotonic() - last >= REPORT_EVERY:
            last = time.monotonic()
            if on_progress(n) is False:
                return n, True


def run_job():
    """Dump into <name>.part at the chosen rate, then rename it and record the result
    (manage.py dbdump)."""
    from . import notices
    s = _read()
    if not s or s.get("state") != "running":
        return
    name = s["name"]
    part, final = _files(name)
    DIR.mkdir(parents=True, exist_ok=True)

    def mine():
        """The state now, if it is still this dump's."""
        cur = _read() or {}
        return cur if cur.get("name") == name else None

    def progress(n):
        cur = mine()
        if not cur or cur.get("state") != "running":
            return False  # cancelled (or replaced)
        cur.update(size=n, touched_at=timezone.now().isoformat())
        _write(cur)
        return True

    err, stopped, ok, n = "", False, False, 0
    p = None
    try:
        p = subprocess.Popen(command(), env=_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        errbuf = []
        reader = threading.Thread(target=lambda: errbuf.append(p.stderr.read()), daemon=True)
        reader.start()
        with open(part, "wb") as out:
            n, stopped = _copy(p.stdout, out, float(s.get("rate") or DEFAULT_RATE), progress)
        if stopped:
            p.terminate()
        rc = p.wait(timeout=60)
        reader.join(timeout=5)
        err = (errbuf[0] if errbuf else b"").decode("utf-8", "replace").strip()
        ok = not stopped and rc == 0 and n > 0
    except Exception as e:  # noqa: BLE001: whatever it was, the page must say so
        err = str(e)
        if p and p.poll() is None:
            p.kill()
    cur = mine()
    if not cur or cur.get("state") != "running":  # cancelled or replaced: cancel() wrote the state
        part.unlink(missing_ok=True)
        return
    now = timezone.now().isoformat()
    if ok:
        os.replace(part, final)
        cur.update(state="ready", size=final.stat().st_size, finished_at=now, touched_at=now)
        _write(cur)
        notices.add(f"dbdump:{name}", "system", "Database dump ready",
                    body=f"{name}, {cur['size'] / 1e6:.1f} MB: download it from Settings.", url="/settings/#dump")
        return
    part.unlink(missing_ok=True)
    cur.update(state="failed", error=(err or "pg_dump failed.")[-1500:], finished_at=now)
    _write(cur)
    notices.add(f"dbdump:{name}", "system", "Database dump failed", body=cur["error"][:500], url="/settings/#dump")

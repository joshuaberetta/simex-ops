"""A pg_dump of the whole database, streamed straight to ExCon's browser from Settings.

The app is already among the database's trusted sources, so nobody has to open the cluster's
firewall. Nothing is written on the server: the web container's filesystem is held in memory
(a 2 GB dump written there got the container killed for memory, twice), so pg_dump's output goes
through the response as it comes, and the browser's download shows how far it has got.
Cancelling the download stops pg_dump.

The database is small and shared: pg_dump at full speed swamped it, so the stream is capped
(`RATES`, MB/s, chosen on the card): pg_dump blocks on its full pipe, stops reading its
connection, and the server's COPY waits on the socket, using next to nothing meanwhile. One
dump at a time (`dbdump_running`, a Setting). gunicorn runs gthread workers, so a download of
any length isn't killed by the worker timeout. Restore it locally with pg_restore
(docs/DEBRIEF.md). The file holds participants' mail, logins and password hashes: ExCon only,
audited."""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta

from django.conf import settings
from django.db import connection
from django.utils import timezone

KEY = "dbdump_running"
STALE = timedelta(minutes=2)  # a running dump that hasn't written for this long has gone
RATES = [(2, "Gentle: 2 MB/s"), (5, "Normal: 5 MB/s"), (15, "Fast: 15 MB/s (busier database)")]
DEFAULT_RATE = 5
CHUNK = 256 * 1024
REPORT_EVERY = 5  # seconds between updates of the running dump's Setting
PG_DUMP = "pg_dump"


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


def running():
    """The dump being downloaded now ({by, started_at, touched_at, size, rate}), or None."""
    from .models import Setting
    v = Setting.objects.filter(key=KEY).values_list("value", flat=True).first()
    try:
        s = json.loads(v) if v else None
    except ValueError:
        s = None
    if not s:
        return None
    touched = _parse(s.get("touched_at"))
    if not touched or timezone.now() - touched > STALE:
        return None  # its download ended without saying so (the app restarted)
    return s


def _set(state):
    from .models import Setting
    if state is None:
        Setting.objects.filter(key=KEY).delete()
    else:
        Setting.objects.update_or_create(key=KEY, defaults={"value": json.dumps(state)})


def filename():
    return f"simex-ops-{timezone.localtime():%Y%m%d-%H%M}.dump"


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


def throttled(src, rate_mb, on_progress=None):
    """Chunks of src at no more than rate_mb MB/s; on_progress(bytes) every REPORT_EVERY s."""
    limit = rate_mb * 1024 * 1024
    t0 = last = time.monotonic()
    n = 0
    while True:
        chunk = src.read(CHUNK)
        if not chunk:
            return
        n += len(chunk)
        yield chunk
        ahead = n / limit - (time.monotonic() - t0)
        if ahead > 0:
            time.sleep(ahead)  # pg_dump's pipe fills, it stops reading, the server's COPY waits
        if on_progress and time.monotonic() - last >= REPORT_EVERY:
            last = time.monotonic()
            on_progress(n)


def begin(user="", rate=DEFAULT_RATE):
    """Claim the one dump slot; raises RuntimeError if another is running."""
    ok, why = available()
    if not ok:
        raise RuntimeError(why)
    cur = running()
    if cur:
        raise RuntimeError(f"A dump is being downloaded already (started by {cur.get('by') or 'someone'}).")
    now = timezone.now().isoformat()
    state = {"by": str(user or ""), "started_at": now, "touched_at": now, "size": 0,
             "rate": rate, "host": socket.gethostname()}
    _set(state)
    return state


def stream(state):
    """pg_dump's output, throttled, for a StreamingHttpResponse. Closing it (the browser
    cancelled, the connection dropped) stops pg_dump, which closes its database connection."""
    from . import models
    p = subprocess.Popen(command(), env=_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    errbuf = []
    reader = threading.Thread(target=lambda: errbuf.append(p.stderr.read()), daemon=True)
    reader.start()

    def progress(n):
        state.update(size=n, touched_at=timezone.now().isoformat())
        _set(state)

    n = 0
    finished = False
    try:
        for chunk in throttled(p.stdout, float(state.get("rate") or DEFAULT_RATE), progress):
            n += len(chunk)
            yield chunk
        rc = p.wait(timeout=60)
        reader.join(timeout=5)
        finished = True
        err = (errbuf[0] if errbuf else b"").decode("utf-8", "replace").strip()
        detail = f"{n} bytes, {state.get('rate')} MB/s"
        if rc != 0:
            state["error"] = err or f"pg_dump exited with {rc}"
        if rc != 0 and n:
            # The browser already has a file; it is cut short. Say so where ExCon will look.
            from . import notices
            notices.add(f"dbdump-failed:{state['started_at']}", "system", "Database dump failed",
                        body=(err or f"pg_dump exited with {rc}")[-500:], url="/settings/#dump")
            detail += f", failed: {(err or rc)!s:.300}"
        models.audit("dbdump_done", detail, state.get("by"))
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        if not finished:
            models.audit("dbdump_stopped", f"{n} bytes: the download was cancelled or dropped", state.get("by"))
        _set(None)

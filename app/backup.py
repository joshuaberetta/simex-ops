"""Whole-database backup as one JSON file (attachments base64-encoded), and restore into
the configuration tables. The dev database on App Platform has no automatic backups."""

import base64
from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, text

from .db import Attachment, Audit, Inject, Message, Person, Sender, Setting, Team, TestRun, TestRunItem, audit

# Restored in this order (foreign keys); logs are exported but not restored.
RESTORE = [Setting, Sender, Team, Person, Inject, Attachment]
EXPORT = RESTORE + [TestRun, TestRunItem, Message, Audit]


def _row(obj):
    out = {}
    for c in obj.__table__.columns:
        v = getattr(obj, c.key)
        if isinstance(v, datetime):
            v = v.isoformat()
        elif isinstance(v, bytes):
            v = base64.b64encode(v).decode()
        out[c.key] = v
    return out


def export(s):
    return {"format": "simex-ops-backup-1", "exported_at": datetime.utcnow().isoformat(),
            "tables": {m.__tablename__: [_row(o) for o in s.query(m).all()] for m in EXPORT}}


def restore(s, data):
    """Replace settings, senders, teams, people, injects and attachments with the backup's.
    Returns row counts. Logs, test runs and imports are cleared."""
    if data.get("format") != "simex-ops-backup-1":
        raise ValueError("Not a simex-ops backup file.")
    tables = data["tables"]
    for m in reversed(EXPORT):
        s.query(m).delete()
    s.flush()
    counts = {}
    for m in RESTORE:
        rows = tables.get(m.__tablename__, [])
        for r in rows:
            kw = {}
            for c in m.__table__.columns:
                if c.key not in r:
                    continue
                v = r[c.key]
                if v is not None and isinstance(c.type, DateTime):
                    v = datetime.fromisoformat(v)
                elif v is not None and isinstance(c.type, LargeBinary):
                    v = base64.b64decode(v)
                kw[c.key] = v
            s.add(m(**kw))
        s.flush()
        counts[m.__tablename__] = len(rows)
    if s.bind.dialect.name == "postgresql":  # move id sequences past the restored ids
        for m in RESTORE:
            if "id" in m.__table__.columns:
                t = m.__tablename__
                s.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), COALESCE((SELECT MAX(id) FROM {t}), 0) + 1, false)"))
    # Never come back armed: restoring must not start live sending by itself.
    st = s.get(Setting, "live_armed")
    if st:
        st.value = "0"
    audit(s, "restore", str(counts))
    return counts

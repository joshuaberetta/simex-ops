"""Operator settings stored in the database (editable on the Settings tab)."""

from sqlalchemy import select

from .db import Setting

DEFAULTS = {
    "footer": "EXERCISE EXERCISE EXERCISE\nThis message is part of the UNDAC SIMEX. It does not describe a real emergency.",
    "subject_prefix": "",
    "default_reply_to": "",  # where participants' replies go unless a sender or inject overrides it
    "excon_bcc": "",  # copy of every live email, comma-separated
    "test_address": "",  # default address for "Send test"
    "test_redirect": "delivered+{key}@resend.dev",  # default for test runs
    "grace_minutes": "10",  # a scheduled inject later than this is marked overdue, not sent
    "live_armed": "0",
    "paused": "0",
}


def get(s, key):
    # A column select always reads the database (s.get() could return a stale cached row
    # in a long-running send, e.g. miss a pause).
    value = s.execute(select(Setting.value).where(Setting.key == key)).scalar()
    return value if value is not None else DEFAULTS.get(key, "")


def get_all(s):
    out = dict(DEFAULTS)
    for key, value in s.execute(select(Setting.key, Setting.value)):
        if value is not None:
            out[key] = value
    return out


def put(s, key, value):
    row = s.get(Setting, key)
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))


def flag(s, key):
    return get(s, key) == "1"

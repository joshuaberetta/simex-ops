"""Settings that come from the environment (deployment config and secrets).

Everything an operator may change during the exercise (footer, reply-to, arming live
sending...) lives in the database instead; see settings.py.
"""

import os
from zoneinfo import ZoneInfo


def _env(key, default=None):
    v = os.environ.get(key)
    return v if v not in (None, "") else default


def database_url():
    url = _env("DATABASE_URL", "sqlite:///./simex-ops.db")
    # DigitalOcean hands out postgresql://...; SQLAlchemy needs the driver name.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


APP_USERNAME = _env("APP_USERNAME", "undac")
APP_PASSWORD = _env("APP_PASSWORD")  # required in production; see main.py
SECRET_KEY = _env("SECRET_KEY")
RESEND_API_KEY = _env("RESEND_API_KEY")
# Every sender address is <username>@MAIL_DOMAIN; the domain must be verified in Resend.
MAIL_DOMAIN = _env("MAIL_DOMAIN", "simex.kobolabs.dev")
PUBLIC_URL = _env("PUBLIC_URL", "http://127.0.0.1:8000")
TZ_NAME = _env("EXERCISE_TZ", "Africa/Nairobi")
TZ = ZoneInfo(TZ_NAME)
# Seconds between scheduler ticks, and minimum gap between Resend API calls.
TICK_SECONDS = float(_env("TICK_SECONDS", "15"))
RESEND_MIN_INTERVAL = float(_env("RESEND_MIN_INTERVAL", "0.6"))
# "resend" sends for real; "log" only records what would have been sent (local dev, tests).
MAILER = _env("MAILER", "resend" if RESEND_API_KEY else "log")
SCHEDULER_ENABLED = _env("SCHEDULER_ENABLED", "1") == "1"
SECURE_COOKIES = PUBLIC_URL.startswith("https://")

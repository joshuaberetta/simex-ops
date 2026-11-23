"""Django settings for SIMEX inbox. Deployment config and secrets come from the environment;
everything ExCon changes during the exercise lives in the database (ops.conf)."""

import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def env(key, default=None):
    v = os.environ.get(key)
    return v if v not in (None, "") else default


PUBLIC_URL = env("PUBLIC_URL", "http://127.0.0.1:8000")  # production: https://ops.simex.kobolabs.dev
PRODUCTION = PUBLIC_URL.startswith("https://")
SECRET_KEY = env("SECRET_KEY") or ("" if PRODUCTION else "dev-only-secret")
if not SECRET_KEY:
    raise SystemExit("SECRET_KEY must be set in production.")
DEBUG = env("DEBUG", "0" if PRODUCTION else "1") == "1"


def _hostname(url):
    return url.split("://", 1)[-1].split("/")[0].split(":")[0]


# Participants' SIMEX Mail on its own host, served by this same app (production:
# https://mail.simex.kobolabs.dev; ops.middleware.MailHost splits the paths). Unset: all on PUBLIC_URL.
MAIL_URL = (env("MAIL_URL") or "").rstrip("/")
MAIL_HOST = _hostname(MAIL_URL) if MAIL_URL else ""
_host = _hostname(PUBLIC_URL)
ALLOWED_HOSTS = [h for h in env("ALLOWED_HOSTS", f"{_host},{MAIL_HOST},localhost,127.0.0.1,testserver").split(",") if h]
CSRF_TRUSTED_ORIGINS = [u for u in (PUBLIC_URL.rstrip("/"), MAIL_URL) if u]

# The ExCon superuser created by `manage.py seed_simex` (and on every start in Docker).
APP_USERNAME = env("APP_USERNAME", "undac")
APP_PASSWORD = env("APP_PASSWORD", None if PRODUCTION else "nairobi2026")

# Email channel (optional): Resend, like the FastAPI app.
RESEND_API_KEY = env("RESEND_API_KEY")
RESEND_WEBHOOK_SECRET = env("RESEND_WEBHOOK_SECRET")
MAIL_DOMAIN = env("MAIL_DOMAIN", "simex.kobolabs.dev")
MAILER = env("MAILER", "resend" if RESEND_API_KEY else "log")
RESEND_MIN_INTERVAL = float(env("RESEND_MIN_INTERVAL", "0.6"))
TICK_SECONDS = float(env("TICK_SECONDS", "10"))

# The source code (AGPL-3.0, which asks that network users are offered it), linked in the app's footers.
SOURCE_URL = env("SOURCE_URL", "https://github.com/joshuaberetta/simex-ops")

# Push notifications for new SIMEX Mail (optional, ops/push.py): a VAPID key pair, made with
# `manage.py vapid_keys`. Only the private key is set; the public key is derived from it.
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY")
VAPID_SUBJECT = env("VAPID_SUBJECT", f"mailto:excon@{MAIL_DOMAIN}")

# Claude analysis of replies (optional, ops/ai.py): "anthropic" (ANTHROPIC_API_KEY), "bedrock"
# (AWS credentials: AWS_BEARER_TOKEN_BEDROCK, or a profile / SSO session locally) or "off".
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
AI_PROVIDER = env("AI_PROVIDER") or ("anthropic" if ANTHROPIC_API_KEY else
                                     "bedrock" if env("AWS_BEARER_TOKEN_BEDROCK") else "off")
AI_REGION = env("AWS_REGION", "us-east-1")
AI_MODEL_FAST = env("AI_MODEL_FAST")  # default: Claude Haiku 5.5 (us.anthropic.claude-haiku-5-5 on Bedrock)
AI_MODEL_DEEP = env("AI_MODEL_DEEP")  # default: Claude Sonnet 5.5

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "ops",
]

MIDDLEWARE = [
    "ops.middleware.HealthCheck",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "ops.middleware.MailHost",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "ops.middleware.LastSeen",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "simex.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "ops.context.status",
        "ops.context.source",
        "ops.context.sim",
    ]},
}]
WSGI_APPLICATION = "simex.wsgi.application"

DATABASES = {"default": dj_database_url.parse(env("DATABASE_URL", f"sqlite:///{BASE_DIR / 'simex-inbox.db'}"),
                                              conn_max_age=60)}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = []  # exercise accounts; passwords are generated
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = env("EXERCISE_TZ", "Africa/Nairobi")
USE_I18N = False
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {"staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage" if PRODUCTION
                            else "django.contrib.staticfiles.storage.StaticFilesStorage"}}

DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 5000

SESSION_COOKIE_AGE = 14 * 24 * 3600
# ops. and mail. share one session (cookie on their parent domain), so signing in on either works
# for both. Its own cookie name, so host-only cookies from before can't shadow it.
_parent = _host.split(".", 1)[-1]
if MAIL_HOST and MAIL_HOST != _host and _parent.count(".") and MAIL_HOST.endswith("." + _parent):
    SESSION_COOKIE_DOMAIN, SESSION_COOKIE_NAME = _parent, "simex_session"
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = PRODUCTION
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = "SAMEORIGIN"  # email previews are sandboxed iframes on our own pages

LOGGING = {"version": 1, "disable_existing_loggers": False,
           "handlers": {"console": {"class": "logging.StreamHandler"}},
           "root": {"handlers": ["console"], "level": "INFO"}}

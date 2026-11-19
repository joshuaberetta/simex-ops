import os

os.environ.setdefault("MAILER", "log")
os.environ.setdefault("RESEND_API_KEY", "")
os.environ.setdefault("RESEND_WEBHOOK_SECRET", "whsec_dGVzdC1zZWNyZXQtMTIzNDU2")

import pytest  # noqa: E402
from django.core.management import call_command  # noqa: E402


@pytest.fixture(autouse=True)
def _setup(db, settings):
    settings.RESEND_WEBHOOK_SECRET = "whsec_dGVzdC1zZWNyZXQtMTIzNDU2"
    settings.STORAGES = {"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
    from ops import mailer
    mailer.set_mailer(mailer.LogMailer())
    settings.AI_PROVIDER = "off"  # never call a real model (a developer's AWS profile would work)
    from ops import ai
    ai.set_client(None)
    ai.RUN_INLINE = True
    call_command("seed_simex", verbosity=0)
    # Most tests exercise each inject's own channel; the global switch has its own tests.
    from ops import conf
    conf.put("channel_mode", "per_inject")


@pytest.fixture
def excon(client):
    client.login(username="undac", password="nairobi2026")
    return client

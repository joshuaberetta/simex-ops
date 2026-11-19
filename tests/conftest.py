import os
import sys
from pathlib import Path

# Configure before the app is imported: throwaway SQLite file, log mailer, no background loop.
DB = Path(__file__).parent / "test.db"
os.environ.update(DATABASE_URL=f"sqlite:///{DB}", MAILER="log", SCHEDULER_ENABLED="0", APP_PASSWORD="nairobi2026",
                  SECRET_KEY="test", RESEND_API_KEY="",
                  RESEND_WEBHOOK_SECRET="whsec_dGVzdC1zZWNyZXQtMTIzNDU2")
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app import db, main, mailer
    db.Base.metadata.drop_all(db.engine)
    mailer.set_mailer(mailer.LogMailer())
    with TestClient(main.app) as c:
        r = c.post("/login", data={"username": "undac", "password": "nairobi2026"}, follow_redirects=False)
        assert r.status_code == 303
        yield c

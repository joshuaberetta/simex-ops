import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from app import db, importer, mailer, render, scheduler, settings
from app.db import Inject, Message, Person, SessionLocal, Team, TestRunItem, utcnow

SAMPLE = [
    {"inject_id": "1-15", "method": "Email", "sender": "UNDAC-{team}-TL@epeigonia.org", "recipients": "Each team",
     "subject": "Update for {team_name}", "content": "Hello {team_name}", "send_date": "2026-11-23T00:00:00",
     "send_time": "09:00", "mailbox": "teamleader", "supporting_documents": "1-15 Plan.pdf",
     "expected_outcomes": "SECRET OUTCOME", "story": "SECRET STORY", "description": "Task: SECRET"},
    {"inject_id": "3-13", "method": "Email", "sender": "donotreply@epeigonia.org", "recipients": "All teams (Bcc)",
     "subject": "News", "content": "Article", "send_date": "2026-11-24T00:00:00", "send_time": "14:30"},
    {"inject_id": "1-27", "method": "F2F", "content": "Customs visit", "sender": None},
]


def do_import(c, items, overwrite=False):
    r = c.post("/import", files={"file": ("injects.json", json.dumps(items).encode(), "application/json")})
    assert r.status_code == 200, r.text
    bid = re.search(r"/import/(\d+)/apply", r.text).group(1)
    return c.post(f"/import/{bid}/apply", data={"overwrite": "1"} if overwrite else {})


def real_roster():
    with SessionLocal() as s:
        for t in s.query(Team).all():
            s.add(Person(name=f"Real {t.code}", email=f"real-{t.code}@kobotoolbox.org", team_id=t.id))
        settings.put(s, "default_reply_to", "excon@kobotoolbox.org")
        s.commit()


def test_login_required(client):
    client.post("/logout")
    assert client.get("/injects", follow_redirects=False).status_code == 303
    bad = client.post("/login", data={"username": "undac", "password": "nope"})
    assert bad.status_code == 401


def test_import_drops_control_fields_and_non_email(client):
    do_import(client, SAMPLE)
    with SessionLocal() as s:
        injs = {i.mel_id: i for i in s.query(Inject)}
        assert set(injs) == {"1-15", "3-13"}
        i = injs["1-15"]
        blob = json.dumps({c.name: str(getattr(i, c.name)) for c in Inject.__table__.columns})
        assert "SECRET" not in blob
        assert i.sender_local == "undac-{team}-tl" and i.delivery == "each_team" and i.status == "scheduled"
        assert i.send_at.isoformat() == "2026-11-23T06:00:00"  # 09:00 Nairobi = 06:00 UTC
        assert injs["3-13"].delivery == "all_bcc"


def test_render_each_team_fills_placeholders(client):
    do_import(client, SAMPLE)
    real_roster()
    with SessionLocal() as s:
        inj = s.query(Inject).filter_by(mel_id="1-15").one()
        drafts, warnings = render.build(s, inj, settings.get_all(s))
    assert len(drafts) == 4
    d = drafts[0]
    assert d.from_addr == '"UNDAC Team A TL" <undac-a-tl@simex.kobolabs.dev>'
    assert d.subject == "Update for Team A" and "Hello Team A" in d.text
    assert "EXERCISE EXERCISE EXERCISE" in d.text
    assert "real-a@kobotoolbox.org" in d.to and any("placeholder" in w for w in d.warnings)


def test_all_bcc_goes_to_reply_to(client):
    do_import(client, SAMPLE)
    real_roster()
    with SessionLocal() as s:
        inj = s.query(Inject).filter_by(mel_id="3-13").one()
        (d,), _ = render.build(s, inj, settings.get_all(s))
    assert d.to == ["excon@kobotoolbox.org"] and len(d.bcc) >= 4


def test_live_send_needs_arming_and_skips_placeholders(client):
    do_import(client, SAMPLE)
    real_roster()
    with SessionLocal() as s:
        inj = s.query(Inject).filter_by(mel_id="1-15").one()
        inj.send_at = utcnow() - timedelta(minutes=1)
        s.commit()
    scheduler.tick()
    log = mailer.get()
    assert log.sent == []  # disarmed
    with SessionLocal() as s:
        settings.put(s, "live_armed", "1")
        s.commit()
    scheduler.tick()
    assert len(log.sent) == 4
    for payload, key in log.sent:
        assert all("placeholder" not in a for a in payload["to"]) and key.startswith("live-")
    with SessionLocal() as s:
        assert s.query(Inject).filter_by(mel_id="1-15").one().status == "sent"
    scheduler.tick()
    assert len(log.sent) == 4  # never twice


def test_overdue_not_sent(client):
    do_import(client, SAMPLE)
    real_roster()
    with SessionLocal() as s:
        settings.put(s, "live_armed", "1")
        inj = s.query(Inject).filter_by(mel_id="1-15").one()
        inj.send_at = utcnow() - timedelta(hours=2)
        s.commit()
    scheduler.tick()
    assert mailer.get().sent == []
    with SessionLocal() as s:
        assert s.query(Inject).filter_by(mel_id="1-15").one().status == "overdue"


def test_pause_stops_everything(client):
    do_import(client, SAMPLE)
    real_roster()
    with SessionLocal() as s:
        settings.put(s, "live_armed", "1")
        settings.put(s, "paused", "1")
        s.query(Inject).filter_by(mel_id="1-15").one().send_at = utcnow()
        s.commit()
    scheduler.tick()
    assert mailer.get().sent == []


def test_test_run_redirects_everyone(client):
    do_import(client, SAMPLE)
    real_roster()
    r = client.post("/tests", data={"name": "R1", "scope": "all", "timing": "interval", "interval": "5",
                                    "redirect": "josh+{key}@kobotoolbox.org"})
    assert r.status_code == 200
    with SessionLocal() as s:
        for it in s.query(TestRunItem):
            it.due_at = utcnow() - timedelta(seconds=1)
        s.commit()
    scheduler.tick()
    sent = mailer.get().sent
    assert len(sent) == 5  # 4 team emails + 1 bcc email
    for payload, key in sent:
        addrs = payload["to"] + payload.get("cc", []) + payload.get("bcc", [])
        assert all(a.startswith("josh+") and a.endswith("@kobotoolbox.org") for a in addrs), addrs
        assert payload["subject"].startswith("[TEST R1]")
        assert "would go to" in payload["text"]
    with SessionLocal() as s:
        assert {i.status for i in s.query(TestRunItem)} == {"sent"}
        assert s.query(Inject).filter_by(mel_id="1-15").one().status == "scheduled"  # untouched


def test_dry_run_sends_nothing(client):
    do_import(client, SAMPLE)
    client.post("/tests", data={"name": "D", "scope": "ids", "ids": "1-15", "timing": "interval", "interval": "5",
                                "redirect": "x+{key}@kobotoolbox.org", "dry_run": "1"})
    with SessionLocal() as s:
        for it in s.query(TestRunItem):
            it.due_at = utcnow()
        s.commit()
    scheduler.tick()
    assert mailer.get().sent == []
    with SessionLocal() as s:
        assert {m.status for m in s.query(Message)} == {"dry"}


def test_send_test_to_me(client):
    do_import(client, SAMPLE)
    r = client.post("/injects/1/test", data={"address": "me@kobotoolbox.org"})
    assert "Test sent to me@kobotoolbox.org" in r.text
    payload, _ = mailer.get().sent[0]
    assert payload["to"] == ["me@kobotoolbox.org"]


def test_resume_after_crash_reuses_idempotency_keys(client):
    do_import(client, SAMPLE)
    real_roster()
    with SessionLocal() as s:
        settings.put(s, "live_armed", "1")
        inj = s.query(Inject).filter_by(mel_id="1-15").one()
        inj.send_at = utcnow()
        s.commit()

    class Boom(mailer.LogMailer):
        def send(self, payload, key):
            if len(self.sent) == 2:
                raise KeyboardInterrupt  # simulate the process dying mid-send
            return super().send(payload, key)
    m = Boom()
    mailer.set_mailer(m)
    with pytest.raises(KeyboardInterrupt):
        scheduler.tick()
    scheduler._busy.clear()
    first_keys = [k for _, k in m.sent]
    m2 = mailer.LogMailer()
    mailer.set_mailer(m2)
    scheduler.tick()
    keys2 = [k for _, k in m2.sent]
    assert len(first_keys) == 2 and len(keys2) == 2 and not set(first_keys) & set(keys2)
    with SessionLocal() as s:
        assert s.query(Inject).filter_by(mel_id="1-15").one().status == "sent"


def test_reimport_keeps_local_edits(client):
    do_import(client, SAMPLE)
    with SessionLocal() as s:
        inj = s.query(Inject).filter_by(mel_id="1-15").one()
        inj.subject = "My edit"
        s.commit()
    changed = [dict(x) for x in SAMPLE]
    changed[0]["subject"] = "MEL v2 subject"
    changed[0]["content"] = "MEL v2 body"
    do_import(client, changed)
    with SessionLocal() as s:
        inj = s.query(Inject).filter_by(mel_id="1-15").one()
        assert inj.subject == "My edit" and inj.body == "MEL v2 body"
    do_import(client, changed, overwrite=True)
    with SessionLocal() as s:
        assert s.query(Inject).filter_by(mel_id="1-15").one().subject == "MEL v2 subject"


MEL = Path("/Users/josh/code/undac-simex/extracted/UNDAC Induction Course - MASTER COURSE PACKAGE/"
           "5. SIMEX Master 2025/MEL-MIL.xlsx")
JSON = Path("/Users/josh/code/undac-simex/registers/injects.json")


@pytest.mark.skipif(not MEL.exists() or not JSON.exists(), reason="exercise files not on this machine")
def test_xlsx_and_json_import_agree():
    a = importer.parse_upload("MEL-MIL.xlsx", MEL.read_bytes())
    b = importer.parse_upload("injects.json", JSON.read_bytes())
    strip = lambda rs: [{k: v for k, v in r.items() if k != "seq"} for r in rs]  # noqa: E731
    assert strip(a) == strip(b)
    assert len(a) == 103


def test_backup_roundtrip(client):
    do_import(client, SAMPLE)
    client.post("/injects/1/attachments", files={"files": ("plan.pdf", b"%PDF-1 test", "application/pdf")})
    data = client.get("/settings/backup").json()
    client.post("/injects/1/delete", data={"confirm": "1-15"})
    r = client.post("/settings/restore", data={"confirm": "RESTORE"},
                    files={"file": ("b.json", json.dumps(data).encode(), "application/json")})
    assert "Restored" in r.text
    with SessionLocal() as s:
        inj = s.query(Inject).filter_by(mel_id="1-15").one()
        assert inj.attachments[0].data == b"%PDF-1 test"


def test_attachments_are_sent(client):
    do_import(client, SAMPLE)
    client.post("/injects/1/attachments", files={"files": ("1-15 Plan.pdf", b"%PDF-1 plan", "application/pdf")})
    client.post("/injects/1/test", data={"address": "me@kobotoolbox.org"})
    payload, _ = mailer.get().sent[0]
    import base64
    assert payload["attachments"] == [{"filename": "1-15 Plan.pdf",
                                       "content": base64.b64encode(b"%PDF-1 plan").decode()}]

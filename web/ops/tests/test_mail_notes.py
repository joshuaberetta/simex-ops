"""Notes on mail and SIMEX AI's reading of a conversation (ops/mail_notes.py, views_notes.py):
who writes and reads them where, the reading and its updates, the lists and exports."""

import pytest

from ops import ai, conf, mail_notes, reset
from ops.models import Mail, MailNote, MailReview, ThreadReview, Team
from ops.tests.test_ai import FakeClient
from ops.tests.test_desk import FETCH, desk_client, setup, team_writes
from ops.tests.test_leaders import leader
from ops.tests.test_ops import deliver_now, participant


def reading(kw):
    text = kw["messages"][0]["content"][0]["text"]
    refs = [int(x.split("]")[0]) for x in text.split("--- [")[1:] if "ASSESS" in x.split("\n")[0]]
    return {"summary": "Team A asked for the road status; nobody answered yet.", "open": ["Road status"],
            "assessments": [{"ref": r, "verdict": "poor", "assessment": "No greeting, vague subject.",
                             "issues": ["Vague subject line"]} for r in refs] + [{"ref": 999, "verdict": "good",
                                                                                  "assessment": "x", "issues": []}]}, "end_turn"


@pytest.fixture
def fake():
    c = FakeClient(reading)
    ai.set_client(c)
    conf.put("ai_enabled", "1")
    conf.put("mail_review", "1")
    return c


def world(excon):
    setup(excon)
    deliver_now("3-13")
    ta = Team.objects.get(code="a")
    pa = participant("a", "Jane")
    got, _ = team_writes(pa, ta, "road?")
    return ta, pa, got


def test_desk_notes_beside_an_email_and_who_reads_them(excon):
    ta, pa, got = world(excon)
    d = desk_client()
    desk_mail = Mail.objects.get(team=None, folder="inbox")
    page = d.get(f"/inbox/desk/t/{got.thread}/")
    assert page.status_code == 200 and b"data-nt-side" in page.content
    url = f"/inbox/desk/t/{got.thread}/notes/"
    r = d.post(url, {"act": "add", "mail": desk_mail.id, "body": "Poorly structured email, no body"}, headers=FETCH)
    assert r.status_code == 200 and "Poorly structured" in r.json()["html"]
    n = MailNote.objects.get()
    assert (n.scope, n.team, n.about_team, n.author_name) == ("desk", None, ta, "Anna Desk")
    # Another desk member reads it, can't change it; ExCon may delete it.
    e = desk_client("Ben Desk", "ben.desk")
    assert "Poorly structured" in e.get(url).json()["html"]
    assert e.post(url, {"act": "edit", "note": n.id, "body": "x"}).status_code == 403
    assert d.post(url, {"act": "edit", "note": n.id, "body": "Better"}).status_code == 200
    assert MailNote.objects.get().body == "Better" and MailNote.objects.get().edited_at
    # Never the teams, never their leader.
    assert b"data-nt-side" not in pa.get(f"/inbox/t/{got.thread}/").content
    assert pa.get("/inbox/desk/notes/").status_code == 403
    la = leader("a")
    assert b"Better" not in la.get(f"/inbox/team/t/{got.thread}/").content
    assert la.get("/inbox/notes/").status_code == 200 and b"Better" not in la.get("/inbox/notes/").content
    # The lists
    assert b"Better" in d.get("/inbox/desk/notes/").content
    assert b"Better" in excon.get("/notes/").content
    excon.post(url, {"act": "delete", "note": n.id})
    assert not MailNote.objects.exists()


def test_leader_notes_in_read_only_mailboxes(excon):
    ta, pa, got = world(excon)
    la = leader("a")
    team_mail = Mail.objects.get(team=ta, owner=None, folder="sent")
    page = la.get(f"/inbox/team/t/{got.thread}/")
    assert b"data-nt-side" in page.content
    r = la.post(f"/inbox/team/t/{got.thread}/notes/", {"act": "add", "mail": team_mail.id, "body": "Unprofessional subject"})
    assert r.status_code == 200
    assert MailNote.objects.get().scope == "leader"
    # The team's mail is untouched by it, and still can't be sent from there.
    assert la.post("/inbox/team/send/", {"to": "p:cnn", "body": "x"}).status_code in (403, 404, 405)
    # ExCon reads it viewing the team; another team's leader never.
    assert b"Unprofessional subject" in excon.get(f"/inbox/as/{ta.id}/t/{got.thread}/").content
    lb = leader("b")
    assert lb.get(f"/inbox/leaders/{ta.id}/t/{got.thread}/notes/").status_code in (403, 404)
    assert b"Unprofessional" not in lb.get("/inbox/notes/").content
    # a note only on a mail of that mailbox
    other = Mail.objects.get(team=None, folder="inbox")
    assert la.post(f"/inbox/team/t/{got.thread}/notes/", {"act": "add", "mail": other.id, "body": "x"}).status_code == 404
    # Markdown export, of the conversation and of the list
    md = la.get(f"/inbox/team/t/{got.thread}/notes/?format=md")
    assert md["Content-Type"].startswith("text/markdown") and b"Unprofessional subject" in md.content and b"road?" in md.content
    md = la.get("/inbox/notes/?format=md")
    assert b"Unprofessional subject" in md.content and b"road?" in md.content
    assert b"Unprofessional subject" in la.get("/inbox/notes/?format=csv").content


def test_reading_queued_on_open_and_again_on_new_mail(excon, fake):
    ta, pa, got = world(excon)
    conf.put("ai_auto", "0")
    d = desk_client()
    d.get(f"/inbox/desk/t/{got.thread}/")
    tr = ThreadReview.objects.get(box="desk")
    assert tr.status == "queued"
    assert ai.tick() >= 1
    tr.refresh_from_db()
    assert tr.status == "done" and "road status" in tr.summary and tr.open_points == ["Road status"]
    desk_mail = Mail.objects.get(team=None, folder="inbox")
    rev = MailReview.objects.get(mail=desk_mail)
    assert rev.verdict == "poor" and rev.issues == ["Vague subject line"]
    j = d.get(f"/inbox/desk/t/{got.thread}/notes/").json()
    assert "No greeting" in j["html"] and "road status" in j["html"] and not j["pending"]
    # Nothing new: not read again.
    d.get(f"/inbox/desk/t/{got.thread}/")
    assert ThreadReview.objects.get(box="desk").status == "done"
    # The team writes again: read again on open, only the new email assessed.
    team_copy = Mail.objects.filter(team=ta, owner=None, folder="inbox").order_by("-id").first()
    pa.post("/inbox/send/", {"to": "p:donotreply", "subject": "Re: News", "body": "And tomorrow?", "in_reply_to": team_copy.id})
    d.get(f"/inbox/desk/t/{got.thread}/")
    assert ThreadReview.objects.get(box="desk").status == "queued"
    fake.calls.clear()
    ai.tick()
    text = fake.calls[0]["messages"][0]["content"][0]["text"]
    assert text.count("ASSESS") == 1 and "And tomorrow?" in text
    assert MailReview.objects.filter(mail__team=None).count() == 2
    # Members never see it; their leader does, in the team inbox (its own reading).
    assert "No greeting" not in pa.get(f"/inbox/t/{got.thread}/").content.decode()
    # Start afresh: gone.
    reset.start_afresh()
    assert not ThreadReview.objects.exists() and not MailReview.objects.exists()


def test_automatic_readings_and_the_switch(excon, fake):
    ta, pa, got = world(excon)
    while ai.tick():  # a few calls a pass: the reply's analysis too
        pass
    boxes = set(ThreadReview.objects.values_list("box", flat=True))
    assert "desk" in boxes and str(ta.id) in boxes  # the desk and the team inbox
    assert all(r.status == "done" for r in ThreadReview.objects.all())
    n = len(fake.calls)
    while ai.tick():
        pass
    assert len(fake.calls) == n  # nothing new
    conf.put("mail_review", "0")
    pa.post("/inbox/send/", {"to": "p:donotreply", "subject": "Again", "body": "Hello?"})
    assert mail_notes.tick(5) == 0


def test_notes_routes_are_closed_to_members_and_anonymous(excon, client):
    ta, pa, got = world(excon)
    for url in (f"/inbox/t/{got.thread}/notes/", f"/inbox/me/t/{got.thread}/notes/", "/inbox/notes/", "/notes/",
                "/inbox/desk/notes/", f"/inbox/desk/t/{got.thread}/notes/"):
        assert pa.get(url).status_code in (403, 404), url
    client.logout()
    assert client.get("/notes/").status_code == 302

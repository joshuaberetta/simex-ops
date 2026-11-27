"""Files (ops/file_index.py): every file of the exercise in one list for the persona desk, a
team leader (their team only) and ExCon, copies merged, linked back, never an Event."""

from django.core.files.uploadedfile import SimpleUploadedFile

from ops import conf
from ops.models import DocumentAnalysis, Event, Mail, MailAttachment, Team
from ops.tests.test_desk import desk_client
from ops.tests.test_leaders import leader
from ops.tests.test_ops import SAMPLE, arm, deliver_now, do_import, participant


def _setup(excon):
    do_import(excon, SAMPLE)
    arm()
    deliver_now("1-15")
    deliver_now("3-13")
    pa = participant("a", "Jane")
    pb = participant("b", "Omar")
    mail = Mail.objects.get(owner=None, team__code="a", inject__mel_id="3-13")
    r = pa.post("/inbox/send/", {"to": "p:donotreply", "subject": "Re: News", "body": "Our plan", "in_reply_to": mail.id,
                                 "files": SimpleUploadedFile("plan.docx", b"PK plan a")}, headers={"x-requested-with": "fetch"})
    assert r.status_code == 200, r.content
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Internal", "body": "For the leader",
                             "files": SimpleUploadedFile("notes.txt", b"inside")}, headers={"x-requested-with": "fetch"})
    mb = Mail.objects.get(owner=None, team__code="b", inject__mel_id="3-13")
    pb.post("/inbox/send/", {"to": "p:donotreply", "subject": "Re: News", "body": "B's", "in_reply_to": mb.id,
                             "files": SimpleUploadedFile("b-photo.png", b"\x89PNG\r\n\x1a\nxx")},
            headers={"x-requested-with": "fetch"})
    return pa, pb


def _names(resp):
    return sorted(r["filename"] for _, rows, _ in resp.context["groups"] for r in rows)


def test_files_lists_for_desk_leader_and_excon(excon):
    _setup(excon)
    ta = Team.objects.get(code="a")
    desk = desk_client()
    page = desk.get("/inbox/desk/files/")
    assert page.status_code == 200
    assert _names(page) == ["b-photo.png", "notes.txt", "plan.docx"]
    rows = {r["filename"]: r for _, rs, _ in page.context["groups"] for r in rs}
    plan = rows["plan.docx"]
    # The team inbox's Sent, Jane's own Sent and the desk's Inbox copy: one row.
    assert plan["direction"] == "to_persona" and len(plan["boxes"]) >= 2 and plan["url"].startswith(f"/inbox/as/{ta.id}")
    assert rows["notes.txt"]["direction"] == "internal" and rows["b-photo.png"]["type"] == "image"
    assert "ai" not in plan  # the desk never sees SIMEX AI's readings
    # Inject documents only when asked; grouped by team; the filters.
    assert _names(desk.get(f"/inbox/desk/files/?team={ta.id}")) == ["notes.txt", "plan.docx"]
    assert _names(desk.get("/inbox/desk/files/?dir=internal")) == ["notes.txt"]
    assert _names(desk.get("/inbox/desk/files/?type=image&group=none")) == ["b-photo.png"]
    assert _names(desk.get("/inbox/desk/files/?q=photo")) == ["b-photo.png"]
    csv = desk.get("/inbox/desk/files/?format=csv").content.decode()
    assert "plan.docx" in csv and "SIMEX AI" not in csv

    # A team leader: their team's only, with SIMEX AI's reading.
    la = leader("a")
    a = MailAttachment.objects.filter(filename="plan.docx").first()
    DocumentAnalysis.objects.create(team=ta, sha256=a.sha256, filename="plan.docx", status="done", doc_type="Plan",
                                    summary="Shelter plan", ai_written=80)
    lp = la.get("/inbox/files/")
    assert lp.status_code == 200 and _names(lp) == ["notes.txt", "plan.docx"]
    lrow = next(r for _, rs, _ in lp.context["groups"] for r in rs if r["filename"] == "plan.docx")
    assert lrow["band"] == "likely" and lrow["url"].startswith("/inbox/team/")
    assert _names(la.get("/inbox/files/?band=likely")) == ["plan.docx"]
    b_file = MailAttachment.objects.filter(filename="b-photo.png").first()
    assert la.get(f"/inbox/files/m/{b_file.id}/").status_code == 404  # never another team's
    got = la.get(f"/inbox/files/m/{a.id}/")
    assert got.status_code == 200 and got.content == b"PK plan a"
    assert desk.get(f"/inbox/desk/files/m/{b_file.id}/?inline=1")["Content-Type"] == "image/png"
    assert desk.get(f"/inbox/desk/files/m/{a.id}/?inline=1").status_code == 404  # not a picture
    assert not Event.objects.filter(type="download").exists()

    # ExCon: everything, the zip.
    ex = excon.get("/files/")
    assert ex.status_code == 200 and _names(ex) == ["b-photo.png", "notes.txt", "plan.docx"]
    z = excon.get("/files/?format=zip")
    assert z.status_code == 200 and b"".join(z.streaming_content)[:2] == b"PK"
    assert excon.get(f"/files/get/m/{a.id}/").content == b"PK plan a"


def test_files_are_for_desk_leaders_and_excon_only(excon):
    pa, _ = _setup(excon)
    conf.put("personal_mailboxes", "1")
    a = MailAttachment.objects.filter(filename="plan.docx").first()
    for url in ("/inbox/desk/files/", "/inbox/files/", "/files/", f"/inbox/desk/files/m/{a.id}/", f"/inbox/files/m/{a.id}/",
                f"/files/get/m/{a.id}/"):
        assert pa.get(url).status_code in (302, 403, 404), url

"""Rich message bodies in SIMEX Mail (ops/richtext.py, web/docs/RICH_MAIL_PLAN.md): formatting
and images in the text travel with every copy, are cleaned on the way in and out, and go out
by email as inline (cid) images."""

from django.core.files.uploadedfile import SimpleUploadedFile

from ops import mailer
from ops.models import Mail, MailAttachment, Message, Team
from ops.tests.test_desk import FETCH, desk_client, setup, team_writes
from ops.tests.test_ops import deliver_now, participant

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = ('<p>We need <b>the road status</b> <i>today</i>.</p><script>alert(1)</script>'
        '<p><img src="blob:https://mail/x" data-cid="img-1" alt="map"></p><img src="https://evil/p.gif">')


def image(data=PNG, name="map.png", ctype="image/png"):
    return SimpleUploadedFile(name, data, content_type=ctype)


def test_a_member_writes_with_formatting_and_an_image(excon):
    setup(excon)
    ta = Team.objects.get(code="a")
    deliver_now("3-13")
    pa = participant("a")
    got = Mail.objects.get(team=ta, owner=None, folder="inbox")
    r = pa.post("/inbox/send/", {"to": "p:donotreply", "subject": "Re: News", "body": "fallback", "html": HTML,
                                 "in_reply_to": got.id, "inline": [image()], "inline_cid": ["img-1"]})
    assert r.status_code == 302
    sent = Mail.objects.get(team=ta, owner=None, folder="sent")
    assert "<b>the road status</b>" in sent.html and 'src="cid:img-1"' in sent.html
    assert "script" not in sent.html and "evil" not in sent.html and "blob:" not in sent.html
    assert sent.body == "We need the road status today.\n\n[image: map]"  # the text, for search and SIMEX AI
    f = sent.files.get()
    assert (f.cid, f.content_type, bytes(f.data)) == ("img-1", "image/png", PNG) and not sent.attached
    # The desk's copy has it too.
    box = Mail.objects.get(team=None, folder="inbox")
    assert box.html == sent.html and box.files.get().cid == "img-1"
    # Shown in the conversation, the image from its file, not listed as an attachment.
    page = pa.get(f"/inbox/t/{sent.thread}/?folder=sent").content.decode()
    assert f'<img src="/inbox/file/{f.id}/?inline=1" data-cid="img-1" alt="map">' in page
    assert "1 attachment" not in page and "<script>alert" not in page
    shown = pa.get(f"/inbox/file/{f.id}/?inline=1")
    assert shown["Content-Type"] == "image/png" and shown["Content-Disposition"] == "inline"
    assert pa.get(f"/inbox/file/{f.id}/")["Content-Type"] == "application/octet-stream"  # a download, as before
    # Another team never gets it.
    pb = participant("b")
    assert pb.get(f"/inbox/file/{f.id}/?inline=1").status_code == 404


def test_not_an_image_is_left_out_and_plain_text_stays_plain(excon):
    setup(excon)
    ta = Team.objects.get(code="a")
    deliver_now("3-13")
    pa = participant("a")
    got = Mail.objects.get(team=ta, owner=None, folder="inbox")
    pa.post("/inbox/send/", {"to": "p:donotreply", "subject": "Re: News", "html": HTML, "in_reply_to": got.id,
                             "inline": [image(SVG, "x.svg", "image/svg+xml")], "inline_cid": ["img-1"]})
    sent = Mail.objects.get(team=ta, owner=None, folder="sent")
    assert "<img" not in sent.html and not sent.files.exists()
    pa.post("/inbox/send/", {"to": "p:donotreply", "subject": "Plain", "body": "x",
                             "html": "<div>Just text</div><div><br></div><div>more</div>"})
    plain = Mail.objects.get(team=ta, owner=None, folder="sent", subject="Plain")
    assert plain.html == "" and plain.body == "Just text\n\nmore"


def test_a_draft_keeps_its_images(excon):
    setup(excon)
    ta = Team.objects.get(code="a")
    deliver_now("3-13")
    pa = participant("a")
    r = pa.post("/inbox/draft/", {"to": "p:donotreply", "subject": "Draft", "html": HTML, "inline": [image()],
                                  "inline_cid": ["img-1"]})
    did = r.json()["id"]
    d = pa.get(f"/inbox/draft/{did}/").json()
    f = MailAttachment.objects.get(mail_id=did)
    assert f'src="/inbox/file/{f.id}/?inline=1"' in d["html"] and 'data-cid="img-1"' in d["html"]
    # Saved again without the image uploaded: kept. Sent: the Sent copy has it.
    pa.post("/inbox/draft/", {"draft_id": did, "to": "p:donotreply", "subject": "Draft", "html": d["html"]})
    assert MailAttachment.objects.filter(mail_id=did, cid="img-1").count() == 1
    pa.post("/inbox/send/", {"draft_id": did, "to": "p:donotreply", "subject": "Draft", "html": d["html"]})
    sent = Mail.objects.get(team=ta, owner=None, folder="sent", subject="Draft")
    assert sent.files.get().cid == "img-1" and 'src="cid:img-1"' in sent.html
    assert not Mail.objects.filter(pk=did).exists()
    # The image removed from the text: gone from the draft too.
    r = pa.post("/inbox/draft/", {"to": "p:donotreply", "subject": "D2", "html": HTML, "inline": [image()],
                                  "inline_cid": ["img-1"]})
    pa.post("/inbox/draft/", {"draft_id": r.json()["id"], "to": "p:donotreply", "subject": "D2", "html": "<p><b>x</b></p>"})
    assert not MailAttachment.objects.filter(mail_id=r.json()["id"]).exists()


def test_the_desk_answers_with_formatting_in_app_and_by_email(excon):
    setup(excon)
    ta = Team.objects.get(code="a")
    deliver_now("3-13")
    pa = participant("a")
    team_writes(pa, ta)
    d = desk_client()
    box = Mail.objects.get(team=None, folder="inbox")
    r = d.post("/inbox/desk/send/", {"from": "p:donotreply", "to": f"t:{ta.id}", "subject": "Re: News", "html": HTML,
                                     "in_reply_to": box.id, "inline": [image()], "inline_cid": ["img-1"]}, headers=FETCH)
    assert r.json()["ok"], r.json()
    m = Message.objects.get(kind="reply")
    assert "<b>the road status</b>" in m.body_html and m.files.get().cid == "img-1"
    got = Mail.objects.get(team=ta, owner=None, folder="inbox", message=m)
    assert got.html == m.body_html and got.files.get().cid == "img-1" and not got.attached
    assert Mail.objects.get(team=None, folder="sent", message=m).files.get().cid == "img-1"
    # By email: the HTML part has the image inline, by its content id.
    assert 'src="cid:img-1"' in m.html and "<b>the road status</b>" in m.html
    p = mailer.payload(m, list(m.files.all()))
    assert p["attachments"][0]["content_id"] == "img-1" and p["attachments"][0]["content_type"] == "image/png"


def test_participants_read_it_in_every_mailbox(excon):
    """A formatted message with a picture from the desk to a team: every member reads it formatted,
    picture shown, in the team inbox and their own mailbox; so does the leader, read-only."""
    from ops.tests.test_leaders import leader
    setup(excon)
    ta = Team.objects.get(code="a")
    jane, john = participant("a", "Jane"), participant("a", "John")
    tl = leader("a")
    d = desk_client()
    r = d.post("/inbox/desk/send/", {"from": "p:donotreply", "to": f"t:{ta.id}", "subject": "Road map", "html": HTML,
                                     "inline": [image()], "inline_cid": ["img-1"]}, headers=FETCH)
    assert r.json()["ok"], r.json()
    team = Mail.objects.get(team=ta, owner=None, folder="inbox", subject="Road map")
    for c, base, owner in ((jane, "/inbox", None), (john, "/inbox", None), (jane, "/inbox/me", jane.person),
                           (john, "/inbox/me", john.person), (tl, "/inbox/team", None)):
        m = Mail.objects.get(team=ta, owner=owner, folder="inbox", subject="Road map")
        f = m.files.get()
        page = c.get(f"{base}/t/{m.thread}/").content.decode()
        assert "<b>the road status</b>" in page and f'<img src="{base}/file/{f.id}/?inline=1"' in page, (base, owner)
        assert "<script>alert" not in page
        shown = c.get(f"{base}/file/{f.id}/?inline=1")
        assert shown.status_code == 200 and shown["Content-Type"] == "image/png", (base, owner)
    assert team.html and "&lt;b&gt;" not in team.html
    # Another team's member never reads it.
    pb = participant("b")
    assert pb.get(f"/inbox/file/{team.files.get().id}/?inline=1").status_code == 404

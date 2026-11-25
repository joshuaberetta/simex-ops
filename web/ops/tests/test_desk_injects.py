"""The persona desk's Injects tab (ops/views_desk_injects.py): read-only, what is coming and what
went, for the desk and ExCon only."""

from django.utils import timezone

from ops import conf
from ops.models import Attachment, Inject, Mail, Team
from ops.tests.test_desk import desk_client, setup
from ops.tests.test_leaders import leader
from ops.tests.test_ops import deliver_now, participant


def schedule(mel_id, minutes=30):
    inj = Inject.objects.get(mel_id=mel_id)
    inj.send_at, inj.status = timezone.now() + timezone.timedelta(minutes=minutes), "scheduled"
    inj.save()
    return inj


def test_desk_sees_what_is_coming_and_what_it_says(excon):
    setup(excon)
    inj = schedule("1-15")
    Attachment.objects.create(inject=inj, filename="1-15 Plan.pdf", content_type="application/pdf", size=4, data=b"%PDF")
    participant("a")  # a member with an email address: never shown to the desk
    d = desk_client()
    r = d.get("/inbox/desk/injects/")
    assert r.status_code == 200
    html = r.content.decode()
    assert "Update for {team_name}" in html and "1-15" in html and "Next" in html and "in 30 min" in html
    assert 'href="/inbox/desk/injects/"' in html  # the tab
    assert "SECRET" not in html
    r = d.get(f"/inbox/desk/injects/{inj.id}/")
    html = r.content.decode()
    assert r.status_code == 200
    assert "Update for Team A" in html and "Hello Team B" in html  # each team's version, names filled
    assert "1-15 Plan.pdf" in html and "real-a@kobotoolbox.org" not in html and "SECRET" not in html
    a = inj.attachments.get()
    got = d.get(f"/inbox/desk/injects/{inj.id}/files/{a.id}/")
    assert got.status_code == 200 and got.content == b"%PDF"
    other = Inject.objects.exclude(pk=inj.pk).first()
    assert d.get(f"/inbox/desk/injects/{other.id}/files/{a.id}/").status_code == 404
    assert d.get(f"/inbox/desk/injects/?show=sent").content.decode().count("Update for {team_name}") == 0


def test_sent_injects_link_to_the_desk_conversation(excon):
    setup(excon)
    inj = deliver_now("3-13")
    d = desk_client()
    html = d.get("/inbox/desk/injects/?show=sent").content.decode()
    assert "News" in html and "3-13" in html
    html = d.get(f"/inbox/desk/injects/{inj.id}/").content.decode()
    thread = Mail.objects.filter(team=None, owner=None, folder="sent", message__inject=inj).first().thread
    assert f"/inbox/desk/t/{thread}/" in html
    # the conversation on the desk links back to the inject
    page = d.get(f"/inbox/desk/t/{thread}/").content.decode()
    assert f"/inbox/desk/injects/{inj.id}/" in page


def test_filters_by_persona_and_team(excon):
    setup(excon)
    schedule("1-15")
    i = schedule("3-13", 60)
    tb = Team.objects.get(code="b")
    i.team_ids = [tb.id]
    i.save()
    d = desk_client()
    ta = Team.objects.get(code="a")
    html = d.get(f"/inbox/desk/injects/?team={ta.id}").content.decode()
    assert "1-15" in html and "3-13" not in html
    html = d.get("/inbox/desk/injects/?persona=donotreply").content.decode()
    assert "3-13" in html and "Update for {team_name}" not in html
    assert "News" in d.get("/inbox/desk/injects/?q=article").content.decode()


def test_only_the_desk_and_excon_and_only_when_on(excon):
    setup(excon)
    inj = schedule("1-15")
    assert excon.get("/inbox/desk/injects/").status_code == 200
    for c in (participant("a"), leader("a")):
        assert c.get("/inbox/desk/injects/").status_code == 403
        assert c.get(f"/inbox/desk/injects/{inj.id}/").status_code == 403
    d = desk_client()
    conf.put("desk_injects", "0")
    assert d.get("/inbox/desk/injects/").status_code == 404
    assert d.get(f"/inbox/desk/injects/{inj.id}/").status_code == 404
    assert 'href="/inbox/desk/injects/"' not in d.get("/inbox/desk/").content.decode()

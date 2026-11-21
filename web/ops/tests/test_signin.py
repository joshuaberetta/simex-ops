"""QR sign-in: the login slips' codes sign a member or team leader in on any device, set it up,
and stop working when the password is reset or the login disabled; never for ExCon."""

from django.contrib.auth import get_user_model
from django.test import Client

from ops import signin
from ops.models import Person
from ops.tests.test_leaders import leader
from ops.tests.test_ops import participant


def test_qr_signs_in_and_sets_up_the_device():
    pa = participant("a", "Amina")
    user = pa.person.user
    c = Client()
    r = c.get(signin.path(user))
    assert r.status_code == 302 and r["Location"] == "/inbox/setup/"
    page = c.get("/inbox/setup/")
    assert page.status_code == 200 and b"Welcome, Amina" in page.content and b"data-setup-alerts" in page.content
    assert page.context["app_url"].startswith("/inbox/go/") and page.context["app_url"].endswith("?app=1")
    assert c.get("/inbox/").context["v"].team.code == "a"
    # The same slip on a second device.
    assert Client().get(signin.path(user))["Location"] == "/inbox/setup/"
    # A leader's opens their own mailbox.
    la = leader("b")
    c2 = Client()
    c2.get(signin.path(la.person.user))
    assert c2.get("/inbox/").context["v"].owner == la.person


def test_qr_stops_working():
    pa = participant("a")
    user = pa.person.user
    old = signin.path(user)
    user.set_password("new-one")
    user.save()
    c = Client()
    r = c.get(old, follow=True)
    assert r.redirect_chain[-1][0].startswith("/login/") and b"no longer works" in r.content
    assert Client().get(signin.path(user))["Location"] == "/inbox/setup/"  # a new slip works
    tok = signin.token(user)
    user.is_active = False
    user.save()
    assert signin.user_for(tok) is None
    user.is_active = True
    user.save()
    assert signin.user_for(tok[:-2] + "xx") is None  # tampered
    signin.TOKEN_DAYS, days = -1, signin.TOKEN_DAYS
    try:
        assert signin.user_for(signin.token(user)) is None  # expired
    finally:
        signin.TOKEN_DAYS = days
    Person.objects.filter(pk=pa.person.pk).update(active=False)
    assert signin.user_for(signin.token(user)) is None


def test_never_for_excon(excon):
    staff = get_user_model().objects.get(username="undac")
    assert not signin.can_use(staff) and signin.user_for(signin.token(staff)) is None
    assert Client().get(signin.path(staff))["Location"] == "/inbox/"


def test_home_screen_app_and_sign_out():
    pa = participant("a")
    user = pa.person.user
    app = signin.path(user) + "?app=1"
    js = pa.get("/inbox/manifest.webmanifest").json()
    assert js["start_url"].startswith("/inbox/go/") and js["start_url"].endswith("?app=1") and js["id"] == "/inbox/"
    assert Client().get("/inbox/manifest.webmanifest").json()["start_url"] == "/inbox/"
    assert pa.get(app)["Location"] == "/inbox/"  # already signed in: straight to the mailbox
    c = Client()
    assert c.get(app)["Location"] == "/inbox/setup/"  # an iPhone's app: its own cookies, signs in once
    c.post("/logout/")
    assert c.cookies["simex_signed_out"].value == "1"
    assert c.get(app)["Location"] == "/inbox/" and c.get("/inbox/")["Location"].startswith("/login/")  # stays out
    assert c.get(signin.path(user))["Location"] == "/inbox/setup/"  # scanning the slip again signs in
    assert not c.cookies["simex_signed_out"].value


def test_slips(excon):
    pa = participant("a", "Amina")
    leader("a")
    r = excon.get("/people/slips/?team=" + str(pa.person.team_id))
    html = r.content.decode()
    assert r.status_code == 200 and "Amina" in html and "team leader" in html and html.count("<svg") == 2
    assert "Password: the one you were given" in html
    r = excon.post("/people/credentials/", {"rows": f'[["Amina", "Team A", "{pa.person.user.username}", "pw-1"]]'})
    assert b"<svg" in r.content and b"pw-1" in r.content
    r = excon.post("/people/credentials/", {"rows": '[["Nobody", "", "nobody", "pw"]]'})
    assert b"<svg" not in r.content
    assert pa.get("/people/slips/").status_code == 403
    assert b"/people/slips/?person=" in excon.get("/people/").content

from django.contrib.auth import get_user_model
from django.utils import timezone

from ops import activity, signin
from ops.models import LoginEvent, MailAction, Person, Team
from ops.views_excon import ensure_account


def _member():
    team = Team.objects.filter(is_test=False).first()
    p = Person.objects.create(name="Jane Doe", team=team, role="member")
    pw = ensure_account(p)
    return p, pw


def test_password_signin_failure_and_signout_are_logged(client):
    p, pw = _member()
    client.post("/login/", {"username": p.user.username, "password": "wrong"})
    assert LoginEvent.objects.filter(person=p, kind="failed").count() == 1
    client.post("/login/", {"username": p.user.username, "password": pw}, HTTP_USER_AGENT="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1")
    ev = LoginEvent.objects.get(person=p, kind="signin")
    assert ev.method == "password" and activity.device(ev.user_agent) == "iPhone · Safari"
    p.refresh_from_db()
    assert p.last_seen_at and activity.online(p)
    assert activity.summary([p])[p.id] == {"signins": 1, "failed": 0}
    client.post("/logout/")
    assert LoginEvent.objects.filter(person=p, kind="signout").exists()


def test_qr_signin_is_logged_as_qr(client):
    p, _ = _member()
    client.get(f"/inbox/go/{signin.token(p.user)}/")
    assert LoginEvent.objects.get(person=p, kind="signin").method == "qr"


def test_excon_and_unknown_users_not_logged(client, excon):
    client.post("/login/", {"username": "nobody", "password": "x"})
    assert not LoginEvent.objects.exists()


def test_people_page_and_activity_page(excon):
    p, _ = _member()
    LoginEvent.objects.create(person=p, username=p.user.username, kind="signin", method="qr", user_agent="Mozilla/5.0 (Macintosh) Chrome/120 Safari/537")
    MailAction.objects.create(team=p.team, person=p, action="search", detail={"q": "cholera"})
    get_user_model().objects.filter(pk=p.user_id).update(last_login=timezone.now())
    r = excon.get("/people/")
    assert f"/people/{p.id}/activity/" in r.content.decode()
    r = excon.get(f"/people/{p.id}/activity/")
    body = r.content.decode()
    assert r.status_code == 200 and "Signed in with the QR code" in body and "cholera" in body and "Mac · Chrome" in body
    assert "cholera" not in excon.get(f"/people/{p.id}/activity/?show=signin").content.decode()


def test_activity_page_is_excon_only(client):
    p, pw = _member()
    client.post("/login/", {"username": p.user.username, "password": pw})
    assert client.get(f"/people/{p.id}/activity/").status_code in (302, 403)

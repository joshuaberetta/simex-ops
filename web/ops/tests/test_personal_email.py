"""Personal emails: in-app injects also emailed to people's personal addresses, login emails
(new password, QR code) that mark a login shared, and the roster import that links them."""

import base64
import io
import re

import openpyxl
from django.contrib.auth import authenticate
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from ops import conf, delivery, inbound, logins, mailer, render, signin
from ops.models import Inject, Mail, Message, Person, Team
from ops.tests.test_ops import arm, participant


def _inject(**kw):
    return Inject.objects.create(**{"mel_id": "P-1", "sender_local": "cnn", "subject": "Hello {team_name}",
                                    "body": "Body", "delivery": "each_team", "channel": "inapp", **kw})


def test_inject_also_by_email_goes_to_personal_addresses(excon):
    a = participant("a", "Amina").person
    b = participant("b", "Bo").person
    Person.objects.filter(pk=a.pk).update(personal_email="amina@home.org")
    Person.objects.filter(pk=b.pk).update(personal_email="bo@example.org")  # a placeholder: skipped live
    inj = _inject(also_email=True, team_ids=[a.team_id, b.team_id])
    drafts, warnings = render.build(inj, conf.get_all())
    assert [(d.channel, d.target) for d in drafts] == [
        ("inapp", "Team A"), ("inapp", "Team B"),
        ("email", "Team A: Amina · personal email"), ("email", "Team B: Bo · personal email")]
    assert drafts[2].to == ["amina@home.org"] and drafts[2].from_addr.endswith("<cnn@simex.kobolabs.dev>")
    arm()
    assert delivery.start_live(inj) is None
    delivery.process_live(inj.id)
    inj.refresh_from_db()
    assert inj.status == "partial"  # Team B's copy had only a placeholder address
    assert Mail.objects.filter(inject=inj, owner=None, folder="inbox").count() == 2
    assert Mail.objects.filter(inject=inj, team=None, folder="sent").count() == 2  # the desk: no email copies
    sent = [p for p, _ in mailer.get().sent]
    assert [p["to"] for p in sent] == [["amina@home.org"]] and sent[0]["reply_to"].startswith("cnn+")
    # An answer from the personal address is the person's, matched by its reply token.
    m = Message.objects.get(inject=inj, channel="email", status="sent")
    r = inbound._new_email("rcv-p", {"from": "Amina <amina@home.org>", "to": [m.reply_to], "subject": "Re"})
    assert (r.person, r.team.code, r.match) == (a, "a", "token")


def test_also_by_email_only_for_inapp_and_not_in_test_sends(excon):
    a = participant("a", "Amina").person
    Person.objects.filter(pk=a.pk).update(personal_email="amina@home.org")
    inj = _inject(also_email=True)
    msgs, _ = delivery.send_oneoff(inj, all_targets=True)
    assert msgs and all(m.channel == "inapp" for m in msgs) and not mailer.get().sent
    inj.channel = "email"
    inj.save()
    drafts, _ = render.build(inj, conf.get_all())
    assert all("personal" not in d.target for d in drafts)  # by email anyway, to the channel's address
    conf.put("channel_mode", "inapp")
    drafts, warnings = render.build(Inject.objects.get(pk=inj.pk), conf.get_all())
    assert [d.to for d in drafts if d.channel == "email"] == [["amina@home.org"]]


def test_inject_form_and_bulk_set_also_email(excon):
    conf.put("channel_mode", "inapp")
    inj = _inject()
    page = excon.get(f"/injects/{inj.id}/")
    assert b'name="also_email"' in page.content
    excon.post("/injects/bulk/", {"ids": [inj.id], "action": "also_email"})
    assert Inject.objects.get(pk=inj.pk).also_email
    excon.post("/injects/bulk/", {"ids": [inj.id], "action": "no_email"})
    assert not Inject.objects.get(pk=inj.pk).also_email


def test_login_email_sends_new_password_and_qr_and_marks_shared(excon):
    pa = participant("a", "Amina")
    p = pa.person
    p.personal_email = "amina@home.org"
    p.save()
    old_slip = signin.token(p.user)
    r = excon.post("/people/logins/email/", {"person": p.id, "note": "Starts Monday 08:00."})
    assert r.status_code == 302
    payload, key = mailer.get().sent[-1]
    assert payload["to"] == ["amina@home.org"] and key == f"login-{p.id}-1"
    password = payload["text"].split("Password: ")[1].split()[0]
    assert authenticate(username=p.user.username, password=password)
    assert not authenticate(username=p.user.username, password="pw")  # the old one stops working
    assert signin.user_for(old_slip) is None
    link = re.search(r"https?://\S+/inbox/go/(\S+)/", payload["text"])
    assert signin.user_for(link.group(1)) == p.user and "Starts Monday 08:00." in payload["text"]
    link = link.group(0)
    att = payload["attachments"][0]
    assert att["content_id"] == "signin-qr" and base64.b64decode(att["content"])[:4] == b"\x89PNG"
    assert 'src="cid:signin-qr"' in payload["html"]
    # What is kept has neither the password nor the link.
    m = Message.objects.get(kind="login")
    assert m.status == "sent" and password not in m.text + m.html and link not in m.text + m.html
    p.refresh_from_db()
    assert p.login_shared_at and p.login_shared_via == "amina@home.org"
    assert b"shared " in excon.get("/people/").content
    # A reset issues a password nobody has: not shared any more.
    excon.post(f"/people/{p.id}/account/", {"action": "reset"})
    p.refresh_from_db()
    assert p.login_shared_at is None


def test_login_email_to_the_persona_desk_has_the_qr(excon):
    from ops.tests.test_desk import desk_client
    p = desk_client("Anna Desk").person
    p.personal_email = "anna@home.org"
    p.save()
    assert excon.post("/people/logins/email/", {"person": p.id}).status_code == 302
    payload, _ = mailer.get().sent[-1]
    assert payload["to"] == ["anna@home.org"] and "persona desk" in payload["text"]
    link = re.search(r"https?://\S+/inbox/go/(\S+)/", payload["text"])
    assert link and signin.user_for(link.group(1)) == p.user
    assert payload["attachments"][0]["content_id"] == "signin-qr" and 'src="cid:signin-qr"' in payload["html"]


def test_bulk_login_emails_skip_shared_and_missing(excon):
    a = participant("a", "Amina").person
    b = participant("b", "Bo").person
    c = participant("c", "Cy").person
    Person.objects.filter(pk=a.pk).update(personal_email="amina@home.org")
    Person.objects.filter(pk=b.pk).update(personal_email="bo@home.org")
    excon.post(f"/people/{b.id}/shared/", {"shared": "1"})
    assert Person.objects.get(pk=b.pk).login_shared_via == "by hand"
    excon.post("/people/logins/email/", {"reply_to": "excon@undac.org"})
    sent = [p for p, _ in mailer.get().sent]
    assert [p["to"] for p in sent] == [["amina@home.org"]] and sent[0]["reply_to"] == "excon@undac.org"
    assert conf.get("login_reply_to") == "excon@undac.org"
    assert not Person.objects.get(pk=c.pk).login_shared_at  # no personal email
    excon.post("/people/logins/email/", {"again": "1", "team": b.team_id})
    assert [p["to"] for p, _ in mailer.get().sent][-1] == ["bo@home.org"]
    assert Person.objects.get(pk=b.pk).login_shared_via == "bo@home.org"


def test_answer_to_a_login_email_is_ignored():
    p = participant("a", "Amina").person
    Person.objects.filter(pk=p.pk).update(personal_email="amina@home.org")
    r = inbound._new_email("rcv-l", {"from": "amina@home.org", "to": [f"{logins.FROM_LOCAL}@simex.kobolabs.dev"],
                                     "subject": "Re: Your SIMEX Mail login"})
    assert r.status == "ignored"


def _xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["SIMEX teams"])
    ws.append(["Name", "email", "team", "Flight"])
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile("roster.xlsx", buf.getvalue())


def test_roster_import_links_existing_and_adds_the_rest(excon):
    old = participant("a", "Joel Cretet").person
    lead = Person.objects.create(name="Lead A", team=old.team, role="tl")
    r = excon.post("/people/import/", {"file": _xlsx([
        ["Joel Cretet (Tony)", "t.cretet@atlas.org", "A", "PG256"],
        ["Karina Angela \tMorales Avalos", "karina@gmail.com\n", "B", "PG257"],
        ["Bad Email", "nope", "C", "PG258"],
    ])}, follow=True)
    page = r.content.decode()
    assert "similar name" in page and "Lead A" in page  # not in the file: left as they are
    assert "is not a usable address" in page
    r = excon.post("/people/import/apply/", {"accounts": "1"}, follow=True)
    old.refresh_from_db()
    assert old.personal_email == "t.cretet@atlas.org" and old.name == "Joel Cretet" and old.user.username == "a.Joel Cretet"
    k = Person.objects.get(personal_email="karina@gmail.com")
    assert k.name == "Karina Angela Morales Avalos" and k.team.code == "b" and k.role == "member"
    assert k.user and k.user.username == "b.karina.avalos"
    assert not Person.objects.filter(name="Bad Email").exists()
    assert Person.objects.get(pk=lead.pk).role == "tl"
    assert b"karina" in r.content  # the new login, shown once
    # Applying needs a fresh upload.
    assert excon.post("/people/import/apply/").status_code == 302
    assert Team.objects.filter(code="b").count() == 1


def test_people_page_saves_personal_email(excon):
    p = participant("a", "Amina").person
    excon.post("/people/save/", {"id": p.id, "name": "Amina", "email": "", "personal_email": "amina@home.org",
                                 "team_id": p.team_id, "role": "member", "active": "1"})
    assert Person.objects.get(pk=p.pk).personal_email == "amina@home.org"
    r = Client().get("/people/")
    assert r.status_code == 302  # ExCon only

"""SIMEX Calendar, step 7: the email channel. Invitations by email carry a text/calendar part
(METHOD REQUEST / CANCEL, the UID and SEQUENCE of the event, times in UTC), once per event,
change and recipient; a guest's iMIP REPLY in a received email sets their answer."""

import base64
from datetime import timedelta, timezone as dt_tz

from django.conf import settings
from django.utils import timezone

from ops import conf, ics, inbound, mailer
from ops.models import CalendarAttendee, CalendarEvent, Message, Person, Response, Team
from ops.tests.test_calendar_invites import invite, setup
from ops.tests.test_desk import desk_client


def email_setup(excon):
    setup(excon)
    conf.put("channel_mode", "email")
    ta = Team.objects.get(code="a")
    ta.email = "team-a@kobotoolbox.org"
    ta.save()
    return ta


def sent_ics():
    """[(to, method, ics text)] of the emails the log mailer sent with a calendar part."""
    out = []
    for p, key in mailer.get().sent:
        for a in p.get("attachments") or []:
            if a["filename"].endswith(".ics"):
                text = base64.b64decode(a["content"]).decode().replace("\r\n ", "")  # unfolded
                out.append((p["to"], a.get("content_type", ""), text, key))
    return out


def test_the_invitation_email_carries_the_event(excon):
    ta = email_setup(excon)
    anna = desk_client()
    r = invite(anna, "p:gov-plaida", [f"t:{ta.id}"], base="/inbox/desk", title="Road briefing, Plaida")
    assert r.json()["ok"], r.content
    ev = CalendarEvent.objects.get()
    got = sent_ics()
    assert len(got) == 1
    to, ctype, text, key = got[0]
    assert to == ["team-a@kobotoolbox.org"] and ctype == "text/calendar; method=REQUEST"
    assert "METHOD:REQUEST" in text and f"UID:{ev.uid}@{settings.MAIL_DOMAIN}" in text and "SEQUENCE:0" in text
    assert f"DTSTART:{ev.starts_at.astimezone(dt_tz.utc).strftime('%Y%m%dT%H%M%SZ')}" in text
    assert "SUMMARY:Road briefing\\, Plaida" in text and "LOCATION:OSOCC tent" in text
    assert f"ORGANIZER;CN=\"Grace Mwangi\":mailto:gov-plaida@{settings.MAIL_DOMAIN}" in text
    assert "mailto:team-a@kobotoolbox.org" in text and "PARTSTAT=NEEDS-ACTION" in text
    m = Message.objects.get(kind="desk")
    assert m.idem_key == f"cal-{ev.id}-0-request-t{ta.id}" and m.channel == "email"
    # A change: a REQUEST again with the next SEQUENCE; the same change never sent twice.
    s = (timezone.localtime(ev.starts_at) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    e = (timezone.localtime(ev.ends_at) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    invite(anna, "p:gov-plaida", [f"t:{ta.id}"], base="/inbox/desk", title="Road briefing, Plaida", eid=ev.id, start=s, end=e)
    ev.refresh_from_db()
    upd = sent_ics()[-1][2]
    assert ev.sequence == 1 and "METHOD:REQUEST" in upd and "SEQUENCE:1" in upd
    from ops import calendar  # the same send again: skipped by its key
    n = Message.objects.count()
    lay = next(x for x in calendar.layers(_desk_viewer(anna)) if x["key"] == "p:gov-plaida")
    calendar._send(_desk_viewer(anna), lay, [{"key": f"t:{ta.id}", "kind": "team", "name": ta.name, "addr": ""}], ev,
                   "update", None)
    assert Message.objects.count() == n
    # Cancelled: METHOD:CANCEL, STATUS:CANCELLED.
    anna.post(f"/inbox/desk/calendar/delete/{ev.id}/")
    to, ctype, text, _ = sent_ics()[-1]
    assert ctype == "text/calendar; method=CANCEL" and "METHOD:CANCEL" in text and "STATUS:CANCELLED" in text
    assert "SEQUENCE:2" in text


def _desk_viewer(client):
    from django.test import RequestFactory
    from ops.views_inbox import Viewer
    req = RequestFactory().get("/inbox/desk/calendar/")
    req.user = client.person.user
    return Viewer(req, box="desk")


def _reply(uid, email, partstat):
    return ("BEGIN:VCALENDAR\r\nMETHOD:REPLY\r\nBEGIN:VEVENT\r\n"
            f"UID:{uid}\r\nATTENDEE;CN=\"Team A: Ops; desk\";PARTSTAT={partstat}:mailto:{email}\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n")


def test_reply_parsing():
    assert ics.parse_reply(_reply("abc@x", "A@B.org", "ACCEPTED")) == {"uid": "abc@x", "answers": [("a@b.org", "accepted")]}
    assert ics.parse_reply(_reply("abc@x", "a@b.org", "TENTATIVE"))["answers"] == [("a@b.org", "tentative")]
    assert ics.parse_reply(_reply("abc@x", "a@b.org", "DECLINED"))["answers"] == [("a@b.org", "declined")]
    assert ics.parse_reply(_reply("abc@x", "a@b.org", "NEEDS-ACTION")) is None
    assert ics.parse_reply(_reply("abc@x", "a@b.org", "ACCEPTED").replace("METHOD:REPLY", "METHOD:REQUEST")) is None
    assert ics.parse_reply("hello") is None and ics.parse_reply(b"\xff\xfe garbage BEGIN:VCALENDAR") is None
    folded = _reply("abc@x", "a@b.org", "ACCEPTED").replace("PARTSTAT=", "PART\r\n STAT=")
    assert ics.parse_reply(folded)["answers"] == [("a@b.org", "accepted")]
    assert ics.parse_reply("BEGIN:VCALENDAR\n" + "X" * 400000) is None  # capped, malformed


def test_an_imip_reply_sets_the_answer(excon, monkeypatch):
    ta = email_setup(excon)
    anna = desk_client()
    invite(anna, "p:gov-plaida", [f"t:{ta.id}"], base="/inbox/desk", title="Road briefing")
    ev = CalendarEvent.objects.get()
    jane = Person.objects.create(name="Jane", team=ta, email="jane@kobotoolbox.org")
    lm = mailer.get()
    body = _reply(ics.uid(ev), "jane@kobotoolbox.org", "ACCEPTED").encode()
    lm.received["r1"] = {"text": "Accepted", "html": "", "headers": {}, "message_id": "<r1@x>", "subject": "Accepted: Road briefing",
                         "attachments": [{"filename": "invite.ics"}]}
    monkeypatch.setattr(type(lm), "received_attachments", lambda self, rid: [
        {"filename": "invite.ics", "content_type": "text/calendar; method=REPLY", "size": len(body), "download_url": "x"}],
        raising=False)
    monkeypatch.setattr(type(lm), "download", lambda self, url, cap: body, raising=False)
    sent_before = len(lm.sent)
    rid = inbound.record_event({"type": "email.received", "data": {
        "email_id": "r1", "from": "Jane <jane@kobotoolbox.org>", "to": [f"gov-plaida@{settings.MAIL_DOMAIN}"],
        "subject": "Accepted: Road briefing", "created_at": timezone.now().isoformat()}})
    inbound.fetch(rid)
    row = CalendarAttendee.objects.get(event=ev, team=ta)
    assert row.response == "accepted" and row.responded_by == jane
    r = Response.objects.get(pk=rid)
    assert r.inject_id is None and r.match != "guess"  # kept as a Response, never guessed onto an inject
    assert len(lm.sent) == sent_before  # nothing sent back
    # Unknown UIDs and someone not invited: ignored.
    body2 = _reply(f"0123456789abcdef@{settings.MAIL_DOMAIN}", "jane@kobotoolbox.org", "DECLINED").encode()
    monkeypatch.setattr(type(lm), "download", lambda self, url, cap: body2, raising=False)
    lm.received["r2"] = {**lm.received["r1"], "message_id": "<r2@x>"}
    rid2 = inbound.record_event({"type": "email.received", "data": {
        "email_id": "r2", "from": "Jane <jane@kobotoolbox.org>", "to": [f"gov-plaida@{settings.MAIL_DOMAIN}"],
        "subject": "Declined", "created_at": timezone.now().isoformat()}})
    inbound.fetch(rid2)
    assert CalendarAttendee.objects.get(event=ev, team=ta).response == "accepted"

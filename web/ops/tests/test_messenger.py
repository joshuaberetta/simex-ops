"""SIMEX Messenger (ops/messenger.py): rooms, posts, who may read what (teams never cross, a
member added later reads from then on), the poll by cursor, pushes once per burst, edits and
deletions kept for ExCon, the leader's read-only view, Files, clearing and backups."""

import json
from datetime import datetime

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from ops import backup, conf, messenger, push, reset, simclock
from ops.models import MailAction, Post, PostEdit, PushSubscription, ReadMark, Room, RoomChange, Team
from ops.tests.test_leaders import leader
from ops.tests.test_ops import participant

F = {"headers": {"x-requested-with": "fetch"}}


def on():
    conf.put("chat_enabled", "1")


def say(c, room_id, body="", **extra):
    r = c.post(f"/chat/r/{room_id}/send/", {"body": body, **extra}, **F)
    assert r.status_code == 200, r.content
    return r.json()["post"]


def room_of(c, key):
    r = c.post("/chat/new/", {"keys": [key]}, **F)
    assert r.status_code == 200, r.content
    return r.json()["id"]


def poll(c, cursor, room=None, base="/chat"):
    r = c.get(f"{base}/poll/?c={cursor}" + (f"&r={room}" if room else ""), **F)
    return None if r.status_code == 204 else r.json()


def test_off_by_default_and_for_teams_only(excon):
    pa = participant("a", "Jane")
    assert pa.get("/chat/").status_code == 404
    on()
    assert pa.get("/chat/").status_code == 200
    assert excon.get("/chat/").status_code == 302 and excon.get("/chats/").status_code == 200
    assert Client().get("/chat/").status_code == 302  # signed out: to the sign-in page
    assert pa.get("/chats/").status_code in (302, 403)
    m = pa.get("/chat/manifest.webmanifest").json()
    assert m["id"] == "/chat/" and m["name"] == "SIMEX Messenger" and "app=chat" in m["start_url"]
    assert "simex-messenger-" in pa.get("/chat/sw.js").content.decode()


def test_team_room_direct_chats_and_receipts(excon):
    on()
    pa, po = participant("a", "Jane"), participant("a", "Omar")
    pb = participant("b", "Bea")
    la = leader("a")
    ta = Team.objects.get(code="a")
    pa.get("/chat/")
    pb.get("/chat/")
    team_room = Room.objects.get(kind="team", team=ta)
    assert {m.key for m in team_room.members.all()} == {f"t:{ta.id}", f"l:{la.person.id}"}
    rooms = pa.get("/chat/rooms/", **F).json()
    c0 = rooms["c"]
    assert [r["kind"] for r in rooms["rooms"]] == ["team"]
    p = say(pa, team_room.id, "Morning all", client_key="k1")
    again = say(pa, team_room.id, "Morning all", client_key="k1")  # a resend: the same post
    assert again["id"] == p["id"] and Post.objects.filter(kind="text").count() == 1
    # Omar sees it in the poll, unread; reading it moves Jane's ticks.
    got = poll(po, c0, team_room.id)
    assert [x["body"] for x in got["posts"]] == ["Morning all"] and got["rooms"][0]["unread"] == 1
    assert po.post(f"/chat/r/{team_room.id}/read/", {"upto": p["id"]}, **F).status_code == 200
    assert ReadMark.objects.filter(person=po.person).exists()
    mine = poll(pa, got["c"], team_room.id)
    assert mine["seen"][str(p["id"])] == 1
    assert poll(pa, mine["c"], team_room.id) is None  # nothing new: a 204
    # The leader is in the team room; another team never.
    assert la.get(f"/chat/r/{team_room.id}/posts/", **F).status_code == 200
    assert pb.get(f"/chat/r/{team_room.id}/posts/", **F).status_code == 404
    assert pb.post(f"/chat/r/{team_room.id}/send/", {"body": "hi"}, **F).status_code == 404
    # Direct chats: a teammate yes, another team's member no (not in the book).
    d = room_of(pa, f"m:{po.person.id}")
    assert room_of(pa, f"m:{po.person.id}") == d and room_of(po, f"m:{pa.person.id}") == d  # one room per pair
    assert pa.post("/chat/new/", {"keys": [f"m:{pb.person.id}"]}, **F).status_code == 400
    say(pa, d, "Can you call the airport?")
    assert [r["id"] for r in pb.get("/chat/rooms/", **F).json()["rooms"]] == [Room.objects.get(kind="team", team__code="b").id]
    # The leader reads the team's chats read-only, their direct chat included; never posts there.
    page = la.get("/chat/team/rooms/", **F).json()
    assert d in [r["id"] for r in page["rooms"]]
    assert la.get(f"/chat/team/r/{d}/posts/", **F).json()["posts"][0]["body"] == "Can you call the airport?"
    assert pa.get("/chat/team/").status_code == 403
    assert MailAction.objects.filter(action="chat_post", person=pa.person).count() == 2
    conf.put("chat_direct", "0")
    assert f"m:{po.person.id}" not in [b["key"] for b in pa.get("/chat/book/", **F).json()["book"]]


def test_groups_join_later_reads_from_then(excon):
    on()
    pa, po, pz = participant("a", "Jane"), participant("a", "Omar"), participant("a", "Zoe")
    r = pa.post("/chat/new/", {"keys": [f"m:{po.person.id}"], "title": "Logistics"}, **F)
    gid = r.json()["id"]
    assert Room.objects.get(pk=gid).kind == "group"
    say(pa, gid, "Before Zoe")
    assert pa.post(f"/chat/r/{gid}/settings/", {"act": "add", "keys": [f"m:{pz.person.id}"]}, **F).status_code == 200
    say(po, gid, "After Zoe")
    bodies = [p["body"] for p in pz.get(f"/chat/r/{gid}/posts/", **F).json()["posts"] if p["kind"] == "text"]
    assert bodies == ["After Zoe"]
    assert po.post(f"/chat/r/{gid}/settings/", {"act": "rename", "title": "x"}, **F).status_code == 400  # not an admin
    assert pz.post(f"/chat/r/{gid}/settings/", {"act": "leave"}, **F).status_code == 200
    assert pz.get(f"/chat/r/{gid}/posts/", **F).status_code == 404
    conf.put("chat_groups", "0")
    assert pa.post("/chat/new/", {"keys": [f"m:{po.person.id}", f"m:{pz.person.id}"], "title": "G"}, **F).status_code == 400


def test_edits_and_deletions_are_kept_for_excon(excon):
    on()
    pa, po = participant("a", "Jane"), participant("a", "Omar")
    d = room_of(pa, f"m:{po.person.id}")
    p = say(pa, d, "The meeting is at 3")
    assert po.post(f"/chat/p/{p['id']}/edit/", {"body": "hijack"}, **F).status_code == 403
    assert pa.post(f"/chat/p/{p['id']}/edit/", {"body": "The meeting is at 4"}, **F).json()["post"]["edited"]
    assert PostEdit.objects.get().body == "The meeting is at 3"
    assert pa.post(f"/chat/p/{p['id']}/react/", {"emoji": "👍"}, **F).status_code == 200
    assert pa.post(f"/chat/p/{p['id']}/delete/", **F).status_code == 200
    seen = po.get(f"/chat/r/{d}/posts/", **F).json()["posts"][-1]
    assert seen["deleted"] and seen["body"] == "" and "files" not in seen
    page = excon.get(f"/chats/{d}/")
    assert b"The meeting is at 4" in page.content and b"The meeting is at 3" in page.content and b"Deleted by Jane" in page.content
    assert b"The meeting is at 3" in excon.get(f"/chats/{d}/?format=md").content
    excon.post(f"/chats/{d}/freeze/", {"on": "1"})
    assert pa.post(f"/chat/r/{d}/send/", {"body": "x"}, **F).status_code == 400


def test_push_once_per_burst_to_messenger_devices_only(excon, settings, django_capture_on_commit_callbacks):
    settings.VAPID_PRIVATE_KEY = "Uq1-1K7JXzT4Bo8SzbsxJAX5BMAI0uBkN6lSV4jL8A8"
    on()
    pa, po = participant("a", "Jane"), participant("a", "Omar")
    PushSubscription.objects.create(person=po.person, endpoint="https://fcm.googleapis.com/chat", p256dh="k", auth="a", app="chat")
    PushSubscription.objects.create(person=po.person, endpoint="https://fcm.googleapis.com/mail", p256dh="k", auth="a")
    sent = []
    push.set_sender(lambda info, body: sent.append((info["endpoint"], json.loads(body))) or 201)
    push.RUN_INLINE = True
    try:
        d = room_of(pa, f"m:{po.person.id}")
        with django_capture_on_commit_callbacks(execute=True):
            say(pa, d, "Secret plan one")
        with django_capture_on_commit_callbacks(execute=True):
            say(pa, d, "Secret plan two")
    finally:
        push.set_sender(None)
        push.RUN_INLINE = False
    assert len(sent) == 1 and sent[0][0].endswith("/chat")
    assert sent[0][1]["url"] == f"/chat/r/{d}/" and "Secret" not in json.dumps(sent[0][1])


def test_members_see_scenario_time_files_and_clearing(excon):
    on()
    pa, po = participant("a", "Jane"), participant("a", "Omar")
    la = leader("a")
    simclock.set_clock(timezone.make_aware(datetime(2035, 5, 20, 9, 0)), 1)
    d = room_of(pa, f"m:{po.person.id}")
    p = say(pa, d, "See the map", files=SimpleUploadedFile("map.png", b"\x89PNG\r\n\x1a\nxx"))
    assert p["day"] == "Sun 20 May" and "sim" not in p  # a member: the scenario's date only
    lp = la.get(f"/chat/team/r/{d}/posts/", **F).json()["posts"][-1]
    assert lp["sim"].startswith("Sun 20 May") and lp["day"] != "Sun 20 May"  # the leader: both
    fid = p["files"][0]["id"]
    assert po.get(f"/chat/f/{fid}/?inline=1")["Content-Type"] == "image/png"
    assert Participant404(participant("b", "Bea"), f"/chat/f/{fid}/")
    lf = la.get("/inbox/files/")
    assert "map.png" in [r["filename"] for _, rows, _ in lf.context["groups"] for r in rows]
    assert la.get(f"/inbox/files/c/{fid}/").status_code == 200
    data = backup.export()
    assert any(o["model"] == "ops.post" for o in data["objects"])
    reset.clear_team(Team.objects.get(code="a"), scope="members")
    assert not Room.objects.filter(kind="direct").exists()
    assert pa.get("/chat/rooms/", **F).status_code == 200
    reset.start_afresh()
    assert not Post.objects.exists() and not RoomChange.objects.exists()
    conf.put("sim_enabled", "0")


def Participant404(c, url):
    return c.get(url).status_code == 404


def test_viewer_room_cache_follows_new_rooms(excon):
    on()
    pa, po = participant("a", "Jane"), participant("a", "Omar")
    c = pa.get("/chat/rooms/", **F).json()["c"]
    po.get("/chat/rooms/", **F)
    d = room_of(pa, f"m:{po.person.id}")
    say(pa, d, "New room")
    got = poll(po, c)  # Omar's cached room ids didn't have it: the member change refreshes them
    assert d in [r["id"] for r in got["rooms"]]
    assert messenger.unread_count(po.person) == 1

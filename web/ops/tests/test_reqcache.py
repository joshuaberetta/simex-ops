"""ops/reqcache.py: a memo for one web request only; outside a request every read is fresh,
and pause / arming are always read anew."""

from django.db import connection
from django.test.utils import CaptureQueriesContext

from ops import conf, reqcache, render
from ops.models import Sender, Setting


def in_request(fn):
    token = reqcache._memo.set({})
    try:
        return fn()
    finally:
        reqcache._memo.reset(token)


def test_outside_a_request_nothing_is_kept(db):
    conf.put("footer", "one")
    assert conf.get("footer") == "one"
    Setting.objects.filter(key="footer").update(value="two")  # behind its back
    assert conf.get("footer") == "two" and not reqcache.active()


def test_within_a_request_read_once_and_writes_seen(db):
    Sender.objects.create(local_part="cnn", display_name="CNN")

    def go():
        with CaptureQueriesContext(connection) as q:
            for _ in range(5):
                conf.get("footer"), conf.flag("personal_mailboxes"), render.sender_by_local("cnn")
        assert len(q.captured_queries) == 2  # the settings once, the persona once
        conf.put("footer", "new")  # a write in the request is read back
        assert conf.get("footer") == "new"
        s = Sender.objects.get(local_part="cnn")
        s.display_name = "CNN News"
        s.save()
        assert render.sender_by_local("cnn")[0].display_name == "CNN News"
        # Pause and arming are read fresh every time, even within the request.
        Setting.objects.update_or_create(key="paused", defaults={"value": "0"})
        assert not conf.flag("paused")
        Setting.objects.filter(key="paused").update(value="1")
        assert conf.flag("paused")
    in_request(go)
    assert not reqcache.active()


def test_the_middleware_drops_the_memo_after_each_request(client, db):
    conf.put("footer", "a")
    assert client.get("/healthz").status_code == 200
    assert not reqcache.active()

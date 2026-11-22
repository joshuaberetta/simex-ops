"""Push notifications for new SIMEX Mail (Web Push with VAPID): a participant's phone (the
home-screen app on iPhone) or browser gets "New mail" even with SIMEX Mail closed.

Devices subscribe from the mail client (static/ops/mail.js, service worker sw.js) and are
stored as PushSubscription. Every Mail that lands in a team's inbox is pushed to the team's
active members, after the transaction commits, by a few background threads so neither the
scheduler nor a request waits on the push services. The payload is only what the team sees
in its inbox list: sender, subject and the conversation link. Off without VAPID_PRIVATE_KEY.
"""

import base64
import functools
import json
import logging
import queue
import threading

from django.conf import settings
from django.db import transaction
from django.utils import timezone

log = logging.getLogger(__name__)
RUN_INLINE = False  # tests: no background thread
_sender = None  # tests: set_sender(fake); fake(subscription_info, data) -> HTTP status


def enabled():
    return bool(settings.VAPID_PRIVATE_KEY)


def _vapid():
    from py_vapid import Vapid02
    return Vapid02.from_string(settings.VAPID_PRIVATE_KEY)


def public_key():
    """The application server key browsers subscribe with (base64url, uncompressed point)."""
    return _public(settings.VAPID_PRIVATE_KEY) if enabled() else ""


@functools.lru_cache(maxsize=2)
def _public(private_key):
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid02
    raw = Vapid02.from_string(private_key).public_key.public_bytes(serialization.Encoding.X962,
                                                                   serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def set_sender(fn):
    global _sender
    _sender = fn


def _webpush(info, data):
    from pywebpush import WebPushException, webpush
    try:
        r = webpush(info, data, vapid_private_key=_vapid(), vapid_claims={"sub": settings.VAPID_SUBJECT},
                    ttl=12 * 3600, timeout=10, headers={"Urgency": "high"})
        return r.status_code
    except WebPushException as e:
        return e.response.status_code if e.response is not None else 0


def payload(mail):
    # A member's own mailbox is at /inbox/me/; a leader's, like the team inbox, at /inbox/; the persona desk's at /inbox/desk/.
    base = "/inbox/desk" if not mail.team_id else "/inbox/me" if mail.owner_id and not mail.owner.is_leader else "/inbox"
    return {"title": mail.from_name or mail.from_addr or "New message", "body": mail.subject or "(no subject)",
            "url": f"{base}/t/{mail.thread}/", "tag": f"mail-{mail.id}"}


def notify_mail(mail):
    """Push a Mail that just landed in a mailbox (call inside or outside a transaction): the
    team's members' devices for the team inbox, the owner's for a leader's or member's own mailbox,
    the persona desk's for the desk."""
    if not (enabled() or _sender) or mail.folder != "inbox":
        return
    transaction.on_commit(lambda: _start(mail.team_id, payload(mail), mail.author_id, mail.owner_id))


# A few worker threads per process take the pushes from a queue: an inject landing in every
# team's inbox at once must not open a database connection per Mail (the cluster has few).
WORKERS = 3
_queue = queue.Queue()
_workers = []
_workers_lock = threading.Lock()


def _work():
    while True:
        args = _queue.get()
        try:
            send_team(*args)
        except Exception:
            log.exception("push failed")


def _start(team_id, data, author_id, owner_id=None):
    if RUN_INLINE:
        return send_team(team_id, data, author_id, owner_id)
    with _workers_lock:
        while len(_workers) < WORKERS:
            t = threading.Thread(target=_work, daemon=True, name=f"push-{len(_workers)}")
            t.start()
            _workers.append(t)
    _queue.put((team_id, data, author_id, owner_id))


def send_team(team_id, data, author_id=None, owner_id=None):
    from django.db import connection
    from .models import PushSubscription
    try:
        who = ({"person_id": owner_id} if owner_id else {"person__team_id": team_id, "person__role": "member"}
               if team_id else {"person__role": "desk"})  # no team: the persona desk's mailbox
        subs = list(PushSubscription.objects.filter(**who, person__active=True,
                                                    person__user__is_active=True).exclude(person_id=author_id))
        body = json.dumps(data)
        if not RUN_INLINE:  # no database connection held through the push services' round trips
            connection.close()
        gone, ok = [], []
        for s in subs:
            info = {"endpoint": s.endpoint, "keys": {"p256dh": s.p256dh, "auth": s.auth}}
            try:
                status = (_sender or _webpush)(info, body)
            except Exception as e:  # a push service being down must not break anything else
                log.warning("push to %s failed: %s", s.endpoint[:60], e)
                continue
            if status in (404, 410):  # the device unsubscribed or the subscription expired
                gone.append(s.pk)
            elif 200 <= status < 300:
                ok.append(s.pk)
            else:
                log.warning("push to %s: HTTP %s", s.endpoint[:60], status)
        if gone:
            PushSubscription.objects.filter(pk__in=gone).delete()
        if ok:
            PushSubscription.objects.filter(pk__in=ok).update(last_ok_at=timezone.now())
    finally:
        if not RUN_INLINE:
            connection.close()

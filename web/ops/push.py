"""Push notifications for new SIMEX Mail (Web Push with VAPID): a participant's phone (the
home-screen app on iPhone) or browser gets "New mail" even with SIMEX Mail closed.

Devices subscribe from the mail client (static/ops/mail.js, service worker sw.js) and are
stored as PushSubscription. Every Mail that lands in a team's inbox is pushed to the team's
active members, after the transaction commits, in a background thread so neither the
scheduler nor a request waits on the push services. The payload is only what the team sees
in its inbox list: sender, subject and the conversation link. Off without VAPID_PRIVATE_KEY.
"""

import base64
import functools
import json
import logging
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
    return {"title": mail.from_name or mail.from_addr or "New message", "body": mail.subject or "(no subject)",
            "url": f"/inbox/t/{mail.thread}/", "tag": f"mail-{mail.id}"}


def notify_mail(mail):
    """Push a Mail that just landed in a team's inbox (call inside or outside a transaction)."""
    if not (enabled() or _sender) or mail.folder != "inbox":
        return
    transaction.on_commit(lambda: _start(mail.team_id, payload(mail), mail.author_id))


def _start(team_id, data, author_id):
    if RUN_INLINE:
        return send_team(team_id, data, author_id)
    threading.Thread(target=send_team, args=(team_id, data, author_id), daemon=True).start()


def send_team(team_id, data, author_id=None):
    from django.db import connection
    from .models import PushSubscription
    try:
        subs = list(PushSubscription.objects.filter(person__team_id=team_id, person__active=True,
                                                    person__user__is_active=True).exclude(person_id=author_id))
        body = json.dumps(data)
        for s in subs:
            info = {"endpoint": s.endpoint, "keys": {"p256dh": s.p256dh, "auth": s.auth}}
            try:
                status = (_sender or _webpush)(info, body)
            except Exception as e:  # a push service being down must not break anything else
                log.warning("push to %s failed: %s", s.endpoint[:60], e)
                continue
            if status in (404, 410):  # the device unsubscribed or the subscription expired
                s.delete()
            elif 200 <= status < 300:
                PushSubscription.objects.filter(pk=s.pk).update(last_ok_at=timezone.now())
            else:
                log.warning("push to %s: HTTP %s", s.endpoint[:60], status)
    finally:
        if not RUN_INLINE:
            connection.close()

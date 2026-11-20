"""Resend API client, plus a log-only mailer for local development and tests."""

import base64
import threading
import time
import uuid

import httpx

from django.conf import settings as config

API = "https://api.resend.com"


class SendError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class ResendMailer:
    name = "resend"

    def __init__(self, api_key, min_interval=0.6):
        self.client = httpx.Client(base_url=API, timeout=30,
                                   headers={"Authorization": f"Bearer {api_key}", "User-Agent": "simex-ops"})
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def _wait(self):
        with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()

    def send(self, payload, idem_key):
        """POST /emails with an idempotency key; returns the Resend email id."""
        for attempt in range(4):
            self._wait()
            try:
                r = self.client.post("/emails", json=payload, headers={"Idempotency-Key": idem_key[:256]})
            except httpx.HTTPError as e:
                if attempt == 3:
                    raise SendError(f"Network error: {e}", retryable=True) from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 3:
                    raise SendError(f"Resend {r.status_code}: {r.text[:300]}", retryable=True)
                time.sleep(float(r.headers.get("retry-after") or 2 ** attempt))
                continue
            if r.status_code >= 400:
                try:
                    msg = r.json().get("message") or r.text
                except ValueError:
                    msg = r.text
                raise SendError(f"Resend {r.status_code}: {msg[:500]}")
            return r.json().get("id")
        raise SendError("Resend: gave up after retries", retryable=True)

    def _get(self, path, **params):
        self._wait()
        r = self.client.get(path, params=params or None)
        if r.status_code >= 400:
            raise SendError(f"Resend {r.status_code}: {r.text[:300]}", retryable=r.status_code >= 500)
        return r.json()

    def get_received(self, email_id):
        return self._get(f"/emails/receiving/{email_id}")

    def get_sent(self, email_id):
        return self._get(f"/emails/{email_id}")

    def list_received(self, limit=50):
        return self._get("/emails/receiving", limit=limit).get("data", [])

    def received_attachments(self, email_id):
        return self._get(f"/emails/receiving/{email_id}/attachments").get("data", [])

    def download(self, url, max_bytes):
        with httpx.stream("GET", url, timeout=60, follow_redirects=True) as r:
            r.raise_for_status()
            data = b""
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > max_bytes:
                    return None
            return data

    def domain_status(self, domain):
        r = self.client.get("/domains")
        if r.status_code >= 400:
            return {"error": f"Resend {r.status_code}: {r.text[:300]}"}
        for d in r.json().get("data", []):
            if d.get("name") == domain:
                detail = self.client.get(f"/domains/{d['id']}")
                return detail.json() if detail.status_code < 400 else d
        return {"error": f"{domain} is not added in Resend yet."}


class LogMailer:
    """Records payloads instead of sending (MAILER=log)."""
    name = "log"

    def __init__(self):
        self.sent = []
        self.received = {}  # tests put fake received emails here: id -> retrieve-response dict

    def get_received(self, email_id):
        if email_id not in self.received:
            raise SendError("MAILER=log: no such received email")
        return self.received[email_id]

    def list_received(self, limit=50):
        return [{"id": k, **v} for k, v in self.received.items()]

    def get_sent(self, email_id):
        return {"id": email_id, "message_id": f"<{email_id}@log.invalid>"}

    def received_attachments(self, email_id):
        return [{**a, "download_url": "log://" + a["filename"]} for a in self.received.get(email_id, {}).get("attachments", [])]

    def download(self, url, max_bytes):
        return b"log attachment " + url.encode()

    def send(self, payload, idem_key):
        self.sent.append((payload, idem_key))
        return f"log-{uuid.uuid4().hex[:12]}"

    def domain_status(self, domain):
        return {"error": "MAILER=log: emails are recorded, not sent (no RESEND_API_KEY)."}


def payload(msg, attachments, headers=None):
    p = {"from": msg.from_addr, "to": msg.to, "subject": msg.subject, "text": msg.text, "html": msg.html,
         "headers": {"X-Simex-Inject": str(msg.inject_id or ""), **(headers or {})}}
    if msg.cc:
        p["cc"] = msg.cc
    if msg.bcc:
        p["bcc"] = msg.bcc
    if msg.reply_to:
        p["reply_to"] = msg.reply_to
    if attachments:
        p["attachments"] = [{"filename": a.filename, "content": base64.b64encode(bytes(a.data)).decode()}
                            for a in attachments]
    return p


_mailer = None


def get():
    global _mailer
    if _mailer is None:
        _mailer = (ResendMailer(config.RESEND_API_KEY, config.RESEND_MIN_INTERVAL)
                   if config.MAILER == "resend" and config.RESEND_API_KEY else LogMailer())
    return _mailer


def set_mailer(m):
    global _mailer
    _mailer = m

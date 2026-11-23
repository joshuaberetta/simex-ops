"""Who signed in, when, and what they did since: each Person's sign-ins, sign-outs and failed
sign-ins (LoginEvent, from Django's auth signals), when they were last seen (Person.last_seen_at,
stamped by middleware.LastSeen at most once a minute) and their timeline on People & logins →
activity (sign-ins, what they did in SIMEX Mail, what they read and downloaded, what they sent)."""

import re
from datetime import timedelta

from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.core.cache import cache
from django.dispatch import receiver
from django.utils import timezone

from .models import Event, LoginEvent, Mail, MailAction, Message, Person, PushSubscription, Response

SEEN_EVERY = 60  # seconds between two last_seen_at writes for one login
ONLINE = timedelta(minutes=5)  # seen this recently: shown as "online"


def client_ip(request):
    return (request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get("REMOTE_ADDR") or "").split(",")[0].strip()[:64]


def _person(user):
    if not user or not getattr(user, "pk", None):
        return None
    return Person.objects.filter(user_id=user.pk).first()


def _log(request, kind, user=None, username="", method=""):
    person = _person(user)
    if user is not None and not person:
        return None  # ExCon staff without a Person: not tracked here
    if user is None and username:  # a failed sign-in: whose login it was meant to be
        person = Person.objects.filter(user__username__iexact=username).first()
        if not person:
            return None
    if not person:
        return None
    return LoginEvent.objects.create(
        person=person, username=(user.username if user else username)[:150], kind=kind, method=method,
        ip=client_ip(request) if request else "",
        user_agent=(request.META.get("HTTP_USER_AGENT", "") if request else "")[:400],
        host="mail" if getattr(request, "mail_host", False) else "ops")


@receiver(user_logged_in)
def _signed_in(sender, request, user, **kw):
    ev = _log(request, "signin", user, method=getattr(request, "signin_method", "password") if request else "password")
    if ev:
        Person.objects.filter(pk=ev.person_id).update(last_seen_at=ev.at)


@receiver(user_logged_out)
def _signed_out(sender, request, user, **kw):
    if user:
        _log(request, "signout", user)


@receiver(user_login_failed)
def _failed(sender, credentials, request=None, **kw):
    username = (credentials or {}).get("username") or ""
    if username:
        _log(request, "failed", username=username.strip()[:150])


def seen(request):
    """Stamp the signed-in person's last_seen_at (at most once a minute per login)."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return
    key = f"seen:{user.pk}"
    if cache.get(key):
        return
    cache.set(key, 1, SEEN_EVERY)
    Person.objects.filter(user_id=user.pk).update(last_seen_at=timezone.now())


def online(person):
    return bool(person.last_seen_at and timezone.now() - person.last_seen_at < ONLINE)


def device(ua):
    """A short name for a browser's user agent: "iPhone · Safari", "Mac · Chrome"..."""
    ua = ua or ""
    if not ua:
        return ""
    os_ = next((name for pat, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                       ("Windows", "Windows"), ("Macintosh", "Mac"), ("CrOS", "Chromebook"),
                                       ("Linux", "Linux")) if pat in ua), "")
    browser = next((name for pat, name in ((r"EdgA?/|EdgiOS", "Edge"), (r"OPR/", "Opera"), (r"SamsungBrowser", "Samsung"),
                                           (r"Firefox/|FxiOS", "Firefox"), (r"Chrome/|CriOS", "Chrome"),
                                           (r"Safari/", "Safari"), (r"AppleWebKit", "app")) if re.search(pat, ua)), "")
    return " · ".join(x for x in (os_, browser) if x) or ua[:40]


SIGNIN_TEXT = {"signin": "Signed in", "signout": "Signed out", "failed": "Failed sign-in (wrong password)"}
ACTION_TEXT = {"star": "Starred", "unstar": "Unstarred", "read": "Marked read", "unread": "Marked unread",
               "archive": "Archived", "unarchive": "Moved back to the Inbox", "trash": "Deleted", "restore": "Restored",
               "label_add": "Labelled", "label_remove": "Removed a label from", "move": "Moved",
               "discard": "Discarded a draft", "label_create": "Created a label", "label_delete": "Deleted a label",
               "search": "Searched", "draft": "Saved a draft", "send": "Sent"}
GROUPS = {"signin": "Sign-ins", "mail": "In SIMEX Mail", "read": "Reading", "sent": "Sent"}


def _box(person, team_id, owner_id):
    if team_id is None:
        return "the persona desk"
    if owner_id is None:
        return "the team inbox"
    return "their own mailbox" if owner_id == person.id else "a member's mailbox"


def timeline(person, limit=500, group=""):
    """The person's activity, latest first: dicts of at, group (GROUPS), text, detail, link."""
    items = []
    if group in ("", "signin"):
        for e in LoginEvent.objects.filter(person=person)[:limit]:
            how = {"qr": "with the QR code", "password": "with the password"}.get(e.method, "")
            items.append({"at": e.at, "group": "signin", "text": " ".join(x for x in (SIGNIN_TEXT.get(e.kind, e.kind), how) if x),
                          "detail": " · ".join(x for x in (device(e.user_agent), "SIMEX Mail" if e.host == "mail" else "", e.ip) if x),
                          "warn": e.kind == "failed", "ua": e.user_agent})
    if group in ("", "mail"):
        acts = list(MailAction.objects.filter(person=person).order_by("-at")[:limit])
        threads = {t for a in acts for t in (a.threads or [])[:20]}
        subjects = dict(Mail.objects.filter(thread__in=threads).exclude(subject="").values_list("thread", "subject")) if threads else {}
        for a in acts:
            d = a.detail or {}
            subj = [subjects[t] for t in (a.threads or []) if t in subjects]
            what = "; ".join(f"“{s}”" for s in subj[:3]) + (f" and {len(subj) - 3} more" if len(subj) > 3 else "")
            if not what and a.threads:
                what = f"{len(a.threads)} conversation{'s' if len(a.threads) != 1 else ''}"
            extra = ", ".join(d.get("labels") or []) or d.get("name") or d.get("q") or ""
            if a.action == "send" and d.get("kind") in ("reply", "forward"):
                extra = ""
                what = f"a {d['kind']} in {what}" if what else f"a {d['kind']}"
            items.append({"at": a.at, "group": "mail", "text": " ".join(x for x in (ACTION_TEXT.get(a.action, a.action), what) if x),
                          "detail": " · ".join(x for x in (f"“{extra}”" if extra else "", "in " + _box(person, a.team_id, a.owner_id)) if x)})
    if group in ("", "read"):
        for e in Event.objects.filter(person=person, type__in=("read", "download")).select_related("message").order_by("-at")[:limit]:
            m = e.message
            items.append({"at": e.at, "group": "read",
                          "text": f"Opened “{m.subject}”" if e.type == "read" else f"Downloaded {e.link or 'a file'}",
                          "detail": (f"from “{m.subject}”" if e.type == "download" else "") or device(e.user_agent or ""),
                          "link": f"/log/{m.id}/"})
    if group in ("", "sent"):
        for r in Response.objects.filter(person=person).order_by("-received_at")[:limit]:
            items.append({"at": r.received_at, "group": "sent", "text": f"Wrote “{r.subject}”",
                          "detail": f"to {r.persona}" if r.persona else "", "link": f"/replies/{r.id}/"})
        for m in Message.objects.filter(author=person).exclude(kind="login").order_by("-created_at")[:limit]:
            items.append({"at": m.created_at, "group": "sent", "text": f"Sent “{m.subject}”",
                          "detail": f"{m.kind} · {m.target or ''}".strip(" ·"), "link": f"/log/{m.id}/"})
    items.sort(key=lambda i: i["at"], reverse=True)
    return items[:limit]


def devices(person):
    """Devices seen at sign-in (user agent -> first, last, count) and those with alerts on."""
    seen_ = {}
    for e in LoginEvent.objects.filter(person=person, kind="signin").order_by("at"):
        d = seen_.setdefault(e.user_agent, {"name": device(e.user_agent) or "unknown", "ua": e.user_agent,
                                            "first": e.at, "last": e.at, "n": 0, "ip": e.ip})
        d["last"], d["n"], d["ip"] = e.at, d["n"] + 1, e.ip
    push = [{"name": device(s.user_agent) or "unknown", "ua": s.user_agent, "since": s.created_at, "ok": s.last_ok_at}
            for s in PushSubscription.objects.filter(person=person).order_by("-created_at")]
    return sorted(seen_.values(), key=lambda d: d["last"], reverse=True), push


def summary(people):
    """{person id: {"signins": n, "failed": n since the last sign-in}} for People & logins."""
    out = {}
    for e in LoginEvent.objects.filter(person__in=people).exclude(kind="signout").order_by("at").values("person_id", "kind"):
        s = out.setdefault(e["person_id"], {"signins": 0, "failed": 0})
        if e["kind"] == "signin":
            s["signins"] += 1
            s["failed"] = 0
        else:
            s["failed"] += 1
    return out

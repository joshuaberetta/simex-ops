from django.http import JsonResponse
from django.shortcuts import redirect


class HealthCheck:
    """Answer /healthz before host validation: platform health checks may not send our Host."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/healthz":
            from . import conf
            return JsonResponse({"ok": True, "scheduler_tick": conf.get("scheduler_tick") or None})
        return self.get_response(request)


class MailHost:
    """With MAIL_URL set, the participants' SIMEX Mail lives on its own host (mail.): that host
    serves only the mailbox, sign-in and static files, and sends anything else to PUBLIC_URL;
    PUBLIC_URL sends a participant's own mailbox pages to the mail host. ExCon's "view as team"
    (/inbox/as/) and attachment links (/inbox/file/) stay on PUBLIC_URL. Sessions are shared."""

    MAIL_PATHS = ("/inbox/", "/login/", "/logout/", "/static/")
    EXCON_PATHS = ("/inbox/as/", "/inbox/file/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings
        request.mail_host = bool(settings.MAIL_HOST) and request.get_host().split(":")[0] == settings.MAIL_HOST
        if settings.MAIL_HOST and request.method in ("GET", "HEAD"):
            path = request.path
            if request.mail_host:
                if path != "/" and (not path.startswith(self.MAIL_PATHS) or path.startswith("/inbox/as/")):
                    return redirect(settings.PUBLIC_URL.rstrip("/") + request.get_full_path())
            elif path.startswith("/inbox/") and not path.startswith(self.EXCON_PATHS):
                return redirect(settings.MAIL_URL + request.get_full_path())
        return self.get_response(request)


class LastSeen:
    """Stamp the signed-in person's Person.last_seen_at (ops/activity.py), at most once a minute."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        resp = self.get_response(request)
        if request.path.startswith("/static/"):
            return resp
        from . import activity
        activity.seen(request)
        return resp

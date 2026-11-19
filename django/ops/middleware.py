from django.http import JsonResponse


class HealthCheck:
    """Answer /healthz before host validation: platform health checks may not send our Host."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/healthz":
            from . import conf
            return JsonResponse({"ok": True, "scheduler_tick": conf.get("scheduler_tick") or None})
        return self.get_response(request)

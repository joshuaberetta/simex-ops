"""A memo for one web request: settings, personas, team leaders and the like that a page reads
many times over (each poll and page read the same Setting, Sender and Person rows again and
again). Nothing is stored beyond the request: the middleware starts an empty memo when a
request comes in and drops it when the response leaves, so the next request reads the
database afresh, and nothing is ever written through it.

Outside a request (the scheduler, management commands, threads a request starts: contextvars
are not inherited by threading.Thread) there is no memo and every call reads the database, as
before. Any save or delete of a Setting, Sender, Team or Person in the request empties it (the
signals below), so a request that changes one reads the change back."""

import contextvars

_memo = contextvars.ContextVar("simex_reqcache", default=None)


def active():
    """Whether this code runs in a web request (with a memo)."""
    return _memo.get() is not None


def memo(key, fn):
    """fn()'s value, once per request under key (every time outside a request)."""
    d = _memo.get()
    if d is None:
        return fn()
    if key not in d:
        d[key] = fn()
    return d[key]


def clear(*_args, **_kwargs):
    d = _memo.get()
    if d is not None:
        d.clear()


class Middleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _memo.set({})
        try:
            return self.get_response(request)
        finally:
            _memo.reset(token)


def connect():
    """Called from OpsConfig.ready(): a write to one of the memoised models empties the memo."""
    from django.contrib.auth import get_user_model
    from django.db.models.signals import m2m_changed, post_delete, post_save

    from .models import Person, Sender, Setting, Team
    for model in (Setting, Sender, Team, Person):
        post_save.connect(clear, sender=model, dispatch_uid=f"reqcache-save-{model.__name__}")
        post_delete.connect(clear, sender=model, dispatch_uid=f"reqcache-delete-{model.__name__}")
    m2m_changed.connect(clear, sender=get_user_model().groups.through, dispatch_uid="reqcache-groups")  # is_excon

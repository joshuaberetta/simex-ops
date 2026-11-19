"""Who may see what. ExCon (staff or group "excon") sees everything; a participant sees
only their own team's inbox. Every ExCon view is wrapped in excon_required."""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def is_excon(user):
    return user.is_authenticated and user.is_active and (
        user.is_staff or user.is_superuser or user.groups.filter(name="excon").exists())


def participant_of(user):
    """The Person (with a team) behind a participant login, or None."""
    if not user.is_authenticated:
        return None
    person = getattr(user, "person", None)
    return person if person and person.active and person.team_id else None


def excon_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_excon(request.user):
            raise PermissionDenied("ExCon only.")
        return view(request, *args, **kwargs)
    wrapper.excon_only = True
    return wrapper


def login_required_any(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        return view(request, *args, **kwargs)
    return wrapper

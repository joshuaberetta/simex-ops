"""Who may see what. ExCon (staff or group "excon") sees everything; a participant (team
member) sees only their own team's inbox; a team leader sees their own mailbox, their team's
inbox read-only and their team's Dashboard, never another team. Every ExCon view is wrapped
in excon_required, every leader-only view in leader_required."""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse


def is_excon(user):
    return user.is_authenticated and user.is_active and (
        user.is_staff or user.is_superuser or user.groups.filter(name="excon").exists())


def participant_of(user):
    """The team member (a Person with a team) behind a participant login, or None."""
    if not user.is_authenticated:
        return None
    person = getattr(user, "person", None)
    return person if person and person.active and person.team_id and person.role == "member" else None


def leader_of(user):
    """The team leader (ExCon, role tl, with a team) behind a login, or None."""
    if not user.is_authenticated:
        return None
    person = getattr(user, "person", None)
    return person if person and person.active and person.team_id and person.role == "tl" else None


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


def leader_required(view):
    """A team leader's own pages (Team inbox, Dashboard). ExCon has its own views of both."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not leader_of(request.user):
            raise PermissionDenied("Team leaders only.")
        return view(request, *args, **kwargs)
    wrapper.leader_only = True
    return wrapper


def login_required_any(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            if request.headers.get("x-requested-with") == "fetch":  # the page's script says so, keeping the text
                return JsonResponse({"error": "You are signed out: sign in again."}, status=401)
            return redirect_to_login(request.get_full_path())
        return view(request, *args, **kwargs)
    return wrapper

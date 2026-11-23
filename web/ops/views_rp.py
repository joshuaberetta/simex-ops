"""A role player's pages in SIMEX Mail (/inbox/rp/, ops/roleplay.py): what's next and when,
their day, each role play's brief (script, outcomes, documents, notes, files, who else plays
it), the briefing and FAQ for every role player, and "In position" / "Done" for each sitting.
For role players (role rp) and team leaders with role plays to play; every view @rp_required,
showing only the viewer's own sittings. Alerts: the SIMEX Mail service worker and
PushSubscription, as for mail (reminders from roleplay.tick)."""

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import conf, push, roleplay, signin
from .access import rp_required
from .models import RP_STATUSES, PushSubscription, RolePlay, RolePlayFile, RolePlaySeen, RolePlaySlot
from .views_inbox import PUSH_HOSTS


def _seen(person):
    RolePlaySeen.objects.update_or_create(person=person, defaults={"at": timezone.now()})


def _ctx(request, **extra):
    person = request.rp_person
    return {"me": person, "leader": person.role == "tl", "vapid": push.public_key(), "v": roleplay.version(),
            "app_url": signin.path(request.user) + "?app=1" if signin.can_use(request.user) else "", **extra}


@rp_required
def home(request):
    person = request.rp_person
    now = timezone.now()
    slots = roleplay.slots_for(person)
    for s in slots:
        s.team_list = list(s.teams.all())
        s.local = timezone.localtime(s.start) if s.start else None
        s.others = [p for p in roleplay.players_of(s) if p.id != person.id]
        s.past = s.status in ("done", "skipped") or (s.start and s.end() and s.end() < now and s.status != "ready")
    upcoming = [s for s in slots if s.status in ("pending", "ready") and s.start and (s.end() or s.start) >= now]
    current = next((s for s in upcoming if s.start <= now), None)
    nxt = next((s for s in upcoming if s.start > now), None)
    _seen(person)
    return render(request, "ops/rp/home.html", _ctx(request, tab="home", slots=slots, current=current, next=nxt, now=now,
                                                     statuses=RP_STATUSES, briefing=conf.get("rp_briefing"),
                                                     n_plays=len({s.play_id for s in slots})))


def _mine(person, play):
    return play.players.filter(pk=person.pk).exists()


@rp_required
def play(request, pid):
    person = request.rp_person
    p = get_object_or_404(RolePlay, pk=pid)
    if not _mine(person, p):
        raise Http404
    slots = [s for s in roleplay.slots_for(person) if s.play_id == p.id]
    for s in slots:
        s.team_list = list(s.teams.all())
        s.local = timezone.localtime(s.start) if s.start else None
    others = [x for x in p.players.all() if x.id != person.id]
    return render(request, "ops/rp/play.html", _ctx(request, tab="home", play=p, slots=slots, others=others, files=p.files.all(),
                                                     statuses=RP_STATUSES))


@rp_required
@require_POST
def slot_status(request, sid):
    person = request.rp_person
    s = get_object_or_404(RolePlaySlot.objects.select_related("play"), pk=sid)
    if person not in roleplay.players_of(s):
        raise Http404
    status = request.POST.get("status")
    if status not in ("ready", "done", "pending"):
        raise Http404
    roleplay.set_status(s, status, person.name)
    if request.headers.get("x-requested-with") == "fetch":
        return JsonResponse({"ok": True})
    return redirect(request.POST.get("back") if (request.POST.get("back") or "").startswith("/inbox/rp/")
                    else "/inbox/rp/")


@rp_required
def file_get(request, fid):
    from .views_roleplay import file_response
    f = get_object_or_404(RolePlayFile.objects.select_related("play"), pk=fid)
    if not _mine(request.rp_person, f.play):
        raise Http404
    return file_response(f)


@rp_required
def briefing(request):
    return render(request, "ops/rp/briefing.html", _ctx(request, tab="briefing", briefing=conf.get("rp_briefing"),
                                                         faq=conf.get("rp_faq")))


@rp_required
def setup(request):
    """After a QR sign-in: alerts on this device (on an iPhone, Add to Home Screen first)."""
    return render(request, "ops/rp/setup.html", _ctx(request))


@rp_required
def poll(request):
    v = roleplay.version()
    return JsonResponse({"v": v, "changed": request.GET.get("v") != v})


@rp_required
@require_POST
def push_subscribe(request):
    """Save (or with "off", remove) this device's push subscription for the role player."""
    P = request.POST
    endpoint = (P.get("endpoint") or "").strip()
    host = endpoint.split("://", 1)[-1].split("/")[0].lower()
    if not endpoint.startswith("https://") or not any(host == h.lstrip(".") or host.endswith(h) for h in PUSH_HOSTS):
        return JsonResponse({"error": "Unknown push service."}, status=400)
    if P.get("off"):
        PushSubscription.objects.filter(endpoint=endpoint, person=request.rp_person).delete()
        return JsonResponse({"ok": True})
    if not (P.get("p256dh") and P.get("auth")):
        return JsonResponse({"error": "Incomplete subscription."}, status=400)
    PushSubscription.objects.update_or_create(endpoint=endpoint, defaults={
        "person": request.rp_person, "p256dh": P["p256dh"][:200], "auth": P["auth"][:100],
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:400]})
    return JsonResponse({"ok": True})

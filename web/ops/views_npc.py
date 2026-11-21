"""ExCon views for the AI personas (ops/npc.py): the AI personas tab (shown once Settings → AI
personas is on), each persona's profile, timing, queue and sandbox, and that setting. Every view
is excon_required."""

import re

from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import ai, conf, npc
from . import render as rnd
from .access import excon_required
from .models import NpcProfile, NpcReply, NpcSandbox, Sender, audit
from .views_ai import ai_status
from .views_excon import _int, _num, _u

TEXT_MAX = 20_000
OPEN = ["queued", "writing", "held", "failed"]


def _jobs(qs):
    return qs.select_related("npc__sender", "team", "response", "message")


@excon_required
def npcs_page(request):
    """The list, the queue of answers across personas, and the forms to add one (blank, from an
    existing persona, or generated from a brief)."""
    npc.expire()
    if request.method == "POST":
        P = request.POST
        if P.get("brief") is not None:
            brief = P.get("brief", "").strip()
            if not brief:
                messages.error(request, "Describe the persona to generate.")
            elif not ai.enabled():
                messages.error(request, "AI analysis is off (Settings → AI analysis).")
            elif not ai.under_cap():
                messages.error(request, "Today's AI spending cap is reached (Settings → AI analysis).")
            else:
                p = npc.generate(brief, _u(request))
                return redirect(f"/npcs/{p.id}/")
            return redirect("/npcs/")
        if P.get("seed") is not None:  # every persona the injects use, profiled from its injects
            if not ai.enabled():
                messages.error(request, "AI analysis is off (Settings → AI analysis).")
            elif not ai.under_cap():
                messages.error(request, "Today's AI spending cap is reached (Settings → AI analysis).")
            else:
                ids = npc.seed(npc.seedable(), user=_u(request))
                messages.success(request, f"Writing {len(ids)} profile{'s' if len(ids) != 1 else ''} from the injects: "
                                 "each is off until you have read it and saved it." if ids else
                                 "Every persona the injects use has a profile already.")
            return redirect("/npcs/")
        s = Sender.objects.filter(pk=_int(P.get("sender")), team_leader=False).first()
        if not s or NpcProfile.objects.filter(sender=s).exists():
            messages.error(request, "Pick a persona that isn't an AI persona yet (or the team leader persona).")
            return redirect("/npcs/")
        if "{" in s.local_part:
            messages.error(request, f"{s.local_part} is filled in per team ({{team}}): an AI persona needs one address.")
            return redirect("/npcs/")
        if P.get("ai") == "1" and ai.enabled() and ai.under_cap():
            p = NpcProfile.objects.get(pk=npc.seed([s], P.get("hint", ""), _u(request))[0])
        else:
            p = NpcProfile(sender=s)
            npc.apply_timing(p, "responsive")
            p.save()
            audit("npc_create", s.local_part, _u(request))
        return redirect(f"/npcs/{p.id}/")
    profiles = list(NpcProfile.objects.select_related("sender").annotate(
        n_open=Count("replies", filter=Q(replies__status__in=OPEN)),
        n_sent=Count("replies", filter=Q(replies__status="sent"))))
    for p in profiles:
        p.timing_text = npc.timing_text(p)
    return render(request, "ops/npcs.html", {
        "tab": "npcs", "profiles": profiles,
        "queue": _jobs(NpcReply.objects.filter(status__in=OPEN)).order_by("due_at"),
        "recent": _jobs(NpcReply.objects.exclude(status__in=OPEN)).order_by("-updated_at")[:20],
        "free": Sender.objects.filter(team_leader=False, npc__isnull=True).exclude(local_part__contains="{"),
        "seedable": npc.seedable(), "writing": any(p.gen_status == "running" for p in profiles),
        "on": npc.active(), "scope": conf.get("npc_scope"), **ai_status()})


def _hour(v):
    n = _int(v)
    return n if n is not None and 0 <= n <= 23 else None


def _chance(v):
    n = _num(v)
    return min(1.0, max(0.0, n / 100)) if n is not None else 0.0


def _save(request, p):
    """The profile form. Returns an error message, or None once saved."""
    P, s = request.POST, p.sender
    local = re.sub(r"@.*$", "", (P.get("local_part") or "").strip().lower())
    if not rnd.LOCAL.match(local) or "{" in local:
        return f"'{local}' is not a valid username (letters, digits, . _ + -)."
    if Sender.objects.filter(local_part=local).exclude(pk=s.pk).exists():
        return f"The username {local} is already used by another persona."
    inapp = re.sub(r"@.*$", "", (P.get("inapp_local") or "").strip().lower())
    if inapp and not rnd.LOCAL.match(inapp):
        return f"'{inapp}' is not a valid username."
    domain = (P.get("domain") or "").strip().lower().lstrip("@")
    if domain and (not rnd.DOMAIN.match(domain) or "{" in domain or domain.endswith(f"teams.{settings.MAIL_DOMAIN}")):
        return f"'{domain}' is not a valid domain (e.g. ndma.go.ep)."
    old = s.local_part
    s.local_part, s.display_name = local, (P.get("display_name") or "").strip()[:200]
    s.inapp_local, s.domain = ("" if inapp == local else inapp), domain
    clash = next((o for o in Sender.objects.exclude(pk=s.pk) if o.inapp_address == s.inapp_address), None)
    if clash:
        return f"{s.inapp_address} is already the SIMEX Mail address of {clash.local_part}."
    s.active = P.get("active") == "1"
    for f in ("organisation", "role"):
        setattr(p, f, (P.get(f) or "").strip()[:200])
    for f in ("personality", "knowledge", "boundaries"):
        setattr(p, f, (P.get(f) or "").replace("\r\n", "\n").strip()[:TEXT_MAX])
    preset = P.get("timing") if P.get("timing") in npc.TIMINGS else "custom"
    if preset == "custom":
        lo, hi = _num(P.get("min_minutes")), _num(P.get("max_minutes"))
        p.min_minutes = max(0.0, lo if lo is not None else p.min_minutes)
        p.max_minutes = max(p.min_minutes, hi if hi is not None else p.max_minutes)
        p.quick_chance, p.no_reply_chance = _chance(P.get("quick_pct")), _chance(P.get("none_pct"))
    npc.apply_timing(p, preset)
    p.office_from, p.office_to = _hour(P.get("office_from")), _hour(P.get("office_to"))
    p.enabled, p.auto_send = P.get("enabled") == "1", P.get("auto_send") == "1"
    s.save()
    if p.gen_status == "done":
        p.gen_status = ""  # read and saved by ExCon
    p.save()
    audit("npc_save", f"{local}{f' (was {old})' if old != local else ''} enabled={p.enabled}", _u(request))
    return None


@excon_required
def npc_page(request, pid):
    npc.expire()
    p = get_object_or_404(NpcProfile.objects.select_related("sender"), pk=pid)
    if request.method == "POST":
        err = _save(request, p)
        if err:
            messages.error(request, err)
        else:
            messages.success(request, f"Saved {p.sender.display_name or p.sender.local_part}"
                             + ("." if p.enabled else ": it won't answer until you tick Answers on its own."))
        return redirect(f"/npcs/{p.id}/")
    p.timing_text = npc.timing_text(p)
    box = NpcSandbox.objects.filter(npc=p, user=_u(request)).order_by("-id").first()
    return render(request, "ops/npc.html", {
        "tab": "npcs", "p": p, "s": p.sender, "TIMINGS": npc.TIMINGS, "FLAGS": npc.FLAGS, "box": box,
        "quick_pct": round(p.quick_chance * 100), "none_pct": round(p.no_reply_chance * 100),
        "jobs": _jobs(p.replies.all()).order_by("-id")[:50], "on": npc.active(), "scope": conf.get("npc_scope"),
        "refresh": p.gen_status == "running" or (box and box.status == "running"),
        "now": timezone.now(), **ai_status()})


@excon_required
@require_POST
def npc_try(request, pid):
    """The sandbox: ExCon writes as a team, the persona answers; nothing is delivered."""
    p = get_object_or_404(NpcProfile, pk=pid)
    box = NpcSandbox.objects.filter(npc=p, user=_u(request)).order_by("-id").first()
    if request.POST.get("reset") or not box:
        NpcSandbox.objects.filter(npc=p, user=_u(request)).delete()
        box = NpcSandbox.objects.create(npc=p, user=_u(request))
        if request.POST.get("reset"):
            return redirect(f"/npcs/{p.id}/#try")
    text = (request.POST.get("text") or "").strip()
    if not text:
        messages.error(request, "Write a message to try it with.")
    elif box.status == "running":
        messages.error(request, "It is still answering your last message.")
    elif not ai.enabled():
        messages.error(request, "AI analysis is off (Settings → AI analysis).")
    elif not ai.under_cap():
        messages.error(request, "Today's AI spending cap is reached (Settings → AI analysis).")
    else:
        npc.try_it(box, text)
    return redirect(f"/npcs/{p.id}/#try")


@excon_required
@require_POST
def npc_delete(request, pid):
    p = get_object_or_404(NpcProfile.objects.select_related("sender"), pk=pid)
    audit("npc_delete", p.sender.local_part, _u(request))
    p.delete()
    messages.success(request, f"{p.sender.local_part} is no longer an AI persona (it stays on the Senders tab).")
    return redirect("/npcs/")


@excon_required
@require_POST
def npc_reply_action(request, jid):
    """A queued, held or failed answer: write it now, send it (edited), skip it, or try again."""
    job = get_object_or_404(NpcReply.objects.select_related("npc__sender", "response", "team"), pk=jid)
    act, now = request.POST.get("action"), timezone.now()
    back = request.POST.get("next") if (request.POST.get("next") or "").startswith("/npcs/") else f"/npcs/{job.npc_id}/"
    if act == "now" and job.status == "queued":
        NpcReply.objects.filter(pk=job.pk, status="queued").update(due_at=now)
        messages.success(request, "It answers within a minute (the scheduler writes it).")
    elif act == "send" and job.status == "held":
        body = (request.POST.get("body") or "").strip()
        if not body:
            messages.error(request, "The answer is empty.")
        else:
            npc.send(job, body, _u(request))
            if job.status == "failed":
                messages.error(request, f"Not sent to {job.team.name}: {job.error}")
            else:
                messages.success(request, f"Sent to {job.team.name}.")
    elif act == "skip" and job.status in ("queued", "held", "failed"):
        NpcReply.objects.filter(pk=job.pk).update(status="skipped", note=f"Skipped by {_u(request)}.", updated_at=now)
    elif act == "retry" and job.status in ("failed", "skipped"):
        NpcReply.objects.filter(pk=job.pk).update(status="queued", due_at=now, attempts=0, error=None, note="",
                                                  updated_at=now)
        messages.success(request, "It answers within a minute (the scheduler writes it).")
    else:
        messages.error(request, "That answer has moved on: nothing changed.")
        return redirect(back)
    audit(f"npc_reply_{act}", f"#{job.id} {job.npc.sender.local_part} -> {job.team.name}", _u(request))
    return redirect(back)


@excon_required
@require_POST
def settings_npc(request):
    P = request.POST
    conf.put("npc_enabled", "1" if P.get("npc_enabled") == "1" else "0")
    conf.put("npc_scope", "all" if P.get("npc_scope") == "all" else "test")
    audit("settings_npc", f"enabled={conf.get('npc_enabled')} scope={conf.get('npc_scope')}", _u(request))
    messages.success(request, "AI personas: " + ("on, for " + ("every team." if conf.get("npc_scope") == "all"
                                                               else "the Test team only.") if npc.active() else "off."))
    return redirect("/settings/#npc")

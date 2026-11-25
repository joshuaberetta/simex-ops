"""The persona desk with an AI persona's answer not sent yet (ops/npc.py): its Scheduled folder
and the panel at the foot of the conversation, where a person reads SIMEX AI's draft, edits it,
sends it now, changes when it goes or cancels it to answer themselves. Desk members and ExCon
(views_inbox._desk_page)."""

from datetime import timedelta

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import desk, npc
from .access import desk_of
from .models import NpcReply
from .views_excon import from_local_input
from .views_inbox import _desk_page

TEXT_MAX = 20_000
MINUTES = (5, 15, 30, 60, 120, 240)


def _who(request):
    person = desk_of(request.user)
    return person.name if person else (request.user.get_full_name() or request.user.get_username())


@_desk_page
@require_POST
def answer(request, jid):
    """act: save (the text), send (now, with the text), time (in minutes, or at), cancel (a
    person answers, or nobody), retry (a failed one: SIMEX AI writes it again)."""
    P = request.POST
    job = get_object_or_404(NpcReply.objects.select_related("npc__sender", "team", "response", "mail"), pk=jid)
    act, who, body = P.get("act", ""), _who(request), (P.get("body") or "")[:TEXT_MAX]
    ajax = request.headers.get("x-requested-with") == "fetch"
    nxt = P.get("next") or ""
    nxt = nxt if nxt.startswith("/inbox/desk/") else "/inbox/desk/?folder=scheduled"
    done = ""
    token = desk.ACTOR.set(request.user)  # those on the conversation hear of it, not the sender
    try:
        if act == "save":
            npc.edit(job, body, who)
            done = "Saved: it goes with your text."
        elif act == "send":
            npc.send_now(job, body, who, request.user.get_username())
            done = f"Sent as {job.npc.sender.display_name or job.npc.sender.local_part}."
        elif act == "time":
            if P.get("at"):
                try:
                    due = from_local_input(P["at"])
                except ValueError:
                    raise npc.JobError("That time isn't a date and time.")
            else:
                try:
                    minutes = int(P.get("minutes") or 0)
                except ValueError:
                    minutes = 0
                if minutes not in MINUTES:
                    raise npc.JobError("Choose when it goes.")
                due = timezone.now() + timedelta(minutes=minutes)
            if due < timezone.now() - timedelta(minutes=1):
                raise npc.JobError("That time has passed: send it now instead.")
            if body.strip() and body.strip() != job.body:
                npc.edit(job, body, who)
            npc.reschedule(job, due, who)
            done = f"It goes at {timezone.localtime(due):%a %H:%M}."
        elif act == "cancel":
            npc.cancel(job, who)
            done = "Cancelled: answer it yourself (or mark it as needing no reply)."
        elif act == "retry" and job.status == "failed":
            NpcReply.objects.filter(pk=job.pk, status="failed").update(
                status="queued", attempts=0, error=None, note="", updated_at=timezone.now())
            done = "SIMEX AI writes it again."
        else:
            raise npc.JobError("Unknown action.")
    except npc.JobError as e:
        if ajax:
            return JsonResponse({"ok": False, "error": str(e)}, status=400)
        messages.error(request, str(e))
        return redirect(nxt)
    finally:
        desk.ACTOR.reset(token)
    if ajax:
        return JsonResponse({"ok": True, "message": done})
    messages.success(request, done)
    return redirect(nxt)

"""Claude's reading of the persona desk's waiting conversations (DeskConversation ai_*): does
the teams' latest message need an answer from the persona (reply | optional | none), how
urgent, and what they ask, in a line, so the people on the desk see what to answer first and
which "thanks, noted" messages they can close. Only the conversation itself is read (what the
teams and the personas wrote in it): nothing control-side.

Run by the scheduler's AI loop (ai.tick -> tick here), never in a request: every waiting
conversation once per new team message (automatic mode, Settings → AI analysis "ai_auto"),
else when a desk member asks (desk.mark "triage"). Settings → AI analysis "desk_triage" (on);
"ai_tests" for the Test team. A suggestion for the desk: it never closes or answers anything.
"""

import logging

from django.utils import timezone

from . import ai, conf, desk
from .models import DeskConversation, Mail, Team

log = logging.getLogger("simex.ai")

BODY_MAX, MESSAGES_MAX = 3000, 10

SCHEMA = {
    "type": "object",
    "properties": {
        "need": {"type": "string", "enum": ["reply", "optional", "none"]},
        "urgency": {"type": "string", "enum": ["high", "normal", "low"]},
        "ask": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["need", "urgency", "ask", "reason"],
    "additionalProperties": False,
}

RULES = """You help the persona desk: the ExCon role-players who answer, as the personas, the messages \
the teams send. Many messages arrive at once; read the conversation below and say whether the team's \
latest message (or messages) to the persona still needs the persona to answer.
- need: "reply" when the team asks a question, makes a request, asks for a decision, confirmation or \
information, or would reasonably expect the persona to answer; "optional" when an answer would be natural \
but nothing is asked; "none" when it only acknowledges, thanks, confirms receipt, closes the exchange or \
says something needs no answer.
- urgency: "high" when it is time-critical in the scenario (lives, safety, a deadline, a meeting soon, an \
escalation, a complaint), "low" when nothing depends on it soon, else "normal".
- ask: what they want from the persona, in at most 15 words (empty for need "none").
- reason: why, in at most 20 words.
Write in English. Base it on the messages only; never invent what the persona knows."""


def enabled():
    return ai.enabled() and conf.flag("desk_triage")


def conversation_text(thread):
    """The conversation as the model reads it: the latest messages, oldest first."""
    mails = list(Mail.objects.filter(team=None, owner=None, thread=thread, trashed_at__isnull=True)
                 .exclude(folder="drafts").prefetch_related("files").order_by("-id")[:MESSAGES_MAX])[::-1]
    teams = dict(Team.objects.values_list("id", "name"))
    memo, parts = {}, []
    for m in mails:
        persona = desk.describe(m.persona, memo=memo)["name"] if m.persona else "a persona"
        team = teams.get(m.desk_team_id, "the team")
        who = f"{team} ({m.from_name or m.from_addr}) to {persona}" if m.folder == "inbox" else f"{persona} to {team}"
        at = timezone.localtime(m.sim_at or m.created_at).strftime("%a %d %b %H:%M")
        body = (m.body or "").strip()
        if len(body) > BODY_MAX:
            body = body[:BODY_MAX] + " [...]"
        files = ", ".join(f.filename for f in m.files.all())
        parts.append(f"--- {who}, {at}\nSubject: {m.subject}\n{body}" + (f"\n[Attached: {files}]" if files else ""))
    return "\n\n".join(parts)


def triage(c):
    """Read one waiting conversation; stores the reading on c."""
    st = desk.states([c.thread]).get(c.thread)
    if not st or not st["waiting"]:
        c.ai_status, c.ai_upto = "", st["last_in"] if st else 0
        c.save()
        return c
    content = [{"type": "text", "text": "THE CONVERSATION (the latest message last):\n\n" + conversation_text(c.thread)}]
    try:
        out, _ = ai.call("desk", f"k{c.id}", "fast", ai.system("desk"), content, SCHEMA, "low", 2000)
    except ai.AiError as e:
        c.ai_status, c.ai_error, c.ai_upto = "failed", str(e)[:500], st["last_in"]
        c.save()
        return c
    c.ai_need = out.get("need") if out.get("need") in ("reply", "optional", "none") else "reply"
    c.ai_urgency = out.get("urgency") if out.get("urgency") in desk.URGENCY else "normal"
    c.ai_ask, c.ai_reason = (out.get("ask") or "")[:300], (out.get("reason") or "")[:400]
    c.ai_status, c.ai_error, c.ai_upto, c.ai_at = "done", "", st["last_in"], timezone.now()
    c.save()
    return c


def pending():
    """Conversations to read: those a desk member asked for, and (automatic mode) every waiting
    one with a team message newer than its last reading, oldest first."""
    asked = list(DeskConversation.objects.filter(ai_status="queued").order_by("updated_at"))
    out = list(asked)
    if conf.flag("ai_auto"):
        inbox = Mail.objects.filter(team=None, owner=None, folder="inbox", trashed_at__isnull=True)
        if not conf.flag("ai_tests"):
            inbox = inbox.exclude(is_test=True)
        latest = {}
        for t, mid in inbox.order_by("id").values_list("thread", "id"):
            latest[t] = mid
        read = dict(DeskConversation.objects.filter(thread__in=list(latest)).values_list("thread", "ai_upto"))
        fresh = [t for t, mid in latest.items() if mid > read.get(t, 0)]
        for t, st in desk.states(fresh).items():
            if st["waiting"] and t not in {c.thread for c in asked}:
                out.append(desk.conversation(t, create=True))
    return out


def tick(limit):
    """Called from ai.tick: reads up to limit waiting conversations."""
    if not enabled():
        return 0
    n = 0
    for c in pending()[:limit]:
        if not ai.under_cap():
            break
        try:
            triage(c)
        except Exception as e:  # one bad conversation never holds up the rest
            log.exception("desk triage failed")
            last = (Mail.objects.filter(team=None, owner=None, thread=c.thread, folder="inbox")
                    .order_by("-id").values_list("id", flat=True).first() or 0)
            DeskConversation.objects.filter(pk=c.pk).update(ai_status="failed", ai_error=f"{type(e).__name__}: {e}"[:500],
                                                            ai_upto=last)
        n += 1
    return n


ai.INSTRUCTIONS["desk"] = {"label": "Persona desk: what needs an answer", "default": RULES,
                           "help": "Reads each waiting conversation on the persona desk: whether the team's latest "
                                   "message needs the persona to answer, how urgent, and what they ask."}

"""Claude analysis of participants' replies, for ExCon only.

Two readings, both run by the scheduler worker (ai_loop in run_scheduler), never in a request:
- each reply, with the fast model (Claude Haiku 5.5): summary, sentiment and tone, quality,
  what the team asks for and commits to, flags, and an estimate of how likely the text is
  AI-written (ReplyAnalysis);
- each inject x team thread with a rubric, with the deep model (Claude Sonnet 5.5): suggested
  points per criterion with a rationale and quoted evidence (AiGrade). Suggestions only: the
  human Grade is never touched and AI points never count in totals.
Plus an on-demand exercise summary (AiReport) for the Dashboard.

The model sees what the app already holds: the participant-facing inject, the rubric ExCon
wrote, and the replies. No control-side material exists here to send.

Provider (settings.AI_PROVIDER): the Claude API (ANTHROPIC_API_KEY) or Amazon Bedrock (AWS
credentials: AWS_BEARER_TOKEN_BEDROCK, or a profile / SSO session locally). Every call is
logged as an AiCall with its estimated cost; Settings → AI analysis sets a daily cap.
"""

import hashlib
import json
import logging
import re
import threading
from datetime import timedelta

from django.conf import settings
from django.db.models import Max, Sum
from django.utils import timezone

from . import conf, inbound
from .render import fill
from .models import AiCall, AiGrade, AiReport, Inject, ReplyAnalysis, Response, RubricCriterion, Sender, Team

log = logging.getLogger("simex.ai")

PROMPT_VERSION = 1
# USD per million tokens (input, output): Anthropic list prices. Bedrock bills its own rates,
# so costs here are estimates either way.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}
TONES = ["calm", "urgent", "stressed", "confused", "defensive", "assertive", "collaborative"]
FLAGS = {
    "misread": "Misreads the inject",
    "escalate": "Needs ExCon attention",
    "safety": "Safety or duty-of-care issue",
    "wrong_recipient": "Sent to the wrong persona",
    "out_of_role": "Steps out of the exercise",
    "no_action": "No action or decision",
}
SETTLE = timedelta(minutes=3)  # teams often send a quick follow-up: grade the thread once it is quiet
RETRY_AFTER = timedelta(minutes=2)
MAX_ATTEMPTS = 3
BATCH = 3  # calls per worker pass


class AiError(Exception):
    pass


# --- client ------------------------------------------------------------------------------------

_client = None
_lock = threading.Lock()
_no_fallbacks = False  # set if the API rejected server-side fallbacks once


def set_client(client):
    """Tests: a fake with .messages.create (and .beta.messages.create)."""
    global _client
    _client = client


def provider():
    return settings.AI_PROVIDER if settings.AI_PROVIDER in ("anthropic", "bedrock") else "off"


def available():
    return _client is not None or provider() != "off"


def enabled():
    return available() and conf.flag("ai_enabled")


def client():
    global _client
    with _lock:
        if _client is None:
            import anthropic
            if provider() == "anthropic":
                _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=180, max_retries=3)
            elif provider() == "bedrock":
                _client = anthropic.AnthropicBedrock(aws_region=settings.AI_REGION, timeout=180, max_retries=3)
            else:
                raise AiError("No AI provider configured (AI_PROVIDER).")
        return _client


def model_id(kind):
    """kind: fast | deep. Bedrock takes the us. inference profile of the same model."""
    explicit = settings.AI_MODEL_FAST if kind == "fast" else settings.AI_MODEL_DEEP
    if explicit:
        return explicit
    base = "claude-haiku-5-5" if kind == "fast" else "claude-sonnet-5-5"
    return f"us.anthropic.{base}" if provider() == "bedrock" else base


def base_model(model):
    """us.anthropic.claude-haiku-5-5 / arn:...:inference-profile/us.anthropic.x -> claude-haiku-5-5"""
    name = model.rsplit("/", 1)[-1].rsplit(".", 1)[-1] if "claude" in model else model
    return re.sub(r"(-v\d+)?(:\d+)?$", "", name)


def cost(model, usage):
    pin, pout = PRICES.get(base_model(model), (0.0, 0.0))
    cache_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (usage.input_tokens * pin + cache_w * pin * 1.25 + cache_r * pin * 0.1 + usage.output_tokens * pout) / 1e6


def spent_today():
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return AiCall.objects.filter(at__gte=start).aggregate(s=Sum("cost_usd"))["s"] or 0.0


def cap():
    try:
        return max(0.0, float(conf.get("ai_daily_usd") or 0))
    except ValueError:
        return 0.0


def under_cap():
    return spent_today() < cap()


def call(purpose, ref, kind, system, content, schema, effort, max_tokens):
    """One structured-output request. Returns the parsed JSON object; raises AiError."""
    global _no_fallbacks
    import anthropic
    model = model_id(kind)
    params = dict(model=model, max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": content}],
                  output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}})
    c = client()
    try:
        # Claude API: if the deep model declines (a safety classifier on disaster content),
        # let the API retry on another model rather than fail the grade.
        if provider() == "anthropic" and kind == "deep" and not _no_fallbacks:
            try:
                r = c.beta.messages.create(**params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            except anthropic.BadRequestError as e:
                if "fallback" not in str(e).lower():
                    raise
                _no_fallbacks = True
                r = c.messages.create(**params)
        else:
            r = c.messages.create(**params)
    except anthropic.APIStatusError as e:
        AiCall.objects.create(purpose=purpose, ref=ref, model=model, ok=False, error=f"{e.status_code}: {e.message}"[:2000])
        raise AiError(f"{type(e).__name__} {e.status_code}: {e.message}"[:500]) from e
    except anthropic.APIConnectionError as e:
        AiCall.objects.create(purpose=purpose, ref=ref, model=model, ok=False, error=str(e)[:2000])
        raise AiError(f"Could not reach the model: {e}"[:500]) from e
    u = r.usage
    used = getattr(r, "model", None) or model
    row = AiCall.objects.create(purpose=purpose, ref=ref, model=used, input_tokens=u.input_tokens,
                                output_tokens=u.output_tokens, cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                                cost_usd=cost(used, u))
    if r.stop_reason == "refusal":
        cat = getattr(getattr(r, "stop_details", None), "category", None)
        row.ok, row.error = False, f"declined ({cat or 'no category'})"
        row.save(update_fields=["ok", "error"])
        raise AiError(f"The model declined to analyse this ({cat or 'no category'}).")
    if r.stop_reason == "max_tokens":
        row.ok, row.error = False, "max_tokens"
        row.save(update_fields=["ok", "error"])
        raise AiError("The answer was cut off (max_tokens).")
    text = next((b.text for b in r.content if getattr(b, "type", "") == "text"), "")
    try:
        return json.loads(text), used
    except ValueError as e:
        row.ok, row.error = False, "unparseable output"
        row.save(update_fields=["ok", "error"])
        raise AiError("The model's answer was not valid JSON.") from e


def _cached(text):
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def _hash(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


# --- shared context ----------------------------------------------------------------------------

EXERCISE = """You assist the Exercise Control (ExCon) team of an UNDAC simulation exercise (SIMEX). \
Teams of humanitarian professionals play an UNDAC team deployed to a simulated sudden-onset emergency. \
ExCon sends them scenario messages ("injects") from role-played personas (government officials, UN \
agencies, NGOs, media, donors), and the teams answer by email as they would in a real response: \
coordinating, requesting and sharing information, making decisions. Everything is fictional exercise play; \
casualty figures and hazards in the messages are part of the scenario."""


def persona_label(local):
    s = Sender.objects.filter(local_part=local).first() if local else None
    return f"{s.display_name} <{local}>" if s and s.display_name else (local or "unknown")


def inject_block(inj, team=None):
    """The inject as the team received it (placeholders filled for that team)."""
    ctx = {"team": team.code, "team_name": team.name, "name": team.name, "first_name": team.name} if team else {}
    sender = Sender.objects.filter(local_part=inj.sender_local).first() if inj.sender_local else None
    local = fill(inj.sender_local or "", ctx).lower()
    name = fill(inj.from_name or (sender.display_name if sender else ""), ctx)
    lines = [f"INJECT {inj.mel_id}", f"From: {name} <{local}>" if name else f"From: {local or 'unknown'}"]
    if inj.scenario_dt:
        lines.append(f"Scenario time: {inj.scenario_dt}")
    lines += [f"Subject: {fill(inj.subject or '', ctx)}", "", fill(inj.body or "", ctx).strip()]
    if inj.supporting_documents:
        lines += ["", f"Attached documents: {inj.supporting_documents}"]
    if inj.expects_reply:
        lines += ["", "ExCon expects a reply" + (f" within {inj.reply_due_minutes} minutes." if inj.reply_due_minutes else ".")]
    return "\n".join(lines)


def new_text(r):
    return inbound.split_quoted(r.text or "")[0].strip()


def files_of(r):
    if r.channel == "email":
        return [a.filename for a in r.attachments.all()]
    return [f.filename for f in r.mail.files.all()] if r.mail else []


def timing(r):
    """(minutes since the team first saw what it answers, words, words per minute or None)."""
    words = len(new_text(r).split())
    m = r.message
    start = (m.opened_at or m.sent_at) if m else None
    if not start or r.received_at < start:
        return None, words, None
    minutes = (r.received_at - start).total_seconds() / 60
    return round(minutes, 1), words, (round(words / max(minutes, 0.5), 1) if words >= 30 else None)


def reply_block(r, label="REPLY"):
    minutes, words, wpm = timing(r)
    who = r.person.name if r.person else (r.from_name or r.from_addr)
    role = " (team leader)" if r.person and r.person.role == "tl" else ""
    lines = [f"{label} from {who}{role}, {r.team.name if r.team else 'unknown team'}",
             f"To: {persona_label(r.persona)}",
             f"Subject: {r.subject or ''}"]
    if minutes is not None:
        lines.append(f"Sent {minutes:g} minutes after the team first opened the message it answers; {words} words.")
    files = files_of(r)
    if files:
        lines.append(f"Attachments: {', '.join(files)} (not shown)")
    lines += ["", new_text(r) or "(empty)"]
    return "\n".join(lines)


# --- per reply ---------------------------------------------------------------------------------

REPLY_SYSTEM = EXERCISE + """

You read one message a team sent and the inject it answers (if known), and describe the message for ExCon. \
Be factual and brief; report only what the message says. Judge it as professional humanitarian \
coordination communication.

- summary: one sentence, what the team says or does.
- sentiment: -2 (very negative: alarmed, hostile, despairing) to 2 (very positive); 0 neutral and businesslike.
- tone: the dominant tone.
- clarity, completeness (does it address what the inject asked), actionability (can the recipient act on it): 1 (poor) to 5 (excellent).
- asks: what the team requests from others; commitments: decisions taken or actions the team commits to; \
facts: figures, places and times it states. Short phrases, empty lists when there are none.
- flags, only when clearly true: misread (misunderstands the inject or scenario), escalate (ExCon should \
look now: e.g. a team stuck, a request ExCon must answer, a major decision), safety (a real-world safety, \
security or wellbeing concern, not scenario play), wrong_recipient (clearly meant for someone else), \
out_of_role (breaks the exercise frame), no_action (only a holding reply or acknowledgement: no answer, decision or \
commitment at all; never when the commitments list is not empty).
- ai_written: 0-100, how likely the text was produced by an AI assistant rather than written by the \
participant. Base it on concrete signals: chatbot phrasing and structure (headings, bold, symmetrical bullet \
lists unusual in a quick operational email), generic boilerplate, flawless long text written implausibly fast \
(the timing line), content that restates the inject rather than engaging with it. Short messages carry little \
evidence: stay between 10 and 40 without strong signals. Non-native English, formality and good writing are not \
signals. ai_signals: the signals you used, one sentence.
- language: the message's language (e.g. English)."""

REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "sentiment": {"type": "integer", "description": "-2 to 2"},
        "tone": {"type": "string", "enum": TONES},
        "clarity": {"type": "integer", "description": "1 to 5"},
        "completeness": {"type": "integer", "description": "1 to 5"},
        "actionability": {"type": "integer", "description": "1 to 5"},
        "asks": {"type": "array", "items": {"type": "string"}},
        "commitments": {"type": "array", "items": {"type": "string"}},
        "facts": {"type": "array", "items": {"type": "string"}},
        "flags": {"type": "array", "items": {"type": "string", "enum": list(FLAGS)}},
        "ai_written": {"type": "integer", "description": "0 to 100"},
        "ai_signals": {"type": "string"},
        "language": {"type": "string"},
    },
    "required": ["summary", "sentiment", "tone", "clarity", "completeness", "actionability", "asks", "commitments",
                 "facts", "flags", "ai_written", "ai_signals", "language"],
    "additionalProperties": False,
}


def _clamp(v, lo, hi):
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return None


def reply_hash(r):
    return _hash(PROMPT_VERSION, model_id("fast"), r.inject_id, r.subject, new_text(r))


def analyse_reply(r):
    """Read one Response with the fast model; stores and returns its ReplyAnalysis."""
    a, _ = ReplyAnalysis.objects.get_or_create(response=r, defaults={"status": "queued"})
    a.attempts += 1
    content = []
    if r.inject:
        content.append(_cached("The inject this message answers:\n\n" + inject_block(r.inject, r.team)))
    else:
        content.append({"type": "text", "text": "The inject this message answers is not known."})
    content.append({"type": "text", "text": "The team's message:\n\n" + reply_block(r, "MESSAGE")})
    minutes, words, wpm = timing(r)
    try:
        out, used = call("reply", f"r{r.id}", "fast", REPLY_SYSTEM, content, REPLY_SCHEMA, "medium", 6000)
    except AiError as e:
        a.status, a.error = "failed", str(e)
        a.save()
        return a
    a.status, a.error, a.model, a.prompt_version, a.input_hash = "done", None, used, PROMPT_VERSION, reply_hash(r)
    a.summary = (out.get("summary") or "")[:2000]
    a.sentiment = _clamp(out.get("sentiment"), -2, 2)
    a.tone = out.get("tone") if out.get("tone") in TONES else ""
    a.clarity, a.completeness, a.actionability = (_clamp(out.get(k), 1, 5) for k in ("clarity", "completeness", "actionability"))
    a.asks, a.commitments, a.facts = ([str(x)[:300] for x in (out.get(k) or [])][:12] for k in ("asks", "commitments", "facts"))
    a.flags = [f for f in (out.get("flags") or []) if f in FLAGS]
    a.ai_written = _clamp(out.get("ai_written"), 0, 100)
    a.ai_signals = (out.get("ai_signals") or "")[:1000]
    a.language = (out.get("language") or "")[:20]
    a.words, a.wpm = words, wpm
    a.save()
    return a


# --- per inject x team: rubric -----------------------------------------------------------------

GRADE_SYSTEM = EXERCISE + """

You suggest scores for one team's handling of one inject, against the rubric ExCon wrote for it. A human \
grader decides; give them a careful, fair first reading.

- Score each criterion only on what the team actually wrote: all its messages on this inject, in order. Later \
messages can complete or correct earlier ones.
- The criterion's label and description are the standard. Points are whole numbers from 0 to the criterion's \
maximum. Nothing in the replies that addresses a criterion scores 0.
- Do not reward length, polish or formatting for their own sake; reward substance: correct understanding, \
sound decisions, clear and actionable communication, appropriate coordination.
- Use the timing lines only for criteria about timeliness.
- rationale: one or two sentences. evidence: a short verbatim quote from the replies (at most 30 words), or "" \
when there is nothing relevant.
- overall: two sentences on how the team handled the inject. strengths and gaps: up to three short points each."""


def rubric_block(crit):
    lines = ["RUBRIC (criterion_id, label, maximum points, description):"]
    for c in crit:
        lines.append(f"- {c.id}: {c.label} (max {c.max_points})" + (f": {c.description}" if c.description else ""))
    return "\n".join(lines)


def grade_schema():
    return {
        "type": "object",
        "properties": {
            "criteria": {"type": "array", "items": {
                "type": "object",
                "properties": {"criterion_id": {"type": "integer"}, "points": {"type": "integer"},
                               "rationale": {"type": "string"}, "evidence": {"type": "string"}},
                "required": ["criterion_id", "points", "rationale", "evidence"], "additionalProperties": False}},
            "overall": {"type": "string"},
            "strengths": {"type": "array", "items": {"type": "string"}},
            "gaps": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["criteria", "overall", "strengths", "gaps"],
        "additionalProperties": False,
    }


def thread(inj, team):
    return list(Response.objects.filter(inject=inj, team=team, fetched=True).exclude(status="ignored")
                .select_related("person", "team", "message", "mail").order_by("received_at"))


def grade_hash(inj, crit, reps):
    return _hash(PROMPT_VERSION, model_id("deep"), inj.subject, inj.body,
                 [(c.id, c.label, c.description, c.max_points) for c in crit],
                 [(r.id, new_text(r), r.received_at.isoformat()) for r in reps])


def grade_thread(inj, team):
    """Suggest rubric points for one inject x team; stores and returns the AiGrade."""
    g, _ = AiGrade.objects.get_or_create(inject=inj, team=team, defaults={"status": "queued"})
    g.attempts += 1
    crit, reps = list(inj.rubric.all()), thread(inj, team)
    if not crit:
        g.status, g.error = "failed", "This inject has no rubric."
        g.save()
        return g
    content = [_cached(inject_block(inj, team) + "\n\n" + rubric_block(crit)),
               {"type": "text", "text": f"{team.name}'s messages on this inject:\n\n" + (
                   "\n\n---\n\n".join(reply_block(r, f"MESSAGE {n}") for n, r in enumerate(reps, 1))
                   or "(the team sent nothing)")}]
    try:
        out, used = call("grade", f"g{inj.id}-{team.id}", "deep", GRADE_SYSTEM, content, grade_schema(), "medium", 12000)
    except AiError as e:
        g.status, g.error = "failed", str(e)
        g.save()
        return g
    by_id = {c.id: c for c in crit}
    scores, notes = {}, {}
    for item in out.get("criteria") or []:
        c = by_id.get(item.get("criterion_id"))
        if not c:
            continue
        scores[str(c.id)] = _clamp(item.get("points"), 0, c.max_points)
        notes[str(c.id)] = {"rationale": (item.get("rationale") or "")[:1000], "evidence": (item.get("evidence") or "")[:400]}
    g.status, g.error, g.model, g.prompt_version = "done", None, used, PROMPT_VERSION
    g.input_hash = grade_hash(inj, crit, reps)
    g.scores, g.notes = scores, notes
    g.overall = (out.get("overall") or "")[:2000]
    g.strengths = [str(x)[:300] for x in (out.get("strengths") or [])][:5]
    g.gaps = [str(x)[:300] for x in (out.get("gaps") or [])][:5]
    g.save()
    return g


# --- the worker --------------------------------------------------------------------------------

def _kinds():
    return ["live", "test"] if conf.flag("ai_tests") else ["live"]


def _retryable(qs, now):
    return qs.filter(status="failed", attempts__lt=MAX_ATTEMPTS, updated_at__lt=now - RETRY_AFTER)


def pending_replies(now=None):
    now = now or timezone.now()
    base = Response.objects.filter(fetched=True).exclude(status="ignored").select_related("inject", "team", "person", "message", "mail")
    queued = list(base.filter(analysis__status="queued").order_by("received_at"))
    if not conf.flag("ai_auto"):
        return queued
    ids = set(_retryable(ReplyAnalysis.objects.all(), now).values_list("response_id", flat=True))
    rest = base.filter(kind__in=_kinds()).filter(analysis__isnull=True) | base.filter(kind__in=_kinds(), id__in=ids)
    return queued + [r for r in rest.order_by("received_at") if r not in queued]


def pending_grades(now=None):
    """(inject, team) pairs to grade: queued ones, then (auto) threads that are new or changed
    and have been quiet for SETTLE."""
    now = now or timezone.now()
    queued = [(g.inject, g.team) for g in AiGrade.objects.filter(status="queued").select_related("inject", "team")]
    if not conf.flag("ai_auto"):
        return queued
    with_rubric = set(RubricCriterion.objects.values_list("inject_id", flat=True))
    rows = (Response.objects.filter(fetched=True, kind__in=_kinds(), inject_id__in=with_rubric, team__isnull=False)
            .exclude(status="ignored").values("inject_id", "team_id").annotate(last=Max("received_at")))
    existing = {(g.inject_id, g.team_id): g for g in AiGrade.objects.all()}
    out = list(queued)
    for row in rows:
        if row["last"] > now - SETTLE:
            continue
        g = existing.get((row["inject_id"], row["team_id"]))
        if g and g.status == "queued":
            continue
        if g and g.status == "failed" and (g.attempts >= MAX_ATTEMPTS or g.updated_at > now - RETRY_AFTER):
            continue
        if g and g.status == "done" and g.updated_at >= row["last"]:
            continue  # nothing new from the team (rubric edits and re-matches: the grade page offers Re-run)
        inj, team = Inject.objects.get(pk=row["inject_id"]), Team.objects.get(pk=row["team_id"])
        if g and g.status == "done":
            if g.input_hash == grade_hash(inj, list(inj.rubric.all()), thread(inj, team)):
                continue
        out.append((inj, team))
    return out


def tick(limit=BATCH):
    """One pass: a few replies, then a few threads. Returns the number of model calls made."""
    if not enabled():
        return 0
    n = 0
    for r in pending_replies()[:limit]:
        if not under_cap():
            return n
        analyse_reply(r)
        n += 1
    if n >= limit:
        return n
    for inj, team in pending_grades()[:limit - n]:
        if not under_cap():
            return n
        grade_thread(inj, team)
        n += 1
    return n


def queue_reply(r):
    a, _ = ReplyAnalysis.objects.get_or_create(response=r, defaults={"status": "queued"})
    a.status, a.attempts, a.error = "queued", 0, None
    a.save(update_fields=["status", "attempts", "error", "updated_at"])


def queue_grade(inj, team):
    g, _ = AiGrade.objects.get_or_create(inject=inj, team=team, defaults={"status": "queued"})
    g.status, g.attempts, g.error = "queued", 0, None
    g.save(update_fields=["status", "attempts", "error", "updated_at"])


# --- exercise summary --------------------------------------------------------------------------

SUMMARY_SYSTEM = EXERCISE + """

You write a short situation summary of the exercise so far for ExCon, from per-message notes (made by an \
assistant that read each reply), suggested rubric results and the human grades. Be concrete: name teams \
and injects. It is for ExCon's own steering and for the hotwash (the debrief), not for participants.

- headline: one sentence on how the teams are doing overall.
- themes: three to six patterns across teams (what they prioritise, recurring gaps or strengths).
- teams: for every team, its strengths and concerns (one or two short points each).
- watch: things ExCon may want to act on now (a team falling behind, unanswered requests to personas, \
misunderstandings to correct through play).
- hotwash: three to five questions or points for the debrief."""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "themes": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "detail": {"type": "string"}},
            "required": ["title", "detail"], "additionalProperties": False}},
        "teams": {"type": "array", "items": {"type": "object", "properties": {
            "team": {"type": "string"}, "strengths": {"type": "string"}, "concerns": {"type": "string"}},
            "required": ["team", "strengths", "concerns"], "additionalProperties": False}},
        "watch": {"type": "array", "items": {"type": "string"}},
        "hotwash": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "themes", "teams", "watch", "hotwash"],
    "additionalProperties": False,
}


def summary_input(kind):
    from .models import Grade
    lines = []
    reps = (Response.objects.filter(kind=kind, fetched=True).exclude(status="ignored")
            .select_related("inject", "team", "analysis").order_by("received_at"))
    by_inj = {}
    for r in reps:
        by_inj.setdefault(r.inject, []).append(r)
    grades = {(g.inject_id, g.team_id): g for g in Grade.objects.all()}
    ai_grades = {(g.inject_id, g.team_id): g for g in AiGrade.objects.filter(status="done")}
    maxes = dict(RubricCriterion.objects.values_list("inject_id").annotate(s=Sum("max_points")).values_list("inject_id", "s"))
    for inj, rs in sorted(by_inj.items(), key=lambda kv: (kv[0] is None, kv[0].seq if kv[0] else 0)):
        lines.append(f"\n## {inj.mel_id}: {inj.subject}" if inj else "\n## Messages not matched to an inject")
        for r in rs:
            a = getattr(r, "analysis", None)
            note = a.summary if a and a.status == "done" else new_text(r)[:400]
            extra = f" [tone {a.tone}; flags {', '.join(a.flags)}]" if a and a.status == "done" and a.flags else (
                f" [tone {a.tone}]" if a and a.status == "done" else "")
            lines.append(f"- {r.team.name if r.team else '?'} {timezone.localtime(r.received_at):%a %H:%M}: {note}{extra}")
        if inj:
            for team in {r.team for r in rs if r.team}:
                h, ag = grades.get((inj.id, team.id)), ai_grades.get((inj.id, team.id))
                mx = maxes.get(inj.id)
                if h and mx:
                    lines.append(f"  {team.name} human grade {h.total():g}/{mx}" + (f": {h.comment}" if h.comment else ""))
                if ag and mx:
                    lines.append(f"  {team.name} suggested {ag.total():g}/{mx}; gaps: {'; '.join(ag.gaps)}")
    return "\n".join(lines).strip()


def run_summary(report_id):
    rep = AiReport.objects.get(pk=report_id)
    text = summary_input(rep.kind)
    if not text:
        rep.status, rep.error = "failed", "No replies yet."
        rep.save()
        return rep
    teams = ", ".join(Team.objects.filter(active=True, is_test=(rep.kind == "test")).values_list("name", flat=True))
    try:
        out, used = call("summary", f"s{rep.id}", "deep", SUMMARY_SYSTEM,
                         [{"type": "text", "text": f"Teams: {teams}\n\nNotes so far, by inject:\n{text}"}],
                         SUMMARY_SCHEMA, "medium", 16000)
        rep.status, rep.body, rep.model = "done", out, used
    except AiError as e:
        rep.status, rep.error = "failed", str(e)
    except Exception as e:  # a background thread: never lose the reason
        log.exception("summary failed")
        rep.status, rep.error = "failed", f"{type(e).__name__}: {e}"
    rep.save()
    return rep


RUN_INLINE = False  # tests: no background thread


def start_summary(kind, user=""):
    rep = AiReport.objects.create(kind=kind, user=str(user or ""))
    if RUN_INLINE:
        return run_summary(rep.id)
    threading.Thread(target=_summary_thread, args=(rep.id,), daemon=True).start()
    return rep


def _summary_thread(report_id):
    from django.db import connection
    try:
        run_summary(report_id)
    finally:
        connection.close()

"""ExCon views for the Claude analysis (ops/ai.py): the Dashboard tab, the analyse / re-run
buttons, Draft with AI, and Settings → AI analysis. Every view is excon_required."""

import re
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Max
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import ai, ai_docs, checks, conf, delivery, desk_ai  # noqa: F401 (desk_ai: its instructions)
from .access import excon_required
from .models import (AiCall, AiGrade, AiReport, DocumentAnalysis, Event, Grade, Inject, InjectDraft, InjectReview, MailAction, MailAttachment, Message,
                     Person, ReplyAnalysis, Response, Sender, Team, TeamRole, audit)
from .views_excon import _int, _u, from_local_input, reply_grid, safe_next

# Sequential blue (light -> dark) for the rubric heatmap; ink flips to white on the dark steps.
HEAT = [("#cde2fb", "#0b0b0b"), ("#9ec5f4", "#0b0b0b"), ("#6da7ec", "#0b0b0b"), ("#3987e5", "#0b0b0b"),
        ("#256abf", "#ffffff"), ("#184f95", "#ffffff")]
STEADY, URGENT, STRAINED = {"calm", "collaborative", "assertive"}, {"urgent"}, {"stressed", "confused", "defensive"}
SPARK_W, SPARK_H, PAD = 320, 72, 8
SENTIMENT = {-2: "very negative", -1: "negative", 0: "neutral", 1: "positive", 2: "very positive"}


def heat(pct):
    return HEAT[min(len(HEAT) - 1, int(pct * len(HEAT) / 101))]


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def sparkline(points, t0, t1):
    """Sentiment over exercise time for one team: SVG coordinates for a line and dots."""
    span = max((t1 - t0).total_seconds(), 60)
    w, h = SPARK_W - 2 * PAD, SPARK_H - 2 * PAD
    out = []
    for at, s, a in points:
        x = PAD + w * (at - t0).total_seconds() / span
        y = PAD + h * (2 - s) / 4
        out.append({"x": round(x, 1), "y": round(y, 1), "a": a, "at": at, "label": SENTIMENT.get(s, "")})
    return {"dots": out, "path": " ".join(f"{'M' if i == 0 else 'L'}{p['x']},{p['y']}" for i, p in enumerate(out)),
            "zero": PAD + h / 2, "top": PAD, "bottom": PAD + h, "w": SPARK_W, "h": SPARK_H}


def dashboard_data(kind, team=None):
    """The Dashboard's figures: per-team tiles and the lists of flagged, possibly AI-written
    replies, asks and commitments. With team, that team's only (its leader's Dashboard, which
    shows no grades)."""
    teams, all_in, matrix, team_sum = reply_grid(kind)
    if team:
        teams = [t for t in teams if t.id == team.id]
        team_sum = [ts for ts in team_sum if ts["team"].id == team.id]
        all_in = [r for r in all_in if r.team_id == team.id]
    reps = [r for r in all_in if r.status != "ignored"]
    analyses = {a.response_id: a for a in ReplyAnalysis.objects.filter(response__in=reps)}
    done = {rid: a for rid, a in analyses.items() if a.status == "done"}
    for r in reps:
        r.ai = done.get(r.id)

    # Exercise clock: from the first delivery to the last reply (or now).
    msg_kinds = ["live"] if kind == "live" else ["test", "oneoff"]
    first = Message.objects.filter(kind__in=msg_kinds, status="sent").order_by("sent_at").values_list("sent_at", flat=True).first()
    t0 = min([x for x in [first] + [r.received_at for r in reps[:1]] if x], default=timezone.now() - timedelta(hours=1))
    t1 = max([r.received_at for r in reps] + [t0 + timedelta(minutes=30)])

    # Per-inject delivery / read state, for the funnel.
    delivered, read = {}, {}
    leader_copies = Message.objects.filter(person__role="tl")  # a team leader reading isn't the team reading
    for m in (Message.objects.filter(kind__in=msg_kinds, status="sent").exclude(inject=None).exclude(pk__in=leader_copies)
              .values("inject_id", "team_id", "inbox_team_id", "opened_at")):
        for tid in {m["team_id"], m["inbox_team_id"]} - {None} or {None}:
            delivered.setdefault(tid, set()).add(m["inject_id"])
            if m["opened_at"]:
                read.setdefault(tid, set()).add(m["inject_id"])
    graded = Counter(t for (_, t) in Grade.objects.values_list("inject_id", "team_id"))

    tiles = []
    for ts in team_sum:
        t = ts["team"]
        mine = [r for r in reps if r.team_id == t.id]
        an = [r.ai for r in mine if r.ai]
        tones = Counter(a.tone for a in an if a.tone)
        quality = [(a.clarity + a.completeness + a.actionability) / 3 for a in an
                   if None not in (a.clarity, a.completeness, a.actionability)]
        sentiment = [(r.received_at, r.ai.sentiment, r.ai) for r in mine if r.ai and r.ai.sentiment is not None]
        n_deliv = len(delivered.get(t.id, set()) | delivered.get(None, set()))
        n_read = len(read.get(t.id, set()) | read.get(None, set()))
        n_replied = len({r.inject_id for r in mine if r.inject_id})
        funnel = [("Delivered", n_deliv), ("Read", n_read), ("Answered", n_replied)] + (
            [] if team else [("Graded", graded.get(t.id, 0))])
        top = max(n_deliv, 1)
        tiles.append({
            **ts, "n_analysed": len(an), "quality": round(sum(quality) / len(quality), 1) if quality else None,
            "tones": tones.most_common(3),
            "strain": sum(tones[x] for x in STRAINED), "urgent": sum(tones[x] for x in URGENT),
            "steady": sum(tones[x] for x in STEADY),
            "sentiment_now": SENTIMENT.get(sentiment[-1][1]) if sentiment else None,
            "flags": sum(len(a.flags) for a in an),
            "spark": sparkline(sentiment, t0, t1) if sentiment else None,
            "funnel": [(label, n, round(100 * n / top)) for label, n in funnel],
        })

    flagged = sorted([r for r in reps if r.ai and r.ai.flags], key=lambda r: r.received_at, reverse=True)[:40]
    for r in flagged:
        r.flag_labels = [ai.FLAGS[f] for f in r.ai.flags if f in ai.FLAGS]
    ai_written = sorted([r for r in reps if r.ai and (r.ai.ai_written or 0) >= 60],
                        key=lambda r: -(r.ai.ai_written or 0))[:30]
    commitments = [(r, c) for r in reversed(reps) if r.ai for c in r.ai.commitments][:30]
    asks = [(r, c) for r in reversed(reps) if r.ai for c in r.ai.asks][:30]
    documents = list(ai_docs.readings([t.id for t in teams], kind))
    for d in documents:
        d.band = ai_docs.band(d.ai_written)
    return {"teams": teams, "tiles": tiles, "matrix": matrix, "reps": reps, "analyses": analyses, "flagged": flagged,
            "ai_written": ai_written, "documents": documents,
            "documents_ai": sorted([d for d in documents if (d.ai_written or 0) >= 60], key=lambda d: -(d.ai_written or 0))[:30], "commitments": commitments, "asks": asks, "t0": t0, "t1": t1,
            "by_role": by_team_role(teams, reps, msg_kinds)}


NO_ROLE = "No team role"


def by_team_role(teams, reps, msg_kinds):
    """The teams' members grouped by team role (People & logins → Team roles): what each group
    read, opened, wrote and did in its inbox, in total and per team. [] while there are no
    team roles. reps: the teams' replies, with r.ai set (dashboard_data)."""
    roles = list(TeamRole.objects.all())
    if not roles:
        return []
    people = list(Person.objects.participants().filter(team__in=teams).select_related("team_role"))
    ids = [p.id for p in people]
    reads = Counter(pid for pid, _ in Event.objects.filter(person_id__in=ids, type="read", message__kind__in=msg_kinds)
                    .values_list("person_id", "message_id").distinct())
    downloads = Counter(Event.objects.filter(person_id__in=ids, type="download", message__kind__in=msg_kinds)
                        .values_list("person_id", flat=True))
    actions = dict(MailAction.objects.filter(person_id__in=ids).values_list("person_id")
                   .annotate(n=Count("id")).values_list("person_id", "n"))
    mine = {}
    for r in reps:
        if r.person_id:
            mine.setdefault(r.person_id, []).append(r)
    total = sum(len(xs) for xs in mine.values())
    rows = []
    for name in [r.name for r in roles] + [NO_ROLE]:
        group = [p for p in people if (p.team_role.name if p.team_role else NO_ROLE) == name]
        written = [r for p in group for r in mine.get(p.id, [])]
        active = [p for p in group if mine.get(p.id) or reads[p.id] or downloads[p.id] or (p.active and p.user_id)]
        if not active:
            continue
        n = len(active)
        an = [r.ai for r in written if r.ai]
        quality = [(a.clarity + a.completeness + a.actionability) / 3 for a in an
                   if None not in (a.clarity, a.completeness, a.actionability)]
        minutes = [m for m in (ai.timing(r)[0] for r in written) if m is not None]
        per_team = {t.id: (sum(1 for p in active if p.team_id == t.id),
                           sum(1 for r in written if r.team_id == t.id)) for t in teams}
        rows.append({
            "role": name, "people": n, "written": len(written), "per_person": round(len(written) / n, 1),
            "share": round(100 * len(written) / total) if total else None,
            "injects": len({r.inject_id for r in written if r.inject_id}),
            "median_minutes": _median(minutes),
            "read_per_person": round(sum(reads[p.id] for p in active) / n, 1),
            "downloads": sum(downloads[p.id] for p in active),
            "actions": sum(actions.get(p.id, 0) for p in active),
            "quality": round(sum(quality) / len(quality), 1) if quality else None,
            "ai_likely": sum(1 for a in an if (a.ai_written or 0) >= 70),
            "teams": [{"team": t, "people": per_team[t.id][0], "written": per_team[t.id][1]} for t in teams],
        })
    return rows


@excon_required
def dashboard(request):
    ai.expire_drafts()  # also a summary interrupted by a restart
    kind = "test" if request.GET.get("kind") == "test" else "live"
    source = "ai" if request.GET.get("score") == "ai" else "human"
    data = dashboard_data(kind)
    matrix, reps, analyses = data["matrix"], data["reps"], data["analyses"]

    # Rubric heatmap (human or AI points) and where the two disagree.
    heat_rows, disagree = [], []
    for inj, cells in matrix:
        if not any(c["max"] for c in cells):
            continue
        row = []
        for c in cells:
            v = c["score"] if source == "human" else c["ai"]
            if c["max"] and v is not None:
                pct = round(100 * v / c["max"])
                bg, ink = heat(pct)
                row.append({"team": c["team"], "v": v, "max": c["max"], "pct": pct, "bg": bg, "ink": ink})
            else:
                row.append({"team": c["team"], "v": None, "n": c["n"]})
            if c["max"] and c["score"] is not None and c["ai"] is not None:
                gap = (c["ai"] - c["score"]) / c["max"]
                if abs(gap) >= 0.25:
                    disagree.append({"inj": inj, "team": c["team"], "human": c["score"], "ai": c["ai"], "max": c["max"],
                                     "gap": round(100 * gap)})
        heat_rows.append((inj, row))
    disagree.sort(key=lambda d: -abs(d["gap"]))

    report = AiReport.objects.filter(kind=kind).order_by("-id").first()
    return render(request, "ops/dashboard.html", {
        **data, "tab": "dashboard", "kind": kind, "source": source,
        "heat_rows": heat_rows, "heat_legend": [(f"{round(i * 100 / len(HEAT))}–{round((i + 1) * 100 / len(HEAT))}%", bg, ink)
                                                for i, (bg, ink) in enumerate(HEAT)],
        "disagree": disagree[:20], "report": report, **ai_status(reps, analyses)})


def ai_status(reps=None, analyses=None):
    return {"ai_available": ai.available(), "ai_enabled": ai.enabled(), "ai_provider": ai.provider(),
            "ai_spent": ai.spent_today(), "ai_cap": ai.cap(),
            "ai_pending": ReplyAnalysis.objects.filter(status="queued").count() + AiGrade.objects.filter(status="queued").count()
            + DocumentAnalysis.objects.filter(status="queued").count(),
            "ai_unanalysed": (sum(1 for r in reps if r.id not in analyses) if reps is not None else None)}


@excon_required
@require_POST
def dashboard_summary(request):
    ai.expire_drafts()
    kind = "test" if request.POST.get("kind") == "test" else "live"
    if not ai.enabled():
        messages.error(request, "AI analysis is off (Settings → AI analysis).")
    elif not ai.under_cap():
        messages.error(request, "Today's AI spending cap is reached (Settings → AI analysis).")
    else:
        ai.start_summary(kind, _u(request))
        audit("ai_summary", kind, _u(request))
        messages.success(request, "Writing the summary: this takes a minute. The page refreshes on its own.")
    return redirect(f"/dashboard/?kind={kind}#summary")


@excon_required
def documents(request):
    """Every file the teams sent, with Claude's reading of it (ops/ai_docs.py)."""
    kind = "test" if request.GET.get("kind") == "test" else "live"
    rows = list(DocumentAnalysis.objects.filter(kind=kind).select_related("team", "person", "inject", "response"))
    for d in rows:
        d.band = ai_docs.band(d.ai_written) if d.status == "done" else ""
    team = _int(request.GET.get("team"))
    if team:
        rows = [d for d in rows if d.team_id == team]
    return render(request, "ops/documents.html", {
        "tab": "dashboard", "kind": kind, "rows": rows, "teams": Team.objects.filter(active=True), "team": team,
        "on": ai_docs.enabled(), "auto": conf.flag("ai_auto"), **ai_status()})


@excon_required
def document_page(request, did):
    d = get_object_or_404(DocumentAnalysis.objects.select_related("team", "person", "inject", "response"), pk=did)
    f = d.file()
    src = "m" if isinstance(f, MailAttachment) else "r" if f else ""
    d.band = ai_docs.band(d.ai_written) if d.status == "done" else ""
    others = (DocumentAnalysis.objects.filter(team=d.team, kind=d.kind, status="done").exclude(pk=d.pk)
              .order_by("-sent_at")[:10])
    return render(request, "ops/document.html", {
        "tab": "dashboard", "d": d, "url": f"/files/{src}/{f.id}/" if f else "", "others": others,
        "on": ai_docs.enabled()})


@excon_required
@require_POST
def document_analyse(request, did):
    d = get_object_or_404(DocumentAnalysis, pk=did)
    ai_docs.queue(d)
    audit("ai_document", f"{d.team.name}: {d.filename}", _u(request))
    messages.success(request, "Queued: the reading appears here within a minute." if ai_docs.enabled() else
                     "Queued, but reading documents is off: turn it on in Settings → AI analysis.")
    return redirect(f"/documents/{d.id}/")


@excon_required
@require_POST
def document_queue(request, tid, sha):
    """Analyse a file of a team's from the reply page (automatic reading off, or not read yet)."""
    d = ai_docs.queue_file(tid, sha)
    if not d:
        messages.error(request, "That file isn't one the team sent (or it is no longer stored).")
        return redirect(safe_next(request.POST.get("next"), "/documents/"))
    audit("ai_document", f"{d.team.name}: {d.filename}", _u(request))
    messages.success(request, "Queued: the reading appears within a minute." if ai_docs.enabled() else
                     "Queued, but reading documents is off: turn it on in Settings → AI analysis.")
    return redirect(safe_next(request.POST.get("next"), f"/documents/{d.id}/"))


@excon_required
@require_POST
def reply_analyse(request, rid):
    r = get_object_or_404(Response, pk=rid)
    ai.queue_reply(r)
    messages.success(request, "Queued: the analysis appears here within a minute." if ai.enabled() else
                     "Queued, but AI analysis is off: turn it on in Settings.")
    return redirect(f"/replies/{rid}/")


@excon_required
@require_POST
def inject_ai_check(request, iid):
    inj = get_object_or_404(Inject, pk=iid)
    action = request.POST.get("action", "check")
    if action in ("dismiss", "undismiss"):
        InjectReview.objects.filter(inject=inj).update(dismissed=action == "dismiss")
        audit(f"ai_review_{action}", inj.mel_id, _u(request))
    else:
        ai.queue_review(inj)
        messages.success(request, "Queued: the AI review appears here within a minute or two." if ai.enabled() else
                         "Queued, but AI analysis is off: turn it on in Settings.")
    return redirect(f"/injects/{iid}/")


@excon_required
@require_POST
def injects_ai_check(request):
    """Queue unsent injects for the AI review: every one (e.g. after changing many by hand), only
    those without a review of them as they are now, or the ones selected on the list."""
    scope = request.POST.get("scope", "all")
    back_to = safe_next(request.POST.get("back"), "/injects/")
    rows = [i for i in Inject.objects.all() if checks.unsent(i)]
    if scope == "selected":
        ids = {int(x) for x in request.POST.getlist("ids") if x.isdigit()}
        if not ids:
            messages.warning(request, "Select injects first.")
            return redirect(back_to)
        rows = [i for i in rows if i.id in ids]
    elif scope == "unchecked":
        files, senders = checks.files_by_inject(), {s.local_part: s for s in Sender.objects.all()}
        reviews, prompt = {rv.inject_id: rv for rv in InjectReview.objects.all()}, ai.system("inject")
        rows = [i for i in rows if not ai.current_review(i, files.get(i.id, []), senders, reviews, prompt)
                and not (reviews.get(i.id) and reviews[i.id].status == "queued")]
    else:
        scope = "all"
    for inj in rows:
        ai.queue_review(inj)
    n = len(rows)
    audit(f"ai_review_{scope}", f"{n} injects", _u(request))
    if not ai.enabled():
        messages.success(request, "Queued, but AI analysis is off: turn it on in Settings.")
    elif not n:
        messages.info(request, "Nothing to queue: no unsent inject " + (
            "is selected." if scope == "selected" else "lacks a current AI review." if scope == "unchecked" else "left."))
    else:
        messages.success(request, f"{n} unsent inject{'s' if n != 1 else ''} queued for the AI review; "
                                  "flags appear on this list as they are done.")
    return redirect(back_to)


@excon_required
@require_POST
def grade_analyse(request, iid, tid):
    inj, team = get_object_or_404(Inject, pk=iid), get_object_or_404(Team, pk=tid)
    ai.queue_grade(inj, team)
    messages.success(request, "Queued: the suggestion appears here within a minute." if ai.enabled() else
                     "Queued, but AI analysis is off: turn it on in Settings.")
    return redirect(f"/grades/{iid}/{tid}/")


@excon_required
@require_POST
def settings_ai(request):
    P = request.POST
    for k in ("ai_enabled", "ai_auto", "ai_tests", "ai_injects", "ai_documents", "ai_documents_deep", "desk_triage"):
        conf.put(k, "1" if P.get(k) == "1" else "0")
    try:
        conf.put("ai_daily_usd", f"{max(0.0, float(P.get('ai_daily_usd') or 0)):g}")
    except ValueError:
        messages.error(request, "Daily cap: not a number; not changed.")
    audit("settings_ai", f"enabled={conf.get('ai_enabled')} auto={conf.get('ai_auto')} cap={conf.get('ai_daily_usd')}", _u(request))
    messages.success(request, "AI analysis settings saved.")
    return redirect("/settings/#ai")


PROMPT_MAX = 30_000  # characters per set of instructions


def ai_prompts():
    from . import npc, report  # noqa: F401  register their instructions
    return [{"key": k, **v, "value": ai.instructions(k), "custom": bool((conf.get(f"ai_prompt_{k}") or "").strip())}
            for k, v in ai.INSTRUCTIONS.items()]


@excon_required
@require_POST
def settings_ai_prompts(request):
    """Settings → AI instructions: save the edited ones, or put one back to the default. Text
    equal to the default is stored as empty, so later changes to the default still apply."""
    from . import npc, report  # noqa: F401
    P, changed = request.POST, []
    reset = P.get("reset")
    if reset in ai.INSTRUCTIONS:
        conf.put(f"ai_prompt_{reset}", "")
        changed = [reset]
        messages.success(request, f"{ai.INSTRUCTIONS[reset]['label']}: back to the default instructions.")
    else:
        for k, v in ai.INSTRUCTIONS.items():
            if f"prompt_{k}" not in P:
                continue
            text = P.get(f"prompt_{k}", "").replace("\r\n", "\n").strip()
            if len(text) > PROMPT_MAX:
                messages.error(request, f"{v['label']}: longer than {PROMPT_MAX:,} characters; not saved.")
                continue
            value = "" if not text or text == v["default"].strip() else text
            if value != (conf.get(f"ai_prompt_{k}") or "").strip():
                conf.put(f"ai_prompt_{k}", value)
                changed.append(k)
        messages.success(request, "AI instructions saved." if changed else "No changes to the AI instructions.")
    if changed:
        audit("settings_ai_prompts", ", ".join(changed), _u(request))
    return redirect("/settings/#ai-prompts")


def ai_settings_context():
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    calls = AiCall.objects.filter(at__gte=start)
    return {**ai_status(), "ai_calls_today": calls.count(), "ai_failed_today": calls.filter(ok=False).count(),
            "ai_model_fast": ai.model_id("fast"), "ai_model_deep": ai.model_id("deep"),
            "ai_region": settings.AI_REGION, "ai_last_error": calls.filter(ok=False).order_by("-id").first(),
            "ai_prompts": ai_prompts()}


# --- Draft with AI -----------------------------------------------------------------------------

@excon_required
def drafts_page(request):
    """The request form (prefilled from ?inject=&team=&mode=&response=) and recent drafts. POST
    starts a draft in a thread and goes to its page."""
    src = request.POST if request.method == "POST" else request.GET
    inj = Inject.objects.filter(pk=_int(src.get("inject"))).first()
    team = Team.objects.filter(pk=_int(src.get("team")), is_test=False).first()
    resp = Response.objects.filter(pk=_int(src.get("response"))).select_related("team", "inject").first()
    prev = InjectDraft.objects.filter(pk=_int(src.get("previous"))).first()
    mode = src.get("mode") if src.get("mode") in ai.DRAFT_MODES else "new"
    if mode == "answer" and resp:
        inj, team = resp.inject, resp.team
    elif mode == "answer" or (mode == "followup" and not inj):
        mode = "new"
    ai.expire_drafts()
    if request.method == "POST":
        prompt = (src.get("prompt") or "").strip()[:4000]
        if not ai.enabled():
            messages.error(request, "AI analysis is off (Settings → AI analysis).")
        elif not ai.under_cap():
            messages.error(request, "Today's AI spending cap is reached (Settings → AI analysis).")
        elif not prompt and mode == "new" and not inj and not team:
            messages.error(request, "Say what the inject should be about, or start from an inject or a team.")
        else:
            d = ai.start_draft(mode=mode, prompt=prompt, inject=inj, team=team, response=resp if mode == "answer" else None,
                               previous=prev, user=_u(request))
            audit("ai_draft", f"#{d.id} {mode} {inj.mel_id if inj else ''}".strip(), _u(request))
            return redirect(f"/injects/drafts/{d.id}/")
        return redirect(request.get_full_path())
    return render(request, "ops/drafts.html", {
        "tab": "injects", "inj": inj, "team": team, "resp": resp if mode == "answer" else None, "mode": mode,
        "MODES": ai.DRAFT_MODES, "injects_all": Inject.objects.exclude(status="cancelled").order_by("send_at", "seq"),
        "teams": Team.objects.filter(active=True, is_test=False),
        "can_followup": bool(inj and delivery.followup_targets(inj)),
        "recent": InjectDraft.objects.select_related("inject", "team", "created").order_by("-id")[:30], **ai_status()})


@excon_required
def draft_page(request, did):
    ai.expire_drafts()
    d = get_object_or_404(InjectDraft.objects.select_related("inject", "team", "response", "created"), pk=did)
    known = {s.local_part: s for s in Sender.objects.all()}
    b = d.body or {}
    return render(request, "ops/draft.html", {
        "tab": "injects", "d": d, "b": b, "sender": known.get(b.get("sender")),
        "revisions": InjectDraft.objects.filter(previous=d).order_by("id"), "ai_on": ai.enabled()})


def _send_at(value):
    try:
        return from_local_input((value or "").strip().replace(" ", "T")[:16]) if value else None
    except ValueError:
        return None


@excon_required
@require_POST
def draft_use(request, did):
    """Make the draft a draft inject, or take it to the follow-up or answer form. Never sends."""
    d = get_object_or_404(InjectDraft, pk=did)
    b = d.body or {}
    if d.status != "done":
        messages.error(request, "This draft isn't ready.")
        return redirect(f"/injects/drafts/{did}/")
    if d.mode == "followup" and d.inject_id:
        return redirect(f"/injects/{d.inject_id}/?draft={d.id}#followup")
    if d.mode == "answer" and d.response_id:
        return redirect(f"/replies/{d.response_id}/?draft={d.id}#answer")
    if d.created_id:
        messages.info(request, f"Already made into {d.created.mel_id}.")
        return redirect(f"/injects/{d.created_id}/")
    n = Inject.objects.filter(mel_id__startswith="AI-").count() + 1
    while Inject.objects.filter(mel_id=f"AI-{n}").exists():
        n += 1
    base, known = d.inject, set(Sender.objects.values_list("local_part", flat=True))
    sender = re.sub(r"[^a-z0-9._{}+-]", "", str(b.get("sender") or "").lower())[:120] or (base.sender_local if base else None)
    inj = Inject.objects.create(
        mel_id=f"AI-{n}", seq=(Inject.objects.aggregate(m=Max("seq"))["m"] or 0) + 1, status="draft",
        sender_local=sender or None, from_name=None if sender in known else (str(b.get("from_name") or "")[:200] or None),
        subject=b.get("subject") or None, body=(b.get("body") or "").strip() or None,
        scenario_dt=(b.get("scenario_time") or "").strip()[:20] or None, send_at=_send_at(b.get("send_at")),
        channel=base.channel if base else "inapp", delivery=base.delivery if base else "each_team",
        team_ids=[d.team_id] if d.team_id and b.get("recipients") == "team" else (base.team_ids if base else None),
        supporting_documents="\n".join(x for x in b.get("documents") or [] if x.strip()) or None,
        expects_reply=bool(b.get("expects_reply")), reply_due_minutes=b.get("reply_due_minutes") or None)
    d.created = inj
    d.save(update_fields=["created"])
    if ai.enabled():
        ai.queue_review(inj)
    audit("ai_draft_inject", f"#{d.id} -> {inj.mel_id}", _u(request))
    messages.success(request, f"Draft inject {inj.mel_id} created: check it, attach any documents, then schedule or send it.")
    return redirect(f"/injects/{inj.id}/")

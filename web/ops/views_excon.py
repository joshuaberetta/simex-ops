"""ExCon views: injects, import, senders, people & accounts, test runs, replies & grading,
log, settings. Every view here is excon_required (tests enforce it over the URLconf)."""

import csv
import io
import json
import re
import secrets
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import BooleanField, Count, ExpressionWrapper, Max, Prefetch, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_POST

from . import (ai, legacy_import, backup, checks, conf, delivery, documents, importer, inbound, mailer, notices,
               render as rnd, signin)
from .access import excon_required
from .models import (CHANNELS, DELIVERY_MODES, ROLES, STATUSES, AiGrade, Attachment, AttachmentImport, Audit, Grade,
                     ImportBatch, Inject, InjectDraft, InjectReview, Mail, MailAttachment, MailLabel, Message,
                     MessageAttachment, Notice, Person, PushSubscription, ReplyAnalysis, Response,
                     ResponseAttachment, RubricCriterion, Sender, Team, TeamRole, TestRun, TestRunItem, audit)

MAX_ATTACHMENT = documents.MAX_ATTACHMENT
User = get_user_model()


def local_input(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%dT%H:%M") if dt else ""


def from_local_input(value):
    if not value:
        return None
    return timezone.make_aware(datetime.fromisoformat(value))


def local_day(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%d") if dt else ""


def safe_next(value, default):
    value = str(value or "")
    return value if value.startswith("/") and not value.startswith("//") else default


def _u(request):
    return request.user.get_username()


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _num(v):
    """A posted number (1.5 is fine), or None."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --- injects -----------------------------------------------------------------------------------

@excon_required
def injects(request):
    day, status, q = request.GET.get("day", ""), request.GET.get("status", ""), request.GET.get("q", "")
    flagged = request.GET.get("flag", "")
    rows = list(Inject.objects.all().order_by("send_at", "seq"))
    rows.sort(key=lambda i: (i.send_at is None, i.send_at or timezone.now(), i.seq))
    days = sorted({local_day(i.send_at) for i in rows if i.send_at})
    if day:
        rows = [i for i in rows if local_day(i.send_at) == day or (day == "none" and not i.send_at)]
    if status:
        rows = [i for i in rows if i.status == status]
    if q:
        ql = q.lower()
        rows = [i for i in rows if ql in " ".join(filter(None, [i.mel_id, i.subject, i.body, i.sender_local])).lower()]
    senders = {x.local_part: x for x in Sender.objects.all()}
    files = checks.files_by_inject()
    reviews, prompt = {rv.inject_id: rv for rv in InjectReview.objects.all()}, ai.system("inject")
    ai_stats = {"unsent": 0, "checked": 0, "queued": 0}
    for i in rows:
        fs = files.get(i.id, [])
        i.flags = checks.inject_flags(i, fs, senders)
        if not checks.unsent(i):
            continue
        ai_stats["unsent"] += 1
        rv = ai.current_review(i, fs, senders, reviews, prompt)
        if rv:
            ai_stats["checked"] += 1
            if not rv.dismissed:
                i.flags += [("ai", f"AI: {ai.ISSUE_KINDS.get(x['kind'], 'check')}", x["detail"]) for x in rv.issues]
        elif reviews.get(i.id) and reviews[i.id].status == "queued":
            ai_stats["queued"] += 1
    flag_total = sum(1 for i in rows if i.flags)
    if flagged:
        rows = [i for i in rows if i.flags]
    groups = []
    for i in rows:
        snd = senders.get(i.sender_local)
        i.from_display = i.from_name or (snd.display_name if snd else "")
        i.att_count = len(files.get(i.id, [])) or ""
        i.delivery_label = DELIVERY_MODES.get(i.delivery, i.delivery).split("(")[0]
        key = timezone.localtime(i.send_at).strftime("%A %d %B %Y") if i.send_at else "No send time"
        if not groups or groups[-1]["label"] != key:
            groups.append({"label": key, "key": local_day(i.send_at) or "none", "items": []})
        groups[-1]["items"].append(i)
    for g in groups:
        items = g["items"]
        g["statuses"] = [(st, n) for st in STATUSES if (n := sum(1 for i in items if i.status == st))]
        g["flagged"] = sum(1 for i in items if i.flags)
        g["files"] = sum(len(files.get(i.id, [])) for i in items)
        g["replies"] = sum(1 for i in items if i.expects_reply)
        times = [i.send_at for i in items if i.send_at]
        g["first"], g["last"] = (min(times), max(times)) if times else (None, None)
    return render(request, "ops/injects.html", {"tab": "injects", "groups": groups, "days": days, "day": day,
                                                "status": status, "q": q, "flag": flagged, "flag_total": flag_total,
                                                "ai_on": ai.enabled(), "ai_stats": ai_stats,
                                                "total": len(rows), "STATUSES": STATUSES})


@excon_required
@require_POST
def injects_bulk(request):
    ids = [int(x) for x in request.POST.getlist("ids")]
    action = request.POST.get("action", "")
    back_to = safe_next(request.POST.get("back"), "/injects/")
    if not ids:
        messages.warning(request, "Select injects first.")
        return redirect(back_to)
    n = 0
    for inj in Inject.objects.filter(id__in=ids):
        if action == "schedule" and inj.status in ("draft", "overdue", "cancelled", "failed") and inj.send_at:
            inj.status = "scheduled"
        elif action == "unschedule" and inj.status in ("scheduled", "overdue"):
            inj.status = "draft"
        elif action == "cancel" and inj.status in ("draft", "scheduled", "overdue"):
            inj.status = "cancelled"
        elif action == "shift" and inj.send_at and inj.status not in importer.LOCKED:
            inj.send_at += timedelta(minutes=_num(request.POST.get("minutes")) or 0)
        elif action in ("inapp", "email") and inj.status not in importer.LOCKED:
            inj.channel = action
        else:
            continue
        inj.save()
        n += 1
    audit(f"bulk_{action}", f"{n} injects", _u(request))
    messages.success(request, f"{action.capitalize()}: {n} of {len(ids)} selected injects changed.")
    if action in ("inapp", "email") and conf.get("channel_mode") != "per_inject":
        messages.warning(request, f"Settings → Delivery channel is '{conf.CHANNEL_MODES[conf.get('channel_mode')]}', "
                                  "which overrides each inject's channel. Choose Per inject there for this to apply.")
    return redirect(back_to)


@excon_required
@require_POST
def inject_new(request):
    n = Inject.objects.filter(mel_id__startswith="NEW-").count() + 1
    while Inject.objects.filter(mel_id=f"NEW-{n}").exists():
        n += 1
    inj = Inject.objects.create(mel_id=f"NEW-{n}", seq=(Inject.objects.aggregate(m=Max("seq"))["m"] or 0) + 1)
    audit("inject_new", inj.mel_id, _u(request))
    return redirect(f"/injects/{inj.id}/")


@excon_required
def inject_page(request, iid):
    inj = get_object_or_404(Inject, pk=iid)
    if request.method == "POST":
        return _inject_save(request, inj)
    drafts, warnings = rnd.build(inj, conf.get_all())
    atts = list(inj.attachments.all())
    names, senders = [a.filename for a in atts], {x.local_part: x for x in Sender.objects.all()}
    review = InjectReview.objects.filter(inject=inj).first()
    targets = delivery.followup_targets(inj)
    ai_draft = InjectDraft.objects.filter(pk=_int(request.GET.get("draft")), inject=inj, mode="followup", status="done").first()
    checked = {m.id for m in targets if ai_draft and ai_draft.team_id and ai_draft.team_id in (m.team_id, m.inbox_team_id)}
    ctx = {
        "tab": "injects", "inj": inj, "drafts": drafts, "warnings": warnings,
        "msgs": Message.objects.filter(inject=inj)[:100], "teams": Team.objects.filter(is_test=False),
        "senders": Sender.objects.filter(active=True), "sender": rnd.sender_for(inj), "atts": atts,
        "sender_inapp": rnd.sender_for(inj).inapp_address if rnd.sender_for(inj) and inj.effective_channel == "inapp" else "",
        "missing_docs": documents.missing_docs(inj, names),
        "flags": checks.inject_flags(inj, names, senders), "review": review, "ai_on": ai.enabled(),
        "review_current": bool(review and ai.current_review(inj, names, senders, {inj.id: review})),
        "ISSUE_KINDS": ai.ISSUE_KINDS,
        "rubric": inj.rubric.all(), "reply_count": inj.responses.exclude(status="ignored").count(),
        "with_rubric": Inject.objects.filter(rubric__isnull=False).exclude(pk=iid).distinct().order_by("mel_id"),
        "send_at_input": local_input(inj.send_at), "DELIVERY_MODES": DELIVERY_MODES, "CHANNELS": CHANNELS,
        "selected_teams": inj.team_ids or [t.id for t in Team.objects.filter(is_test=False)],
        "test_team_id": delivery.test_team().id, "followup_targets": targets,
        "ai_draft": ai_draft, "followup_checked": checked, "drafted_from": InjectDraft.objects.filter(created=inj).first(),
    }
    return render(request, "ops/inject.html", ctx)


def _inject_save(request, inj):
    P = request.POST
    if inj.status == "sending":
        messages.warning(request, "This inject is being sent; edit it when sending has finished.")
        return redirect(f"/injects/{inj.id}/")
    mel_id = (P.get("mel_id") or "").strip()
    if mel_id and mel_id != inj.mel_id:
        if Inject.objects.filter(mel_id=mel_id).exclude(pk=inj.pk).exists():
            messages.error(request, f"Inject id {mel_id} already exists.")
            return redirect(f"/injects/{inj.id}/")
        inj.mel_id = mel_id
    try:
        inj.send_at = from_local_input(P.get("send_at"))
    except ValueError:
        messages.error(request, "Send time not understood.")
        return redirect(f"/injects/{inj.id}/")
    local = re.sub(r"@.*$", "", (P.get("sender_local") or "").strip().lower())
    inj.sender_local = local or None
    for f in ("from_name", "reply_to", "to_extra", "cc", "bcc", "subject", "scenario_dt"):
        setattr(inj, f, (P.get(f) or "").strip() or None)
    inj.footer = P.get("footer") or None
    inj.body = (P.get("body") or "").replace("\r\n", "\n") or None
    if P.get("delivery") in DELIVERY_MODES:
        inj.delivery = P.get("delivery")
    if P.get("channel") in CHANNELS:
        inj.channel = P.get("channel")
    if P.get("footer_mode") in ("default", "custom", "none"):
        inj.footer_mode = P.get("footer_mode")
    inj.to_leaders = P.get("to_leaders") == "1"
    team_ids = [int(x) for x in P.getlist("team_ids")]
    all_ids = list(Team.objects.filter(active=True, is_test=False).values_list("id", flat=True))
    inj.team_ids = None if not team_ids or set(all_ids) <= set(team_ids) else team_ids
    if inj.status == "scheduled" and not inj.send_at:
        inj.status = "draft"
    inj.save()
    audit("inject_edit", inj.mel_id, _u(request))
    messages.success(request, "Saved.")
    return redirect(f"/injects/{inj.id}/")


@excon_required
@require_POST
def inject_status(request, iid):
    inj = get_object_or_404(Inject, pk=iid)
    action = request.POST.get("action")
    if action == "schedule":
        if not inj.send_at:
            messages.error(request, "Set a send time first.")
        elif inj.send_at < timezone.now():
            messages.error(request, "The send time has passed: change it, or use Send now.")
        elif inj.status in ("draft", "overdue", "cancelled", "failed"):
            inj.status = "scheduled"
    elif action == "unschedule" and inj.status in ("scheduled", "overdue"):
        inj.status = "draft"
    elif action == "cancel" and inj.status in ("draft", "scheduled", "overdue"):
        inj.status = "cancelled"
    elif action == "reset" and inj.status in ("sent", "partial", "failed"):
        inj.status = "draft"
    inj.save()
    audit(f"inject_{action}", inj.mel_id, _u(request))
    return redirect(f"/injects/{iid}/")


@excon_required
@require_POST
def inject_send(request, iid):
    inj = get_object_or_404(Inject, pk=iid)
    if request.POST.get("confirm") != inj.mel_id:
        messages.error(request, f"Type the inject id ({inj.mel_id}) to confirm a live send.")
        return redirect(f"/injects/{iid}/")
    err = delivery.start_live(inj, retry_failed=request.POST.get("retry") == "1", user=_u(request))
    if not err and inj.effective_channel == "inapp":
        delivery.process_live(inj.id)  # in-app is instant: deliver now rather than on the next tick
        inj.refresh_from_db()
        messages.success(request, f"Delivered: {inj.status}.")
    elif err:
        messages.error(request, err)
    else:
        messages.success(request, "Queued: the scheduler sends it within a few seconds. Refresh to see progress.")
    return redirect(f"/injects/{iid}/")


@excon_required
@require_POST
def inject_followup(request, iid):
    """Send a follow-up in the thread of a delivered inject (a correction, a missing document)."""
    inj = get_object_or_404(Inject, pk=iid)
    P = request.POST
    back = f"/injects/{iid}/#followup"
    if P.get("confirm") != inj.mel_id:
        messages.error(request, f"Type the inject id ({inj.mel_id}) to confirm the follow-up.")
        return redirect(back)
    subject, body = (P.get("subject") or "").strip(), (P.get("body") or "").strip()
    if not subject or not body:
        messages.error(request, "A follow-up needs a subject and a message.")
        return redirect(back)
    chosen = {int(x) for x in P.getlist("to")}
    parents = [m for m in delivery.followup_targets(inj) if m.id in chosen]
    att_ids = [int(x) for x in P.getlist("att")]
    for up in request.FILES.getlist("files"):
        if up.size > MAX_ATTACHMENT:
            messages.error(request, f"{up.name} is over 30 MB; nothing was sent.")
            return redirect(back)
    for up in request.FILES.getlist("files"):  # new files also become attachments of the inject
        att_ids.append(documents.put_attachment(inj, PurePosixPath(up.name).name, up.read()).id)
    draft = InjectDraft.objects.filter(pk=_int(P.get("ai_draft")), inject=inj, mode="followup", status="done").first()
    msgs, err = delivery.send_followup(inj, parents, subject, body, att_ids, user=_u(request), ai_draft=draft)
    if err:
        messages.error(request, err)
    elif any(m.channel == "email" for m in msgs):
        messages.success(request, f"Follow-up queued for {len(msgs)} recipient(s): the scheduler sends it within a few "
                                  "seconds. Refresh to see progress.")
    else:
        messages.success(request, f"Follow-up delivered to {len(msgs)} inbox(es).")
    return redirect(back)


@excon_required
@require_POST
def inject_test(request, iid):
    inj = get_object_or_404(Inject, pk=iid)
    address = (request.POST.get("address") or "").strip()
    if inj.effective_channel == "email":
        if not rnd.EMAIL.match(address):
            messages.error(request, "Enter a valid test address.")
            return redirect(f"/injects/{iid}/")
        conf.put("test_address", address)
    msgs, warnings = delivery.send_oneoff(inj, address, all_targets=request.POST.get("all") == "1", user=_u(request))
    if not msgs:
        messages.error(request, "Nothing to send: " + "; ".join(warnings))
    where = "the Test team inbox" if inj.effective_channel == "inapp" else address
    for m in msgs:
        if m.status == "sent":
            messages.success(request, f"Test delivered to {where} ({m.target}).")
        else:
            messages.error(request, f"Test to {where} failed: {m.error}")
    return redirect(f"/injects/{iid}/")


@excon_required
@require_POST
def inject_delete(request, iid):
    inj = get_object_or_404(Inject, pk=iid)
    if request.POST.get("confirm") != inj.mel_id:
        messages.error(request, "Type the inject id to confirm deletion.")
        return redirect(f"/injects/{iid}/")
    audit("inject_delete", inj.mel_id, _u(request))
    inj.delete()
    messages.success(request, "Inject deleted.")
    return redirect("/injects/")


@excon_required
@require_POST
def attachments_upload(request, iid):
    inj = get_object_or_404(Inject, pk=iid)
    n = 0
    for up in request.FILES.getlist("files"):
        if up.size > MAX_ATTACHMENT:
            messages.error(request, f"{up.name} is over 30 MB; not added.")
            continue
        documents.put_attachment(inj, PurePosixPath(up.name).name, up.read())
        n += 1
    audit("attachments_upload", f"{inj.mel_id}: {n}", _u(request))
    messages.success(request, f"{n} file(s) attached.")
    return redirect(f"/injects/{iid}/")


def _download(data, filename, content_type="application/octet-stream"):
    resp = HttpResponse(bytes(data), content_type=content_type)
    resp["Content-Disposition"] = content_disposition_header(True, re.sub(r"[\r\n]+", " ", filename or "file"))
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


@excon_required
def attachment_get(request, aid):
    a = get_object_or_404(Attachment, pk=aid)
    return _download(a.data, a.filename, a.content_type)


@excon_required
@require_POST
def attachment_delete(request, aid):
    a = get_object_or_404(Attachment, pk=aid)
    iid = a.inject_id
    audit("attachment_delete", a.filename, _u(request))
    a.delete()
    return redirect(f"/injects/{iid}/")


# --- import -----------------------------------------------------------------------------------

@excon_required
def import_page(request):
    if request.method == "POST":
        up = request.FILES.get("file")
        if not up:
            messages.error(request, "Choose a file.")
            return redirect("/import/")
        try:
            records = importer.parse_upload(up.name, up.read())
        except importer.ImportError_ as e:
            messages.error(request, str(e))
            return redirect("/import/")
        batch = ImportBatch.objects.create(filename=up.name, payload=records)
        rows, missing = importer.plan(records)
        summary = {k: sum(1 for r in rows if r["action"] == k) for k in ("new", "update", "conflict", "unchanged")}
        summary["droppable"] = sum(1 for m in missing if m["droppable"])
        domains = sorted(f"{shown or lp}@{d}" for lp, (shown, d) in importer.sender_domains(records).items())
        return render(request, "ops/import_plan.html", {"tab": "injects", "batch": batch, "rows": rows,
                                                        "missing": missing, "summary": summary, "domains": domains})
    batches = ImportBatch.objects.order_by("-id")[:10]
    zips = list(AttachmentImport.objects.all()[:5])
    return render(request, "ops/import.html", {
        "tab": "injects", "batches": batches, "zips": zips, "zip_running": any(z.active for z in zips),
        "n_injects": Inject.objects.count(), "n_attachments": Attachment.objects.count()})


@excon_required
@require_POST
def import_apply(request, bid):
    batch = ImportBatch.objects.filter(pk=bid).first()
    if not batch or batch.applied:
        messages.error(request, "That import was already applied or has expired.")
        return redirect("/import/")
    counts = importer.apply(batch.payload, overwrite_local=request.POST.get("overwrite") == "1",
                            cancel_missing=request.POST.get("cancel_missing") == "1",
                            sender_domains=request.POST.get("sender_domains") == "1", user=_u(request))
    batch.applied = True
    batch.save()
    messages.success(request, f"Imported: {counts['new']} new, {counts['updated']} updated, {counts['kept_local']} "
                              f"local edits kept, {counts['skipped_sent']} already sent (left alone), "
                              f"{counts['rescheduled']} status changed by a new send time, "
                              f"{counts['cancelled']} cancelled (not in the file).")
    return redirect("/injects/")


@excon_required
@require_POST
def import_attachments(request):
    up = request.FILES.get("file")
    if not up or not up.name.lower().endswith(".zip"):
        messages.error(request, "Upload a .zip of the inject documents.")
        return redirect("/import/")
    try:
        documents.start(up, _u(request))
    except documents.ZipError as e:
        messages.error(request, str(e))
        return redirect("/import/")
    messages.success(request, f"{up.name} received. Its documents are being matched to injects; progress is shown below.")
    return redirect("/import/")


@excon_required
@require_POST
def injects_clear(request):
    """Start over: delete every inject's attachments, or every inject (with its attachments,
    rubric, grades and test-run items). Deliveries and replies stay, unlinked, in the Log,
    the inboxes and Replies."""
    what = request.POST.get("what")
    if (request.POST.get("confirm") or "").strip() != "DELETE":
        messages.error(request, "Type DELETE to confirm.")
        return redirect("/import/")
    if Inject.objects.filter(status="sending").exists():
        messages.error(request, "An inject is being sent; try again when it has finished.")
        return redirect("/import/")
    if any(j.active for j in AttachmentImport.objects.filter(status="running")):
        messages.error(request, "A zip of documents is being matched; try again when it has finished.")
        return redirect("/import/")
    with transaction.atomic():
        n_att = Attachment.objects.count()
        if what == "injects":
            n_inj = Inject.objects.count()
            Inject.objects.all().delete()
            detail = f"{n_inj} injects, {n_att} attachments"
        elif what == "attachments":
            Attachment.objects.all().delete()
            detail = f"{n_att} attachments"
        else:
            raise Http404
    audit(f"clear_{what}", detail, _u(request))
    messages.success(request, f"Deleted {detail}.")
    return redirect("/import/")


# --- senders -----------------------------------------------------------------------------------

@excon_required
def senders_page(request):
    if request.method == "POST":
        P = request.POST
        sid = P.get("id")
        local = re.sub(r"@.*$", "", (P.get("local_part") or "").strip().lower())
        if not rnd.LOCAL.match(local.replace("{team}", "x")):
            messages.error(request, f"'{local}' is not a valid username (letters, digits, . _ + - and {{team}}).")
            return redirect("/senders/")
        reply_to = (P.get("reply_to") or "").strip()
        if reply_to and not rnd.EMAIL.match(reply_to):
            messages.error(request, f"Reply-to '{reply_to}' is not an email address.")
            return redirect("/senders/")
        domain = (P.get("domain") or "").strip().lower().lstrip("@")
        if domain and (not rnd.DOMAIN.match(domain) or domain.endswith(f"teams.{settings.MAIL_DOMAIN}")):
            messages.error(request, f"'{domain}' is not a valid domain (e.g. epeigonia.org).")
            return redirect("/senders/")
        inapp_local = re.sub(r"@.*$", "", (P.get("inapp_local") or "").strip().lower())
        if inapp_local == local:
            inapp_local = ""
        if inapp_local and not rnd.LOCAL.match(inapp_local.replace("{team}", "x")):
            messages.error(request, f"'{inapp_local}' is not a valid username (letters, digits, . _ + - and {{team}}).")
            return redirect("/senders/")
        if Sender.objects.filter(local_part=local).exclude(pk=int(sid or 0)).exists():
            messages.error(request, f"The username {local} is already used by another sender.")
            return redirect("/senders/")
        x = get_object_or_404(Sender, pk=_int(sid)) if sid else Sender()
        old = x.local_part
        x.local_part, x.display_name, x.reply_to = local, (P.get("display_name") or "").strip(), reply_to
        x.inapp_local, x.domain = inapp_local, domain
        clash = next((o for o in Sender.objects.exclude(pk=x.pk) if o.inapp_address == x.inapp_address), None)
        if clash:
            messages.error(request, f"{x.inapp_address} is already the SIMEX Mail address of {clash.local_part}.")
            return redirect("/senders/")
        x.active = P.get("active") == "1" or not sid
        x.team_leader = P.get("team_leader") == "1"
        x.save()
        if x.team_leader:  # one team leader persona: each team's leader plays it
            Sender.objects.exclude(pk=x.pk).filter(team_leader=True).update(team_leader=False)
        renamed = 0
        if old and old != local and P.get("rename_injects") == "1":
            renamed = Inject.objects.filter(sender_local=old).update(sender_local=local)
        audit("sender_save", local, _u(request))
        messages.success(request, f"Saved {local} ({x.inapp_address} in SIMEX Mail)." + (f" {renamed} injects now use it." if renamed else ""))
        return redirect("/senders/")
    usage = dict(Inject.objects.values_list("sender_local").annotate(n=Count("id")).values_list("sender_local", "n"))
    senders = list(Sender.objects.all())
    for x in senders:
        x.usage = usage.get(x.local_part, 0)
    return render(request, "ops/senders.html", {"tab": "senders", "senders": senders})


@excon_required
@require_POST
def sender_delete(request, sid):
    x = Sender.objects.filter(pk=sid).first()
    if x:
        audit("sender_delete", x.local_part, _u(request))
        x.delete()
    return redirect("/senders/")


# --- people, teams, accounts -----------------------------------------------------------------------

def _password():
    words = ["cyclone", "balanjar", "plaida", "osocc", "sally", "tarim", "lagash", "relief", "convoy", "beacon",
             "harbour", "monsoon", "radio", "atlas", "summit", "compass"]
    return f"{secrets.choice(words)}-{secrets.choice(words)}-{secrets.randbelow(900) + 100}"


def _username_for(person):
    base = re.sub(r"[^a-z0-9]+", ".", person.name.lower()).strip(".")[:24] or "participant"
    if person.team:
        base = f"{person.team.code}.{base}"
    name, n = base, 1
    while User.objects.filter(username=name).exists():
        n += 1
        name = f"{base}{n}"
    return name


def ensure_account(person, reset=False):
    """Create (or reset) the login of a participant. Returns the new password, or None."""
    if person.user and not reset:
        return None
    pw = _password()
    if person.user:
        person.user.set_password(pw)
        person.user.is_active = True
        person.user.save()
    else:
        u = User(username=_username_for(person), email=person.email, first_name=person.name[:150])
        u.set_password(pw)
        u.save()
        person.user = u
        person.save(update_fields=["user"])
    return pw


@excon_required
def people_page(request):
    teams = list(Team.objects.all())
    people = list(Person.objects.select_related("team", "user"))
    has_placeholders = any(rnd.PLACEHOLDER_DOMAIN.search(p.email or "") for p in people)
    creds = request.session.pop("new_credentials", None)
    for t in teams:  # one team leader (ExCon) and one or more members sharing the inbox
        mine = [p for p in people if p.team_id == t.id and p.active]
        t.n_leaders = sum(p.role == "tl" for p in mine)
        t.n_members = sum(p.role == "member" for p in mine)
    team_roles = list(TeamRole.objects.annotate(n=Count("people", filter=Q(people__role="member"))))
    return render(request, "ops/people.html", {"tab": "people", "teams": teams, "people": people, "ROLES": ROLES,
                                               "team_roles": team_roles,
                                               "has_placeholders": has_placeholders, "creds": creds,
                                               "SIGNIN_DAYS": signin.TOKEN_DAYS,
                                               "creds_json": json.dumps(creds or [])})


@excon_required
@require_POST
def team_save(request):
    P = request.POST
    tid = P.get("id")
    code = re.sub(r"[^a-z0-9-]", "", (P.get("code") or "").strip().lower())
    name, email = (P.get("name") or "").strip(), (P.get("email") or "").strip()
    if not code or not name:
        messages.error(request, "A team needs a code (used in addresses) and a name.")
        return redirect("/people/")
    if email and not rnd.EMAIL.match(email):
        messages.error(request, f"'{email}' is not an email address.")
        return redirect("/people/")
    if Team.objects.filter(code=code).exclude(pk=int(tid or 0)).exists():
        messages.error(request, f"Team code '{code}' is taken.")
        return redirect("/people/")
    t = get_object_or_404(Team, pk=_int(tid)) if tid else Team()
    t.code, t.name, t.email, t.sort = code, name, email, _int(P.get("sort")) or 0
    t.active = P.get("active") == "1" or not tid
    t.save()
    audit("team_save", f"{code} {name}", _u(request))
    return redirect("/people/")


@excon_required
@require_POST
def team_delete(request, tid):
    t = Team.objects.filter(pk=tid).first()
    if t and (Mail.objects.filter(team=t, is_test=False).exists() or Grade.objects.filter(team=t).exists()
              or Response.objects.filter(team=t, kind="live").exists()):
        # Deleting would take its inbox, replies' team and grades with it.
        messages.error(request, f"{t.name} has exercise mail or grades: make it inactive instead of deleting it.")
        return redirect("/people/")
    if t:
        audit("team_delete", t.name, _u(request))
        t.delete()
    return redirect("/people/")


@excon_required
@require_POST
def team_role_save(request):
    """A team role (Deputy Team Leader...) that team members' accounts can hold."""
    P = request.POST
    rid = P.get("id")
    name = " ".join((P.get("name") or "").split())[:80]
    if not name:
        messages.error(request, "A team role needs a name.")
        return redirect("/people/#team-roles")
    if TeamRole.objects.filter(name__iexact=name).exclude(pk=_int(rid) or 0).exists():
        messages.error(request, f"There is already a team role '{name}'.")
        return redirect("/people/#team-roles")
    r = get_object_or_404(TeamRole, pk=_int(rid)) if rid else TeamRole(sort=TeamRole.objects.count())
    r.name = name
    if P.get("sort") not in (None, ""):
        r.sort = _int(P.get("sort")) or 0
    r.save()
    audit("team_role_save", name, _u(request))
    return redirect("/people/#team-roles")


@excon_required
@require_POST
def team_role_delete(request, rid):
    r = TeamRole.objects.filter(pk=rid).first()
    if r:  # its people keep their accounts, without a team role
        audit("team_role_delete", r.name, _u(request))
        r.delete()
    return redirect("/people/#team-roles")


def _team_role(name):
    """The TeamRole called name (any case), created if new; None for an empty name."""
    name = " ".join((name or "").split())[:80]
    if not name:
        return None
    return TeamRole.objects.filter(name__iexact=name).first() or TeamRole.objects.create(
        name=name, sort=TeamRole.objects.count())


def _store_creds(request, rows):
    request.session["new_credentials"] = rows


def _leader_problem(p):
    """Why p can't be a team leader (one active leader per team, who needs a team), or None."""
    if p.role != "tl" or not p.active:
        return None
    if not p.team_id:
        return f"{p.name}: a team leader needs a team."
    other = Person.objects.filter(team_id=p.team_id, role="tl", active=True).exclude(pk=p.pk).first()
    return f"{p.team.name} already has a team leader ({other.name}): one per team." if other else None


@excon_required
@require_POST
def person_save(request):
    P = request.POST
    pid = P.get("id")
    name, email = (P.get("name") or "").strip(), (P.get("email") or "").strip()
    if not name or (email and not rnd.EMAIL.match(email)):
        messages.error(request, "A person needs a name (and, if given, a valid email).")
        return redirect("/people/")
    p = get_object_or_404(Person, pk=_int(pid)) if pid else Person()
    old_role = p.role if pid else None
    p.name, p.email = name, email
    p.team_id = _int(P.get("team_id"))
    p.role = "tl" if P.get("role") == "tl" else "member"
    p.team_role = TeamRole.objects.filter(pk=_int(P.get("team_role_id"))).first() if p.role == "member" else None
    p.active = P.get("active") == "1" or not pid
    problem = _leader_problem(p)
    if problem:
        messages.error(request, problem)
        return redirect("/people/")
    p.save()
    if old_role and old_role != p.role:  # a different mailbox from now on: its devices subscribe again
        PushSubscription.objects.filter(person=p).delete()
    if p.user:
        p.user.is_active = p.active
        p.user.email, p.user.first_name = p.email, p.name[:150]
        p.user.save()
    if not pid and P.get("account") == "1":
        pw = ensure_account(p)
        _store_creds(request, [[p.name, p.team.name if p.team else "", p.user.username, pw]])
    audit("person_save", f"{name} <{email}>", _u(request))
    return redirect("/people/")


@excon_required
@require_POST
def person_delete(request, pid):
    p = Person.objects.filter(pk=pid).first()
    if p:
        audit("person_delete", p.name, _u(request))
        if p.user and not p.user.is_staff:
            p.user.delete()
        p.delete()
    return redirect("/people/")


@excon_required
@require_POST
def person_account(request, pid):
    """Create a login, reset its password, or deactivate it."""
    p = get_object_or_404(Person, pk=pid)
    action = request.POST.get("action")
    if action in ("create", "reset"):
        pw = ensure_account(p, reset=action == "reset")
        if pw:
            _store_creds(request, [[p.name, p.team.name if p.team else "", p.user.username, pw]])
    elif action == "deactivate" and p.user:
        p.user.is_active = False
        p.user.save()
    elif action == "activate" and p.user:
        p.user.is_active = True
        p.user.save()
    audit(f"account_{action}", p.name, _u(request))
    return redirect("/people/")


@excon_required
@require_POST
@transaction.atomic  # replace_placeholders deletes first: an error later must undo that too
def people_bulk(request):
    """Paste CSV: name,email,team code,role (tl, empty for a member, or a team role such as
    Deputy Team Leader, created if new). Creates logins for new people; the passwords are
    shown once (and on a printable sheet)."""
    text = request.POST.get("csv") or ""
    added, errors, creds = 0, [], []
    if request.POST.get("replace_placeholders") == "1":
        for p in Person.objects.filter(email__endswith="placeholder.invalid"):
            if p.user and not p.user.is_staff:
                p.user.delete()
            p.delete()
    teams = {t.code: t for t in Team.objects.all()}
    for n, row in enumerate(csv.reader(io.StringIO(text)), 1):
        row = [c.strip() for c in row]
        if not row or not any(row) or row[0].lower() == "name":
            continue
        name, email = row[0], row[1] if len(row) > 1 else ""
        code = re.sub(r"[^a-z0-9-]", "", row[2].lower()) if len(row) > 2 else ""
        role_cell = row[3] if len(row) > 3 else ""
        role = "tl" if role_cell.lower() in ("tl", "team leader", "leader") else "member"
        team_role = role_cell if role == "member" and role_cell.lower() not in ("member", "team member") else ""
        if email and not rnd.EMAIL.match(email):
            errors.append(f"line {n}: bad email '{email}'")
            continue
        if code and code not in teams:
            teams[code] = Team.objects.create(code=code, name=f"Team {code.upper()}",
                                              sort=len(teams), is_test=code == "test")
        existing = Person.objects.filter(email__iexact=email).first() if email else \
            Person.objects.filter(name=name, team=teams.get(code)).first()
        p = existing or Person()
        old_role = p.role if p.pk else None
        p.name, p.email, p.role, p.active = name, email, role, True
        p.team = teams[code] if code else None
        problem = _leader_problem(p)
        if problem:
            errors.append(f"line {n}: {problem}")
            continue
        p.team_role = _team_role(team_role)
        p.save()
        if old_role and old_role != p.role:
            PushSubscription.objects.filter(person=p).delete()
        if request.POST.get("accounts") == "1":
            pw = ensure_account(p)
            if pw:
                creds.append([p.name, p.team.name if p.team else "", p.user.username, pw])
        added += 1
    audit("people_bulk", f"{added} rows, {len(creds)} logins", _u(request))
    messages.success(request, f"{added} people added or updated, {len(creds)} new login(s).")
    if errors:
        messages.error(request, "; ".join(errors[:10]))
    if creds:
        _store_creds(request, creds)
    return redirect("/people/")


def _slips(request, rows):
    """Login slips ([name, team, username, password], password "" if not shown again), each
    with the QR code that signs that login in (signin.py)."""
    users = {u.username: u for u in User.objects.filter(username__in=[r[2] for r in rows])}
    slips = []
    for name, team, username, password in rows:
        u = users.get(username)
        link = signin.url(u) if signin.can_use(u) else ""
        slips.append({"name": name, "team": team, "username": username, "password": password,
                      "qr": signin.qr_svg(link) if link else ""})
    return render(request, "ops/credentials.html", {"rows": slips, "url": settings.MAIL_URL or settings.PUBLIC_URL,
                                                    "expires": signin.expires()})


@excon_required
@require_POST
def credentials_sheet(request):
    """Printable sheet of the credentials just shown (posted back from the page; not stored)."""
    return _slips(request, json.loads(request.POST.get("rows") or "[]"))


@excon_required
def signin_slips(request):
    """Printable sign-in slips (QR code and username, no password) for everyone with a login,
    a team (?team=) or one person (?person=). The passwords stay as they are."""
    people = Person.objects.filter(active=True, user__is_active=True, team__isnull=False).select_related("team", "user")
    if request.GET.get("team"):
        people = people.filter(team_id=_int(request.GET["team"]))
    if request.GET.get("person"):
        people = people.filter(pk=_int(request.GET["person"]))
    rows = [[p.name, p.team.name + (" · team leader" if p.role == "tl" else ""), p.user.username, ""] for p in people]
    audit("signin_slips", f"{len(rows)} slip(s)", _u(request))
    return _slips(request, rows)


# --- inbox monitor (ExCon view of any team's inbox) ---------------------------------------------------

@excon_required
def inboxes(request):
    """Team inboxes at a glance; "view as team" opens the participant client read-only."""
    if request.GET.get("team"):
        return redirect(f"/inbox/as/{int(request.GET['team'])}/")
    teams = list(Team.objects.all())
    personal = conf.flag("personal_mailboxes")
    for t in teams:
        mails = Mail.objects.filter(team=t, owner=None)
        t.members = list(t.people.participants().filter(active=True)) if personal else []
        t.n_inbox = mails.filter(folder="inbox").count()
        t.n_unread = mails.filter(folder="inbox", read_at__isnull=True, trashed_at__isnull=True).count()
        t.n_sent = mails.filter(folder="sent").count()
        t.last_in = mails.filter(folder="inbox").order_by("-created_at").values_list("created_at", flat=True).first()
        t.last_out = mails.filter(folder="sent").order_by("-created_at").values_list("created_at", flat=True).first()
        t.logins = t.people.participants().exclude(user=None).count()
        t.last_login = (User.objects.filter(person__team=t, person__role="member").exclude(last_login=None)
                        .order_by("-last_login").values_list("last_login", flat=True).first())
        t.lead = t.leader()
    return render(request, "ops/inboxes.html", {"tab": "inboxes", "teams": teams})


@excon_required
def excon_poll(request):
    """ExCon pages poll this without reloading: the Notifications panel (only when it changed
    since the page's `v`) and the counts in the status bar."""
    tick = conf.get("scheduler_tick")
    try:
        notices.scheduler_check(datetime.fromisoformat(tick) if tick else None)
    except ValueError:
        pass
    counts = dict(Inject.objects.values_list("status").annotate(n=Count("id")).values_list("status", "n"))
    out = {"new": Response.objects.filter(status="new", kind="live").count(),
           "overdue": counts.get("overdue", 0), "failed": counts.get("failed", 0) + counts.get("partial", 0),
           "paused": conf.flag("paused"), "v": notices.version(request.user), "unread": notices.unread(request.user)}
    if request.GET.get("v") != out["v"]:
        out["notices"] = notices.listing(request.user)
    return JsonResponse(out)


@excon_required
def notice_open(request, nid):
    """A notice clicked in the panel: read, then where it points."""
    n = get_object_or_404(Notice, pk=nid)
    notices.mark_read(request.user, Notice.objects.filter(pk=n.pk))
    return redirect(n.url if n.url.startswith("/") and not n.url.startswith("//") else "/injects/")


@excon_required
@require_POST
def notice_action(request, nid=None, action=""):
    """Dismiss one notice; or mark read / clear every notice of the panel's filter."""
    qs = (Notice.objects.filter(pk=nid) if nid else notices.of_kind(request.user, request.POST.get("kind", "")))
    (notices.mark_read if action == "read" else notices.dismiss)(request.user, qs)
    return JsonResponse({"ok": True, "unread": notices.unread(request.user)})


@excon_required
@require_POST
def inbox_clear(request):
    """Empty a team's mailbox after testing: its mails, its replies (with their AI analysis),
    ExCon's answers to them, and its grades. Inject deliveries stay in the Log."""
    team = get_object_or_404(Team, pk=int(request.POST.get("team") or 0))
    if (request.POST.get("confirm") or "").strip() != "CLEAR":
        messages.error(request, "Type CLEAR to empty the mailbox.")
        return redirect("/inboxes/")
    with transaction.atomic():
        reps = Response.objects.filter(team=team)
        n_answers = Message.objects.filter(kind="reply", response__in=reps).delete()[0]
        n_reps = reps.count()
        reps.delete()
        n_mails = Mail.objects.filter(team=team).count()
        Mail.objects.filter(team=team).delete()
        MailLabel.objects.filter(team=team).delete()
        n_grades = Grade.objects.filter(team=team).delete()[0] + AiGrade.objects.filter(team=team).delete()[0]
    detail = f"{team.name}: {n_mails} mails, {n_reps} replies, {n_answers} answers, {n_grades} grades"
    audit("inbox_clear", detail, _u(request))
    messages.success(request, f"Cleared {detail}.")
    return redirect("/inboxes/")


# --- test runs -----------------------------------------------------------------------------------

@excon_required
def tests_page(request):
    if request.method == "POST":
        return _tests_create(request)
    runs = list(TestRun.objects.order_by("-id")[:30])
    for r in runs:
        r.stats = dict(r.items.values_list("status").annotate(n=Count("id")).values_list("status", "n"))
    days = sorted({local_day(d) for d in Inject.objects.exclude(send_at=None).values_list("send_at", flat=True)})
    return render(request, "ops/tests.html", {"tab": "tests", "runs": runs, "days": days,
                                              "default_start": local_input(timezone.now() + timedelta(minutes=2)),
                                              "test_team": delivery.test_team()})


def _tests_create(request):
    P = request.POST
    redirect_to = (P.get("redirect") or "").strip()
    if redirect_to and not rnd.EMAIL.match(redirect_to.replace("{key}", "x")):
        messages.error(request, "The test address pattern must be an email address (optionally with {key}).")
        return redirect("/tests/")
    qs = Inject.objects.exclude(status="cancelled")
    scope = P.get("scope")
    if scope == "day":
        chosen = [i for i in qs if local_day(i.send_at) == P.get("day")]
    elif scope == "ids":
        want = [x.strip() for x in re.split(r"[,\s]+", P.get("ids") or "") if x.strip()]
        by = {i.mel_id: i for i in qs}
        chosen = [by[w] for w in want if w in by]
        missing = [w for w in want if w not in by]
        if missing:
            messages.warning(request, "Not found: " + ", ".join(missing))
    else:
        chosen = [i for i in qs if i.send_at]
    if not chosen:
        messages.error(request, "No injects selected for the test run.")
        return redirect("/tests/")
    channel = P.get("channel") if P.get("channel") in CHANNELS else ""
    if any((channel or i.effective_channel) == "email" for i in chosen) and not redirect_to:
        messages.error(request, "Some injects go by email: give a test address pattern.")
        return redirect("/tests/")
    try:
        start = from_local_input(P.get("start")) or timezone.now()
    except ValueError:
        start = timezone.now()
    factor = interval = None
    if P.get("timing") == "factor":
        factor = max(_num(P.get("factor")) or 60, 1.0)
    else:
        interval = max(_num(P.get("interval")) or 30, 5.0)
    name = (P.get("name") or "").strip() or f"Test {timezone.localtime():%d %b %H:%M}"
    if redirect_to:
        conf.put("test_redirect", redirect_to)
    run = delivery.create_test_run(name, chosen, start, redirect_to, P.get("dry_run") == "1", factor=factor,
                                   interval=interval, user=_u(request), channel=channel)
    messages.success(request, f"Test run '{name}' created with {len(chosen)} injects.")
    return redirect(f"/tests/{run.id}/")


@excon_required
def test_run_page(request, rid):
    run = get_object_or_404(TestRun, pk=rid)
    items = list(run.items.select_related("inject"))
    for it in items:
        it.via = "SIMEX Mail" if run.channel_for(it.inject) == "inapp" else "email"
    msg_counts = dict(Message.objects.filter(run=run).values_list("status").annotate(n=Count("id"))
                      .values_list("status", "n"))
    return render(request, "ops/test_run.html", {"tab": "tests", "run": run, "items": items, "msg_counts": msg_counts,
                                                 "test_team": delivery.test_team()})


@excon_required
@require_POST
def test_run_action(request, rid):
    run = get_object_or_404(TestRun, pk=rid)
    action = request.POST.get("action")
    if action == "pause" and run.status == "active":
        run.status = "paused"
    elif action == "resume" and run.status == "paused":
        run.status = "active"
    elif action == "cancel" and run.status in ("active", "paused"):
        run.status = "cancelled"
        run.items.filter(status="pending").update(status="cancelled")
    elif action == "now" and run.status in ("active", "paused"):
        pending = list(run.items.filter(status="pending").order_by("due_at"))
        if pending:
            shift = pending[0].due_at - timezone.now()
            for it in pending:
                it.due_at -= shift
                it.save(update_fields=["due_at"])
    run.save()
    audit(f"test_run_{action}", run.name, _u(request))
    return redirect(f"/tests/{rid}/")


# --- replies & grading ------------------------------------------------------------------------------

def conversations(reps):
    """Responses (newest first) grouped into back-and-forths, newest activity first."""
    groups = {}
    for r in reps:
        groups.setdefault(inbound.thread_key(r), []).append(r)
    answered = {}
    for rid, at in Message.objects.filter(kind="reply", response__in=reps).values_list("response_id", "created_at"):
        answered[rid] = max(at, answered.get(rid, at))
    out = []
    for msgs in groups.values():
        msgs.sort(key=lambda r: r.received_at)
        live = [r for r in msgs if r.status != "ignored"] or msgs
        latest = live[-1]
        last_answer = max((answered[r.id] for r in msgs if r.id in answered), default=None)
        out.append({"latest": latest, "first": msgs[0], "n": len(msgs), "preview": inbound.split_quoted(latest.text or "")[0], "answers": sum(r.id in answered for r in msgs),
                    "new": any(r.status == "new" for r in msgs),
                    "inject": next((r.inject for r in msgs if r.inject_id), None),
                    "guess": any(r.match == "guess" for r in msgs), "files": any(r.attachments.all() or (r.mail and r.mail.files.all()) for r in msgs),
                    "error": any(r.error for r in msgs),
                    "waiting": latest.status != "ignored" and (last_answer is None or last_answer < latest.received_at),
                    "ignored": all(r.status == "ignored" for r in msgs)})
    out.sort(key=lambda c: c["latest"].received_at, reverse=True)
    return out


def reply_grid(kind):
    """Teams, every Response of this kind, the inject x team matrix and per-team totals
    (Replies tab and Dashboard)."""
    kinds = ["live", "reply", "followup", "leader"] if kind == "live" else ["test", "oneoff"]
    teams = list(Team.objects.filter(active=True, is_test=(kind != "live")) if kind != "live"
                 else Team.objects.filter(active=True, is_test=False))
    all_in = list(Response.objects.filter(kind=kind).select_related("inject", "team", "person__team_role", "mail", "message")
                  .prefetch_related(Prefetch("attachments", ResponseAttachment.objects.defer("data")),
                                    Prefetch("mail__files", MailAttachment.objects.defer("data")))
                  .order_by("received_at"))
    by_cell = {}
    for r in all_in:
        if r.status != "ignored":
            by_cell.setdefault((r.inject_id, r.team_id), []).append(r)
    sent = inbound.first_sent(kinds)
    maxes = dict(RubricCriterion.objects.values_list("inject_id").annotate(s=Sum("max_points"))
                 .values_list("inject_id", "s"))
    grades = {(g.inject_id, g.team_id): g for g in Grade.objects.all()}
    ai_grades = {(g.inject_id, g.team_id): g for g in AiGrade.objects.filter(status="done")}
    row_ids = {r.inject_id for r in all_in if r.inject_id and r.status != "ignored"}
    row_ids |= set(Inject.objects.filter(expects_reply=True).values_list("id", flat=True))
    injects_rows = sorted(Inject.objects.filter(id__in=row_ids), key=lambda i: (i.send_at is None, i.send_at or timezone.now(), i.seq))
    now = timezone.now()
    team_sum = {t.id: {"team": t, "replies": 0, "answered": 0, "expected": 0, "minutes": [], "score": 0.0, "max": 0.0,
                       "late": 0, "ai_score": 0.0, "ai_max": 0.0} for t in teams}
    matrix = []
    for inj in injects_rows:
        cells = []
        for t in teams:
            reps = by_cell.get((inj.id, t.id), [])
            sent_at = sent.get((inj.id, t.id)) or sent.get((inj.id, None))
            first = reps[0] if reps else None
            minutes = int((first.received_at - sent_at).total_seconds() // 60) if first and sent_at else None
            g = grades.get((inj.id, t.id))
            due = sent_at + timedelta(minutes=inj.reply_due_minutes) if sent_at and inj.reply_due_minutes else None
            if reps:
                state = "late" if due and first.received_at > due else "replied"
            elif sent_at and inj.expects_reply:
                state = "missing" if due and now > due else "waiting"
            elif sent_at:
                state = "none"
            else:
                state = "unsent"
            score = g.total() if g else None
            ag = ai_grades.get((inj.id, t.id))
            cells.append({"team": t, "replies": reps, "n": len(reps), "minutes": minutes, "grade": g, "state": state,
                          "score": score, "max": maxes.get(inj.id), "ai": ag.total() if ag else None})
            ts = team_sum[t.id]
            ts["replies"] += len(reps)
            if inj.expects_reply and sent_at:
                ts["expected"] += 1
                ts["answered"] += bool(reps)
            if minutes is not None:
                ts["minutes"].append(minutes)
            if state in ("late", "missing"):
                ts["late"] += 1
            if g and maxes.get(inj.id):
                ts["score"] += score
                ts["max"] += float(maxes[inj.id])
            if ag and maxes.get(inj.id):
                ts["ai_score"] += ag.total()
                ts["ai_max"] += float(maxes[inj.id])
        matrix.append((inj, cells))
    for ts in team_sum.values():
        mins = sorted(ts["minutes"])
        ts["median"] = mins[len(mins) // 2] if mins else None
        ts["pct"] = round(100 * ts["score"] / ts["max"]) if ts["max"] else None
        ts["ai_pct"] = round(100 * ts["ai_score"] / ts["ai_max"]) if ts["ai_max"] else None
    return teams, all_in, matrix, list(team_sum.values())


@excon_required
def replies_page(request):
    kind = request.GET.get("kind", "live")
    team_f, inject_f = _int(request.GET.get("team")) or 0, _int(request.GET.get("inject")) or 0
    status_f, matched_f = request.GET.get("status", ""), request.GET.get("matched", "")
    kinds = ["live", "reply", "followup", "leader"] if kind == "live" else ["test", "oneoff"]
    teams, all_in, matrix, team_sum = reply_grid(kind)
    # Read / opens & clicks: per inject x team, from the delivering Messages.
    eng_msgs = list(Message.objects.filter(kind__in=["live"] if kind == "live" else ["test", "oneoff"],
                                           status="sent").exclude(inject=None).exclude(person__role="tl")
                    .select_related("inject"))
    if kind != "live":  # test deliveries all go to the Test team: group them under it
        tt = delivery.test_team()
        for m in eng_msgs:
            m.team_id = tt.id if m.channel == "inapp" else m.team_id
    eng_teams = list(teams) + ([None] if any(m.team_id is None for m in eng_msgs) else [])
    eng = {}
    for m in eng_msgs:
        e = eng.setdefault((m.inject_id, m.team_id), {"n": 0, "read": 0, "opens": 0, "clicks": 0, "first": None,
                                                     "sent": m.sent_at, "bounced": 0, "channel": m.channel})
        e["n"] += 1
        e["read"] += bool(m.opened_at)
        e["opens"] += m.open_count
        e["clicks"] += m.click_count
        e["bounced"] += m.delivery in ("bounced", "complained")
        if m.opened_at and (e["first"] is None or m.opened_at < e["first"]):
            e["first"] = m.opened_at
        e["sent"] = min(e["sent"], m.sent_at) if e["sent"] and m.sent_at else (e["sent"] or m.sent_at)
    eng_rows = []
    eng_injects = sorted({m.inject for m in eng_msgs}, key=lambda i: (i.send_at is None, i.send_at or timezone.now(), i.seq))
    for inj in eng_injects:
        cells = []
        for t in eng_teams:
            e = eng.get((inj.id, t.id if t else None))
            if e and e["first"] and e["sent"]:
                e["minutes"] = max(0, int((e["first"] - e["sent"]).total_seconds() // 60))
            cells.append(e)
        eng_rows.append((inj, cells))
    eng_team_sum = []
    for t in eng_teams:
        es = [v for (iid, tid), v in eng.items() if tid == (t.id if t else None)]
        mins = sorted(v["minutes"] for v in es if v.get("minutes") is not None)
        eng_team_sum.append({"team": t, "n": sum(v["n"] for v in es), "read": sum(v["read"] for v in es),
                             "clicks": sum(v["clicks"] for v in es), "bounced": sum(v["bounced"] for v in es),
                             "median": mins[len(mins) // 2] if mins else None})
    inbox = [r for r in reversed(all_in)
             if (not team_f or r.team_id == team_f) and (not inject_f or r.inject_id == inject_f)
             and (not status_f or r.status == status_f) and (matched_f != "no" or r.match == "none")]
    view = "messages" if request.GET.get("view") == "messages" else "threads"
    return render(request, "ops/replies.html", {
        "view": view, "convs": conversations(inbox)[:300] if view == "threads" else [],
        "tab": "replies", "kind": kind, "teams": teams, "matrix": matrix, "team_sum": team_sum,
        "inbox": inbox[:300], "new_count": sum(1 for r in all_in if r.status == "new"),
        "unmatched": sum(1 for r in all_in if r.match == "none" and r.status != "ignored"),
        "team_f": team_f, "inject_f": inject_f, "status_f": status_f, "matched_f": matched_f,
        "injects_all": Inject.objects.order_by("seq").values_list("id", "mel_id"),
        "webhook_ready": bool(settings.RESEND_WEBHOOK_SECRET),
        "webhook_url": settings.PUBLIC_URL.rstrip("/") + "/webhooks/resend",
        "uses_email": conf.get("channel_mode") != "inapp" or Inject.objects.filter(channel="email").exists(),
        "eng_rows": eng_rows, "eng_teams": eng_teams, "eng_team_sum": eng_team_sum})


@excon_required
@require_POST
def replies_sync(request):
    try:
        added, fetched = inbound.sync()
        messages.success(request, f"Sync: {added} new received email(s), {fetched} fetched.")
    except Exception as e:
        messages.error(request, f"Sync failed: {e}")
    return redirect("/replies/")


@excon_required
def replies_export(request):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["inject", "subject", "team", "criterion", "points", "max", "comment", "grader", "updated"])
    crit = {}
    for c in RubricCriterion.objects.all():
        crit.setdefault(c.inject_id, []).append(c)
    for g in Grade.objects.select_related("inject", "team").order_by("inject__seq", "team__sort"):
        for c in crit.get(g.inject_id, []):
            w.writerow([g.inject.mel_id, g.inject.subject, g.team.name, c.label, (g.scores or {}).get(str(c.id), ""),
                        c.max_points, g.comment or "", g.grader or "",
                        timezone.localtime(g.updated_at).strftime("%Y-%m-%d %H:%M")])
    resp = HttpResponse(out.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="simex-grades.csv"'
    return resp


@excon_required
def reply_page(request, rid):
    r = get_object_or_404(Response.objects.select_related("inject", "team", "person", "message", "mail"), pk=rid)
    if request.method == "POST":
        P = request.POST
        if "inject_id" in P:
            r.inject_id = int(P["inject_id"]) if P.get("inject_id") else None
            r.team_id = int(P["team_id"]) if P.get("team_id") else None
            r.person_id = int(P["person_id"]) if P.get("person_id") else None
            r.match = "manual" if r.inject_id else "none"
        if P.get("status") in ("new", "reviewed", "ignored"):
            r.status = P["status"]
        if P.get("refetch") == "1":
            r.fetched = False
        r.save()
        audit("reply_update", f"#{rid}", _u(request))
        if P.get("refetch") == "1":
            inbound.fetch(rid)
        return redirect(f"/replies/{rid}/")
    if r.status == "new":
        r.status = "reviewed"
        r.save(update_fields=["status"])
    conv = inbound.conversation(r)
    for kind_, _, obj in conv:
        if kind_ == "team":
            obj.new_part, obj.quoted = inbound.split_quoted(obj.text or "")
        elif kind_ == "answer":
            obj.shown = inbound.split_quoted(obj.body or obj.text or "")[0]
        obj.file_list = conv_files(kind_, obj)
    latest = max((o for k, _, o in conv if k == "team" and o.status != "ignored"), key=lambda o: o.received_at, default=r)
    new_part, quoted = inbound.split_quoted(r.text or "")
    return render(request, "ops/reply.html", {
        "tab": "replies", "r": r, "minutes": inbound.response_minutes(r), "conv": conv, "latest": latest,
        "answers": r.answers.order_by("id"), "teams": Team.objects.all(), "people": Person.objects.participants().order_by("name"),
        "injects_all": Inject.objects.order_by("seq"), "new_part": new_part, "quoted": quoted,
        "sender": rnd.sender_by_local(r.persona)[0] if r.persona else None,
        "persona_addr": rnd.persona_addr(r.persona, r.channel) if r.persona else "",
        "analysis": ReplyAnalysis.objects.filter(response=r).first(), "ai_on": ai.enabled(), "FLAGS": ai.FLAGS,
        "ai_draft": InjectDraft.objects.filter(pk=_int(request.GET.get("draft")), mode="answer", status="done",
                                               response__in=[o for k, _, o in conv if k == "team"]).first(),
        "inject_files": r.inject.attachments.defer("data") if r.inject else [],
        "max_mb": delivery.EMAIL_FILES_MAX // (1024 * 1024)})


@excon_required
@require_POST
def reply_answer(request, rid):
    r = get_object_or_404(Response, pk=rid)
    body = (request.POST.get("body") or "").replace("\r\n", "\n").strip()
    uploads = request.FILES.getlist("files")
    docs = list(Attachment.objects.filter(inject_id=r.inject_id, id__in=[_int(x) for x in request.POST.getlist("att")])
                if r.inject_id else [])
    if not body and not uploads and not docs:
        messages.error(request, "Write a reply first.")
        return redirect(f"/replies/{rid}/")
    if sum(f.size for f in uploads) + sum(a.size for a in docs) > delivery.EMAIL_FILES_MAX:
        messages.error(request, f"Attachments are over {delivery.EMAIL_FILES_MAX // (1024 * 1024)} MB.")
        return redirect(f"/replies/{rid}/")
    if conf.flag("paused"):
        messages.error(request, "Sending is paused.")
        return redirect(f"/replies/{rid}/")
    if not r.persona or (r.channel == "inapp" and not r.team_id):
        raise Http404
    files = [(re.sub(r"[\r\n]+", " ", PurePosixPath(f.name.replace("\\", "/")).name) or "file",
              f.content_type or "application/octet-stream", f.read()) for f in uploads]
    draft = InjectDraft.objects.filter(pk=_int(request.POST.get("ai_draft")), mode="answer", status="done",
                                       response__in=Response.objects.filter(team=r.team, inject=r.inject)).first()
    m = delivery.answer(r, body, user=_u(request), files=files, attachments=docs, ai_draft=draft)
    if m.status == "sent":
        messages.success(request, "Reply delivered." if r.channel == "inapp" else "Reply sent.")
    else:
        messages.error(request, f"Reply failed: {m.error}")
    return redirect(f"/replies/{rid}/")


# --- files on the Replies pages: any stored file, by source ----------------------------------------

FILE_SOURCES = {"a": Attachment, "m": MailAttachment, "r": ResponseAttachment, "x": MessageAttachment}
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
STORED = {"m": Q(data__isnull=False) | Q(attachment__isnull=False), "r": Q(data__isnull=False)}


def _with_stored(src, qs):
    return qs.defer("data").annotate(stored=ExpressionWrapper(STORED.get(src, Q(pk__isnull=False)),
                                                              output_field=BooleanField()))


def file_links(src, qs):
    """What a conversation item shows of its files: name, size, download link (none if the
    file was too large to keep) and whether it can be previewed as an image."""
    return [{"name": a.filename, "size": a.size, "url": f"/files/{src}/{a.id}/" if a.stored else None,
             "image": a.stored and a.filename.lower().endswith(IMAGE_EXT)} for a in _with_stored(src, qs)]


def conv_files(kind_, o):
    if kind_ == "team":
        if o.channel == "email":
            return file_links("r", o.attachments.all())
        return file_links("m", o.mail.files.all()) if o.mail_id else []
    out = file_links("a", Attachment.objects.filter(id__in=o.attachment_ids or []))
    return out + (file_links("x", o.files.all()) if kind_ == "answer" else [])


def _file(src, fid):
    model = FILE_SOURCES.get(src)
    if not model:
        raise Http404
    a = get_object_or_404(model, pk=fid)
    data = a.content() if src == "m" else a.data
    if data is None:
        raise Http404
    return a, bytes(data)


def _image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@excon_required
def file_get(request, src, fid):
    a, data = _file(src, fid)
    return _download(data, a.filename)


@excon_required
def file_view(request, src, fid):
    """An image shown in the page (participants' uploads included): only real PNG, JPEG, GIF
    or WebP by their bytes, never what the uploader called it; anything else is a 404."""
    a, data = _file(src, fid)
    ctype = _image_type(data)
    if not ctype:
        raise Http404
    resp = HttpResponse(data, content_type=ctype)
    resp["Content-Disposition"] = content_disposition_header(False, re.sub(r"[\r\n]+", " ", a.filename))
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Content-Security-Policy"] = "default-src 'none'; sandbox"
    resp["Cache-Control"] = "private, max-age=3600"
    return resp


@excon_required
def response_attachment(request, aid):
    a = get_object_or_404(ResponseAttachment, pk=aid)
    if a.data is None:
        raise Http404
    return _download(a.data, a.filename)


@excon_required
def grade_page(request, iid, tid):
    inj, team = get_object_or_404(Inject, pk=iid), get_object_or_404(Team, pk=tid)
    crit = list(inj.rubric.all())
    g = Grade.objects.filter(inject=inj, team=team).first()
    if request.method == "POST":
        g = g or Grade(inject=inj, team=team)
        scores = {}
        for c in crit:
            v = (request.POST.get(f"c{c.id}") or "").strip()
            if v:
                try:
                    scores[str(c.id)] = max(0.0, min(float(v), float(c.max_points)))
                except ValueError:
                    pass
        g.scores, g.comment = scores, (request.POST.get("comment") or "").strip() or None
        g.grader = (request.POST.get("grader") or "").strip() or _u(request)
        g.save()
        audit("grade", f"{inj.mel_id} {team.name}: {sum(scores.values()):g}", _u(request))
        messages.success(request, "Grade saved.")
        nxt = request.POST.get("next")
        return redirect(f"/grades/{iid}/{nxt}/" if nxt else "/replies/")
    reps = list(Response.objects.filter(inject=inj, team=team).exclude(status="ignored")
                .select_related("person", "mail").order_by("received_at"))
    sent = inbound.first_sent(["live", "reply", "followup", "leader", "test", "oneoff"] if team.is_test else ["live", "reply", "followup", "leader"])
    sent_at = sent.get((iid, tid)) or sent.get((iid, None))
    tags = {a.response_id: a for a in ReplyAnalysis.objects.filter(response__in=reps, status="done")}
    for r in reps:
        r.ai = tags.get(r.id)
        r.new_part = inbound.split_quoted(r.text or "")[0]
        r.after = int((r.received_at - sent_at).total_seconds() // 60) if sent_at else None
        r.files = list(r.attachments.all()) if r.channel == "email" else (list(r.mail.files.all()) if r.mail else [])
    teams = list(Team.objects.filter(active=True, is_test=team.is_test))
    ids = [t.id for t in teams]
    nxt = ids[ids.index(tid) + 1] if tid in ids and ids.index(tid) + 1 < len(ids) else None
    sug = AiGrade.objects.filter(inject=inj, team=team).first()
    stale = bool(sug and sug.status == "done" and sug.input_hash != ai.grade_hash(inj, crit, ai.thread(inj, team)))
    for c in crit:
        c.current = (g.scores or {}).get(str(c.id)) if g else None
        c.ai = (sug.scores or {}).get(str(c.id)) if sug and sug.status == "done" else None
        c.ai_note = (sug.notes or {}).get(str(c.id)) if sug and sug.status == "done" else None
        c.choices = [(n, c.current is not None and int(c.current) == n) for n in range(c.max_points + 1)]
    return render(request, "ops/grade.html", {"tab": "replies", "inj": inj, "team": team, "crit": crit, "g": g,
                                              "reps": reps, "sent_at": sent_at, "teams": teams, "next_team": nxt,
                                              "sug": sug, "stale": stale, "ai_on": ai.enabled()})


@excon_required
@require_POST
def rubric_save(request, iid):
    inj = get_object_or_404(Inject, pk=iid)
    P = request.POST
    inj.expects_reply = P.get("expects_reply") == "1"
    inj.critical = P.get("critical") == "1"
    try:
        inj.reply_due_minutes = int(P["reply_due_minutes"]) if P.get("reply_due_minutes") else None
    except ValueError:
        inj.reply_due_minutes = None
    inj.save()
    for c in inj.rubric.all():
        if P.get(f"del{c.id}") == "1":
            # its points leave the totals too (else a team could score 6/3)
            for g in list(Grade.objects.filter(inject=inj)) + list(AiGrade.objects.filter(inject=inj)):
                if str(c.id) in (g.scores or {}):
                    g.scores.pop(str(c.id))
                    g.save(update_fields=["scores"])
            c.delete()
            continue
        c.label = (P.get(f"label{c.id}") or c.label).strip()
        c.description = (P.get(f"desc{c.id}") or "").strip() or None
        c.max_points = max(1, _int(P.get(f"max{c.id}")) or c.max_points)
        c.critical = P.get(f"crit{c.id}") == "1"
        c.sort = _int(P.get(f"sort{c.id}")) if _int(P.get(f"sort{c.id}")) is not None else c.sort
        c.save()
    label = (P.get("new_label") or "").strip()
    if label:
        RubricCriterion.objects.create(inject=inj, label=label, description=(P.get("new_desc") or "").strip() or None,
                                       max_points=max(1, _int(P.get("new_max")) or 3), sort=inj.rubric.count(),
                                       critical=P.get("new_crit") == "1")
    if _int(P.get("copy_from")):
        for c in RubricCriterion.objects.filter(inject_id=_int(P["copy_from"])):
            RubricCriterion.objects.create(inject=inj, label=c.label, description=c.description,
                                           max_points=c.max_points, sort=c.sort, critical=c.critical)
    audit("rubric_save", inj.mel_id, _u(request))
    messages.success(request, "Rubric saved.")
    return redirect(f"/injects/{iid}/#rubric")


@excon_required
def help_page(request):
    from . import docs
    return render(request, "ops/help.html", {"tab": "help", "manual": docs.html("MANUAL.md")})


# --- log & settings --------------------------------------------------------------------------------

@excon_required
def log_page(request):
    kind, status = request.GET.get("kind", ""), request.GET.get("status", "")
    qs = Message.objects.select_related("inject").order_by("-id")
    if kind:
        qs = qs.filter(kind=kind)
    if status:
        qs = qs.filter(status=status)
    return render(request, "ops/log.html", {"tab": "log", "msgs": qs[:300], "audits": Audit.objects.all()[:150],
                                            "kind": kind, "status": status})


@excon_required
def message_page(request, mid):
    m = get_object_or_404(Message.objects.select_related("inject"), pk=mid)
    atts = file_links("a", Attachment.objects.filter(id__in=m.attachment_ids or [])) + file_links("x", m.files.all())
    return render(request, "ops/message.html", {"tab": "log", "m": m, "atts": atts})


EDITABLE = ["footer", "subject_prefix", "default_reply_to", "excon_bcc", "test_address", "test_redirect",
            "grace_minutes", "max_upload_mb"]


@excon_required
def settings_page(request):
    if request.method == "POST":
        P = request.POST
        for k in EDITABLE:
            v = (P.get(k) or "").replace("\r\n", "\n")
            if k in ("default_reply_to", "test_address") and v.strip() and not rnd.EMAIL.match(v.strip()):
                messages.error(request, f"{k.replace('_', ' ')}: '{v}' is not an email address; not saved.")
                continue
            if k == "excon_bcc":
                bad = [a for a in rnd.split_addresses(v) if not rnd.EMAIL.match(a)]
                if bad:
                    messages.error(request, f"ExCon Bcc: not email addresses: {', '.join(bad)}; not saved.")
                    continue
            if k in ("grace_minutes", "max_upload_mb"):
                try:
                    v = str(max(0, int(float(v or (10)))))
                except ValueError:
                    v = "10"
            conf.put(k, v if k == "footer" else v.strip())
        conf.put("team_to_team", "1" if P.get("team_to_team") == "1" else "0")
        conf.put("personal_mailboxes", "1" if P.get("personal_mailboxes") == "1" else "0")
        conf.put("subject_inject_code", "1" if P.get("subject_inject_code") == "1" else "0")
        audit("settings_save", None, _u(request))
        messages.success(request, "Settings saved.")
        return redirect("/settings/")
    from .views_ai import ai_settings_context
    return render(request, "ops/settings.html", {"tab": "settings", "resend_configured": bool(settings.RESEND_API_KEY),
                                                 "mailer_name": mailer.get().name, "MAIL_DOMAIN": settings.MAIL_DOMAIN,
                                                 **ai_settings_context()})


@excon_required
@require_POST
def settings_arm(request):
    if request.POST.get("arm") == "1":
        if (request.POST.get("confirm") or "").strip().upper() != "SEND LIVE":
            messages.error(request, "Type SEND LIVE to arm live sending.")
            return redirect("/settings/")
        conf.put("live_armed", "1")
        audit("live_armed", None, _u(request))
        messages.warning(request, "Live sending armed: scheduled injects will go to participants.")
    else:
        conf.put("live_armed", "0")
        audit("live_disarmed", None, _u(request))
        messages.success(request, "Live sending disarmed.")
    return redirect("/settings/")


@excon_required
@require_POST
def settings_channel(request):
    mode = request.POST.get("mode")
    if mode not in conf.CHANNEL_MODES:
        messages.error(request, "Unknown delivery channel.")
        return redirect("/settings/#channel")
    old = conf.get("channel_mode")
    if mode != old and (request.POST.get("confirm") or "").strip().upper() != "SWITCH":
        messages.error(request, "Type SWITCH to change how injects are delivered.")
        return redirect("/settings/#channel")
    conf.put("channel_mode", mode)
    audit("channel_mode", f"{old} -> {mode}", _u(request))
    msg = f"Injects are now delivered by: {conf.CHANNEL_MODES[mode]}."
    if mode == "inapp":
        msg += " Participants need their SIMEX Mail logins (People & logins)."
    messages.warning(request, msg)
    return redirect("/settings/#channel")


@excon_required
@require_POST
def settings_pause(request):
    paused = request.POST.get("paused") == "1"
    conf.put("paused", "1" if paused else "0")
    audit("paused" if paused else "resumed", None, _u(request))
    (messages.warning if paused else messages.success)(request, "All sending paused." if paused else "Sending resumed.")
    return redirect(safe_next(request.POST.get("back"), "/settings/"))


@excon_required
def settings_domain(request):
    try:
        return JsonResponse(mailer.get().domain_status(settings.MAIL_DOMAIN))
    except Exception as e:
        return JsonResponse({"error": f"{type(e).__name__}: {e}"})


@excon_required
def settings_backup(request):
    data = backup.export()
    audit("backup_download", None, _u(request))
    resp = HttpResponse(json.dumps(data), content_type="application/json")
    resp["Content-Disposition"] = f'attachment; filename="simex-inbox-backup-{timezone.localtime():%Y%m%d-%H%M}.json"'
    return resp


@excon_required
@require_POST
def settings_restore(request):
    up = request.FILES.get("file")
    if request.POST.get("confirm") != "RESTORE" or not up:
        messages.error(request, "Choose a backup file and type RESTORE.")
        return redirect("/settings/")
    try:
        data = json.loads(up.read().decode("utf-8"))
        if data.get("format") == legacy_import.FORMAT:
            counts = legacy_import.import_backup(data)  # a backup from the first (email-only) app
        else:
            counts = backup.restore(data)
    except Exception as e:
        messages.error(request, f"Restore failed, nothing changed: {e}")
        return redirect("/settings/")
    messages.warning(request, "Restored: " + ", ".join(f"{v} {k}" for k, v in counts.items())
                     + ". Live sending is disarmed. Log in again if your session ended.")
    return redirect("/settings/")

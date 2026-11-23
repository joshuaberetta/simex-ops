"""Claude's reading of the files teams send (DocumentAnalysis), for ExCon and the team's leader.

Every file a member or the team leader attaches (in SIMEX Mail, to anyone, or to an email to a
persona) is read once per team and file content: what it is, whether it answers the inject,
and an estimate of how likely it was written with an AI assistant, from its text and the facts
the file keeps about itself (ops/docscan.py: application, editing time, editing sessions,
created and modified times against when it was sent). Inject documents the team forwards, and
files personas or ExCon send, are not theirs and are left out.

Run by the scheduler's AI loop (ai.tick -> tick here), never in a request. discover() finds
every file a team has sent that has no reading yet, those sent before this existed included,
so turning it on mid-exercise covers what is already in the mailboxes. Settings → AI analysis:
"ai_documents" (on), "ai_auto" (else only on request), "ai_tests" (the Test team),
"ai_documents_deep" (the deep model). Suggestions only, never proof, never shown to members.
"""

import base64
import logging

from django.db.models import Q
from django.utils import timezone

from . import ai, conf, docscan, notices
from .models import DocumentAnalysis, MailAttachment, Response, ResponseAttachment, file_hash

log = logging.getLogger("simex.ai")

PROMPT_VERSION = 1
LIKELY, POSSIBLY = 70, 40
AUTHORS = ("member", "tl")


def enabled():
    return ai.enabled() and conf.flag("ai_documents")


def band(pct):
    if pct is None:
        return ""
    return "Likely AI-assisted" if pct >= LIKELY else "Possibly AI-assisted" if pct >= POSSIBLY else ""


# --- which files -------------------------------------------------------------------------------

def team_files():
    """(team id, sha256, values) of every stored file a team sent: what members and leaders
    attached in SIMEX Mail (their Sent copy; never a forwarded inject document) and the files of
    emails teams sent in."""
    mail = (MailAttachment.objects.filter(data__isnull=False, attachment__isnull=True, mail__folder="sent",
                                          mail__team__isnull=False, mail__author__role__in=AUTHORS,
                                          mail__author__team__isnull=False)
            .exclude(sha256="").order_by("id")
            .values("id", "sha256", "filename", "content_type", "size", "mail__author_id", "mail__author__team_id",
                    "mail__author__team__is_test", "mail__created_at", "mail__inject_id"))
    for f in mail:
        yield f["mail__author__team_id"], f["sha256"], {
            "mail_file_id": f["id"], "filename": f["filename"], "content_type": f["content_type"], "size": f["size"],
            "person_id": f["mail__author_id"], "sent_at": f["mail__created_at"], "inject_id": f["mail__inject_id"],
            "kind": "test" if f["mail__author__team__is_test"] else "live"}
    email = (ResponseAttachment.objects.filter(data__isnull=False, response__team__isnull=False, response__fetched=True)
             .exclude(response__status="ignored").exclude(sha256="").order_by("id")
             .values("id", "sha256", "filename", "content_type", "size", "response_id", "response__team_id",
                     "response__person_id", "response__received_at", "response__inject_id", "response__kind"))
    for f in email:
        yield f["response__team_id"], f["sha256"], {
            "response_file_id": f["id"], "response_id": f["response_id"], "filename": f["filename"],
            "content_type": f["content_type"], "size": f["size"], "person_id": f["response__person_id"],
            "sent_at": f["response__received_at"], "inject_id": f["response__inject_id"], "kind": f["response__kind"]}


def hash_missing(limit=200):
    """Files stored without a sha256 (restored from an older backup, the first app's import):
    hashed a few at a time, so they are found too."""
    for model in (MailAttachment, ResponseAttachment):
        for pk in model.objects.filter(data__isnull=False, sha256="").values_list("pk", flat=True)[:limit]:
            data = model.objects.filter(pk=pk).values_list("data", flat=True).first()
            if data is not None:
                model.objects.filter(pk=pk).update(sha256=file_hash(data))


def discover():
    """A queued DocumentAnalysis for each file a team sent that has none (the first copy found
    wins: the earliest sent). Returns how many were added."""
    hash_missing()
    have = set(DocumentAnalysis.objects.values_list("team_id", "sha256"))
    kinds = set(ai._kinds())
    new = {}
    for team_id, sha, v in team_files():
        if (team_id, sha) in have or v["kind"] not in kinds:
            continue
        if (team_id, sha) not in new or v["sent_at"] < new[(team_id, sha)]["sent_at"]:
            new[(team_id, sha)] = v
    for (team_id, sha), v in new.items():
        DocumentAnalysis.objects.get_or_create(team_id=team_id, sha256=sha, defaults={**v, "status": "queued"})
    return len(new)


def pending(now=None):
    now = now or timezone.now()
    base = DocumentAnalysis.objects.select_related("team", "person", "inject", "response")
    queued = list(base.filter(status="queued").order_by("sent_at", "id"))
    if not conf.flag("ai_auto"):
        return queued
    retry = ai._retryable(base.filter(kind__in=ai._kinds()), now).order_by("sent_at", "id")
    return queued + [d for d in retry if d not in queued]


def tick(limit):
    """Called from ai.tick: finds new files (automatic mode) and reads up to limit of them."""
    if not enabled():
        return 0
    if conf.flag("ai_auto"):
        discover()
    n = 0
    for d in pending()[:limit]:
        if not ai.under_cap():
            break
        ai._guarded(analyse, DocumentAnalysis, {"pk": d.pk}, d)
        n += 1
    return n


def queue(d):
    """ExCon's Analyse / Re-run."""
    DocumentAnalysis.objects.filter(pk=d.pk).update(status="queued", attempts=0, error=None, updated_at=timezone.now())


def for_file(team_id, sha):
    """The reading of one file of a team's, if it has one."""
    if not sha or not team_id:
        return None
    return DocumentAnalysis.objects.filter(team_id=team_id, sha256=sha).first()


def queue_file(team_id, sha):
    d = for_file(team_id, sha)
    if d:
        queue(d)
        return d
    for t, s, v in team_files():
        if t == team_id and s == sha:
            d, _ = DocumentAnalysis.objects.get_or_create(team_id=team_id, sha256=sha, defaults={**v, "status": "queued"})
            queue(d)
            return d
    return None


def prune(team=None):
    """Readings of files no longer stored anywhere (cleared mailboxes, a withdrawn delivery)."""
    qs = DocumentAnalysis.objects.filter(team=team) if team else DocumentAnalysis.objects.all()
    shas = set(qs.values_list("sha256", flat=True))
    if not shas:
        return 0
    kept = (set(MailAttachment.objects.filter(sha256__in=shas).values_list("sha256", flat=True))
            | set(ResponseAttachment.objects.filter(sha256__in=shas).values_list("sha256", flat=True)))
    return qs.exclude(sha256__in=kept).delete()[0]


# --- the reading -------------------------------------------------------------------------------

DOC_RULES, DOC_SYSTEM = ai.DOC_RULES, ai.DOC_SYSTEM

DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "doc_type": {"type": "string"},
        "answers_inject": {"type": "string", "enum": ["yes", "partly", "no", "unknown"]},
        "ai_written": {"type": "integer", "description": "0 to 100"},
        "ai_signals": {"type": "string"},
        "passages": {"type": "array", "items": {
            "type": "object", "properties": {"quote": {"type": "string"}, "why": {"type": "string"}},
            "required": ["quote", "why"], "additionalProperties": False}},
        "language": {"type": "string"},
    },
    "required": ["summary", "doc_type", "answers_inject", "ai_written", "ai_signals", "passages", "language"],
    "additionalProperties": False,
}


def covering(d):
    """The team's Response the file came with (email, or an in-app message to a persona)."""
    if d.response_id:
        return d.response
    r = (Response.objects.filter(team_id=d.team_id).filter(Q(mail__files__sha256=d.sha256) | Q(attachments__sha256=d.sha256))
         .select_related("inject", "team", "person", "message", "mail").order_by("received_at").first())
    return r


def _when(dt):
    return f"{timezone.localtime(dt):%a %d %b %H:%M}" if dt else "?"


def _span(minutes):
    minutes = round(minutes)
    if abs(minutes) < 120:
        return f"{minutes} minutes"
    if abs(minutes) < 72 * 60:
        return f"{minutes / 60:.0f} hours"
    return f"{minutes / 1440:.0f} days"


def fact_lines(d, doc, r=None):
    """The file's own facts as sentences (times in the exercise timezone), for the model and ExCon."""
    f, lines = doc.facts, []
    if f.get("application") or f.get("producer"):
        lines.append("Made with: " + " / ".join(x for x in (f.get("application"), f.get("producer")) if x) + ".")
    if f.get("template"):
        lines.append(f"Word template: {f['template']}.")
    who = [f"author {f['author']}" if f.get("author") else "", f"last modified by {f['last_modified_by']}"
           if f.get("last_modified_by") and f.get("last_modified_by") != f.get("author") else ""]
    if any(who):
        lines.append("In the file: " + ", ".join(x for x in who if x) + ".")
    created, modified = docscan.parse_time(f.get("created")), docscan.parse_time(f.get("modified"))
    if created or modified:
        bits = [f"created {_when(created)}" if created else "", f"last saved {_when(modified)}" if modified else ""]
        line = "File times: " + ", ".join(x for x in bits if x) + f"; sent {_when(d.sent_at)}"
        if created and d.sent_at >= created:
            line += f" ({_span((d.sent_at - created).total_seconds() / 60)} after it was created)"
        lines.append(line + ".")
    edit = docscan._int(f.get("editing_minutes"))
    sessions = docscan._int(f.get("editing_sessions"))
    if edit is not None or sessions is not None or f.get("revision"):
        bits = [f"{edit} minutes of editing time" if edit is not None else "",
                f"{sessions} editing sessions" if sessions is not None else "",
                f"revision {f['revision']}" if f.get("revision") else ""]
        line = "Recorded in the file: " + ", ".join(x for x in bits if x) + f", for {doc.words:,} words"
        if edit is not None and doc.words >= 200:
            line += f" ({doc.words / max(edit, 1):.0f} words per editing minute; fast typing is about 40-60)"
        lines.append(line + ".")
    if f.get("tracked_change_authors"):
        lines.append(f"Tracked changes by {f['tracked_change_authors']} people.")
    if f.get("comments"):
        lines.append(f"{f['comments']} comments in the margin.")
    opened = (r.message.opened_at or r.message.sent_at) if r and r.message else None
    if opened:
        lines.append(f"The team first opened the message this answers {_when(opened)}"
                     + (f", {_span((d.sent_at - opened).total_seconds() / 60)} before sending the file." if d.sent_at >= opened else "."))
    return lines


def _content(d, doc, data, r, inj):
    content = []
    if inj:
        content.append(ai._cached("The inject the team is answering:\n\n" + ai.inject_block(inj, d.team)))
    else:
        content.append({"type": "text", "text": "The inject this document answers is not known."})
    if r:
        content.append({"type": "text", "text": "The message the file came with:\n\n" + ai.reply_block(r, "MESSAGE")})
    who = d.person.name if d.person else "a member"
    if d.person and d.person.role == "tl":
        who += " (the team leader)"
    elif d.person and d.person.team_role_id:
        who += f" ({d.person.team_role.name})"
    head = [f"DOCUMENT {d.filename} ({doc.media_type or docscan.ext(d.filename) or 'file'}, {d.size:,} bytes"
            + (f", {doc.pages} pages" if doc.pages else "") + f"), sent by {who} of {d.team.name}"]
    lines = fact_lines(d, doc, r)
    head += (["", "File facts:"] + [f"- {x}" for x in lines]) if lines else ["", "The file records no facts about itself."]
    if doc.note:
        head += ["", doc.note]
    if doc.kind == "text":
        content.append({"type": "text", "text": "\n".join(head) + "\n\nIts text:\n\n" + doc.text})
    else:
        content.append({"type": "text", "text": "\n".join(head)})
        block = "document" if doc.kind == "pdf" else "image"
        content.append({"type": block, "source": {"type": "base64", "media_type": doc.media_type,
                                                  "data": base64.b64encode(data).decode()}})
    return content, lines


def analyse(d):
    """Read one file; stores and returns its DocumentAnalysis."""
    d.attempts += 1
    f = d.file()
    data = f.content() if isinstance(f, MailAttachment) else (bytes(f.data) if f and f.data is not None else None)
    if data is None:
        d.status, d.error = "skipped", "The file is no longer stored."
        d.save()
        return d
    doc = docscan.read(d.filename, d.content_type, data)
    r = covering(d)
    if r and not d.response_id:
        d.response = r
    inj = d.inject or (r.inject if r else None)
    d.inject = inj
    d.words, d.pages = doc.words, doc.pages
    d.facts = {"file": doc.facts, "lines": fact_lines(d, doc, r), "note": doc.note}
    if doc.kind == "none":
        d.status, d.error = "skipped", doc.note
        d.save()
        return d
    content, _ = _content(d, doc, data, r, inj)
    kind = "deep" if conf.flag("ai_documents_deep") else "fast"
    try:
        out, used = ai.call("document", f"d{d.id}", kind, ai.system("document"), content, DOC_SCHEMA, "medium", 6000)
    except ai.AiError as e:
        d.status, d.error = "failed", str(e)
        d.save()
        return d
    d.status, d.error, d.model, d.prompt_version = "done", None, used, PROMPT_VERSION
    d.summary = (out.get("summary") or "")[:2000]
    d.doc_type = (out.get("doc_type") or "")[:60]
    d.answers_inject = out.get("answers_inject") if out.get("answers_inject") in ("yes", "partly", "no", "unknown") else ""
    d.ai_written = ai._clamp(out.get("ai_written"), 0, 100)
    d.ai_signals = (out.get("ai_signals") or "")[:1000]
    d.passages = [{"quote": str(p.get("quote", ""))[:300], "why": str(p.get("why", ""))[:300]}
                  for p in (out.get("passages") or []) if isinstance(p, dict)][:4]
    d.language = (out.get("language") or "")[:20]
    d.save()
    notices.document(d)
    return d


# --- shown ------------------------------------------------------------------------------------

def readings(team_ids=None, kind=None):
    """Done readings, newest first."""
    qs = DocumentAnalysis.objects.filter(status="done").select_related("team", "person", "inject", "response")
    if team_ids is not None:
        qs = qs.filter(team_id__in=team_ids)
    if kind:
        qs = qs.filter(kind=kind)
    return qs


def by_sha(team_id, shas):
    """{sha256: DocumentAnalysis} of one team's files, for badges next to file links."""
    shas = [s for s in shas if s]
    if not shas or not team_id:
        return {}
    return {d.sha256: d for d in DocumentAnalysis.objects.filter(team_id=team_id, sha256__in=shas)}

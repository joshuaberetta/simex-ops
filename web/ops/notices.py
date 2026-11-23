"""ExCon's Notifications panel: one Notice per event, written where the event happens, and
each ExCon user's own read / dismissed state. Writing a notice never breaks what it reports
on (a failure is logged and skipped)."""

import logging

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .models import Notice, NoticeState

log = logging.getLogger(__name__)

KINDS = ["reply", "flag", "delivery", "system"]
SHOWN = 100  # the newest, in the panel
SCHEDULER_STALE = 120  # seconds without a tick (the status bar's warning)


def add(key, kind, title, body="", note="", url="", **links):
    """Create the notice for an event, or refresh its text (an email reply's content arrives
    after the webhook); its time and everyone's read state stay."""
    text = {"kind": kind, "title": title[:300], "body": (body or "")[:500], "note": (note or "")[:300], "url": url}
    try:
        with transaction.atomic():
            n = Notice.objects.filter(key=key[:120]).first()
            if n is None:
                return Notice.objects.create(key=key[:120], **text, **links)
            if any(getattr(n, k) != v for k, v in text.items()):
                Notice.objects.filter(pk=n.pk).update(**text, updated_at=timezone.now())
            return n
    except Exception:
        log.exception("notice %s not written", key)
        return None


def _team(t):
    return t.name if t else "Unknown team"


# --- the events -------------------------------------------------------------------------------

def reply(r):
    if r.status == "ignored":
        return None
    from .inbound import split_quoted
    test = r.kind == "test"
    return add(f"reply:{r.id}", "reply", f"{_team(r.team)}{' (test)' if test else ''} → {r.persona or '?'}",
               r.subject, split_quoted(r.text or "")[0][:160], f"/replies/{r.id}/",
               team=r.team, response=r, test=test)


def flag(a):
    urgent = [f for f in a.flags if f in ("escalate", "safety")] if a.status == "done" else []
    if not urgent:
        return None
    r = a.response
    what = "safety concern" if "safety" in urgent else "needs ExCon attention"
    return add(f"flag:{a.id}", "flag", f"⚑ {_team(r.team)}: {what}", a.summary, r.subject, f"/replies/{r.id}/",
               team=r.team, response=r, test=r.kind == "test")


def document(d):
    """A file a team sent that reads as likely AI-assisted: one notice per team per hour, so a
    batch of files (or turning the reading on mid-exercise) doesn't swamp the panel."""
    from .ai_docs import LIKELY
    if d.status != "done" or (d.ai_written or 0) < LIKELY:
        return None
    hour = timezone.localtime(d.updated_at or timezone.now()).strftime("%Y%m%d%H")
    return add(f"docai:{d.team_id}:{hour}", "flag", f"⚑ {_team(d.team)}: a document reads as likely AI-assisted",
               f"{d.filename}: {d.ai_signals}", d.summary, f"/documents/{d.id}/", team=d.team, response=d.response,
               test=d.kind == "test")


def inject(inj, status, error=""):
    """An inject that failed, partly failed or went overdue (once per attempt / due time)."""
    if status == "overdue":
        key = f"overdue:{inj.id}:{inj.send_at.isoformat() if inj.send_at else ''}"
        title = f"{inj.mel_id} is overdue"
        body = (f"It was due {timezone.localtime(inj.send_at):%a %d %b %H:%M} and was not sent. "
                if inj.send_at else "") + "Send it, reschedule it or cancel it."
        url = f"/injects/{inj.id}/"
    else:
        key = f"{status}:{inj.id}:a{inj.attempt}"
        title = f"{inj.mel_id} {'failed to deliver' if status == 'failed' else 'was only partly delivered'}"
        body, url = error or "", f"/injects/{inj.id}/"
    return add(key, "delivery", title, body, inj.subject or "", url, inject=inj)


def draft_done(d):
    if d.status != "done":
        return add(f"draft:{d.id}", "system", "An AI draft failed", d.error or "", d.prompt or "", f"/injects/drafts/{d.id}/")
    subject = (d.body or {}).get("subject") or ""
    return add(f"draft:{d.id}", "system", "AI draft ready", subject, d.prompt or "", f"/injects/drafts/{d.id}/")


def summary_done(rep):
    ok = rep.status == "done"
    return add(f"summary:{rep.id}", "system", "Exercise summary written" if ok else "The exercise summary failed",
               "" if ok else rep.error or "", url="/dashboard/")


def reports_done(ex):
    failed = ex.reports.filter(status="failed").count()
    return add(f"reports:{ex.id}", "system", "End-of-exercise reports written",
               f"{failed} could not be written: open them to write them again." if failed else "",
               url=f"/reports/{ex.id}/")


def attachments_done(job):
    if job.status == "done":
        return add(f"zip:{job.id}", "system", "Inject documents imported",
                   f"{job.filename}: {job.attached} attached" + (f", {len(job.unused)} unused" if job.unused else ""),
                   url="/import/")
    return add(f"zip:{job.id}", "system", "Inject documents import failed", f"{job.filename}: {job.error}", url="/import/")


def npc(job):
    """An AI persona's answer: sent, waiting for ExCon or failed; a flag it raised on top."""
    who, team = job.npc.sender.display_name or job.npc.sender.local_part, _team(job.team)
    test = bool(job.team and job.team.is_test)
    url, links = f"/npcs/{job.npc_id}/#q{job.id}", dict(team=job.team, response=job.response, test=test)
    if job.status == "failed":
        return add(f"npc:{job.id}:failed", "system", f"AI persona {who} could not answer {team}", job.error or "", url=url, **links)
    out = job.out or {}
    if "escalate" in out.get("flags", []):
        add(f"npc:{job.id}:flag", "flag", f"⚑ AI persona {who}: {team} needs ExCon attention", out.get("note", ""), url=url, **links)
    if job.status == "held":
        return add(f"npc:{job.id}:held", "system", f"AI persona {who}'s answer to {team} is waiting for you",
                   out.get("note", ""), job.body[:160], url, **links)
    if job.status == "sent":
        return add(f"npc:{job.id}:sent", "system", f"AI persona {who} answered {team}", out.get("note", ""), job.body[:160],
                   url, **links)
    return None


def desk_wait(thread, mail, minutes, owner=""):
    """A team has waited minutes for a persona's answer on the persona desk (mail: its first
    unanswered message there): once per message waiting."""
    from .desk import describe
    persona = describe(mail.persona)["name"] if mail.persona else "a persona"
    return add(f"desk-wait:{thread}:{mail.id}", "reply", f"{_team(mail.desk_team)} waiting {minutes} min for {persona}",
               mail.subject, f"Taken by {owner}, not answered yet." if owner else "Nobody on the persona desk has taken it.",
               f"/inbox/desk/t/{thread}/", team=mail.desk_team, test=mail.is_test)


def npc_ready(p):
    if p.gen_status == "done":
        return add(f"npc-new:{p.id}", "system", "AI persona written", p.sender.display_name, p.brief, f"/npcs/{p.id}/")
    return add(f"npc-new:{p.id}", "system", "Writing an AI persona failed", p.gen_error, p.brief, f"/npcs/{p.id}/")


def scheduler_check(tick):
    """Called from ExCon's poll: once per stop, the scheduler hasn't ticked for a while."""
    if tick and (timezone.now() - tick).total_seconds() > SCHEDULER_STALE:
        add(f"scheduler:{tick.isoformat()}", "system", "The scheduler has stopped",
            f"No tick since {timezone.localtime(tick):%H:%M}: nothing is sent on time until the worker runs again.",
            url="/settings/")


# --- the panel --------------------------------------------------------------------------------

def visible(user):
    """The notices this user has not dismissed."""
    return Notice.objects.exclude(id__in=NoticeState.objects.filter(user=user, dismissed_at__isnull=False).values("notice_id"))


def unread(user):
    return visible(user).exclude(id__in=NoticeState.objects.filter(user=user, read_at__isnull=False).values("notice_id")).count()


def version(user):
    """Changes whenever this user's panel would: a new, refreshed or deleted notice, a read or a
    dismissal (from any tab or device)."""
    n = Notice.objects.aggregate(t=Max("updated_at"), k=Max("id"))
    t = NoticeState.objects.filter(user=user).aggregate(t=Max("updated_at"))["t"]
    stamp = lambda d: f"{d.timestamp():.6f}" if d else "0"  # noqa: E731
    return f"{n['k'] or 0}.{Notice.objects.count()}.{stamp(n['t'])}.{stamp(t)}"


def listing(user):
    read = set(NoticeState.objects.filter(user=user, read_at__isnull=False).values_list("notice_id", flat=True))
    return [{"id": n.id, "kind": n.kind, "title": n.title, "body": n.body, "note": n.note, "test": n.test,
             "url": f"/excon/notices/{n.id}/" if n.url else "", "at": n.at.isoformat(), "read": n.id in read}
            for n in visible(user).order_by("-id")[:SHOWN]]


def _mark(user, qs, *fields):
    now = timezone.now()
    ids = list(qs.values_list("id", flat=True))
    have = set(NoticeState.objects.filter(user=user, notice_id__in=ids).values_list("notice_id", flat=True))
    NoticeState.objects.bulk_create([NoticeState(notice_id=i, user=user) for i in ids if i not in have], ignore_conflicts=True)
    for f in fields:
        NoticeState.objects.filter(user=user, notice_id__in=ids, **{f"{f}__isnull": True}).update(**{f: now, "updated_at": now})


def mark_read(user, qs):
    _mark(user, qs, "read_at")


def dismiss(user, qs):
    _mark(user, qs, "read_at", "dismissed_at")


def of_kind(user, kind):
    """What a panel filter shows (All: every kind)."""
    qs = visible(user)
    return qs.filter(kind=kind) if kind in KINDS else qs

"""Notes on mail, and SIMEX AI's reading of a conversation, beside it in SIMEX Mail (like the
comments in the margin of a document): for the persona desk, team leaders and ExCon, never
the teams.

- MailNote: a person's note on one email of a conversation ("Poorly structured, no body,
  unprofessional subject line"). Its scope says who reads it (access below): "desk" (written
  on the persona desk, by its members or ExCon), "leader" (a team leader's, in their own
  mailbox, the team inbox or a member's: that team's leader and ExCon) or "excon" (ExCon
  viewing a team's mailboxes at /inbox/as/: ExCon only). Notes lists: views_notes.py.
- ThreadReview: SIMEX AI's summary of one conversation in one mailbox to date, read again when
  a newer Mail lands in it, with a MailReview (good | fair | poor, a comment, short issues) of
  each email the team side wrote in it: on the desk, the teams' messages to the personas; in a
  team's mailboxes, those its members and leader wrote. Built from that mailbox's mail only
  (what the people reading it there already see): nothing control-side, never another
  mailbox, so a leader's reading holds only their team's mail.

Run by the scheduler's AI loop (ai.tick -> tick here), never in a request: automatically
(Settings → AI analysis "ai_auto") for the conversations with recent mail on the desk, in the
team inboxes and the leaders' mailboxes in which the team side wrote; any conversation when
someone who may see the reading opens it and it is out of date (ensure, from the thread view),
members' own mailboxes included. Settings "mail_review" (on); "ai_tests" for the Test team.
"""

import logging
from datetime import timedelta

from django.db.models import Count, F, Max, Q
from django.utils import timezone

from . import ai, conf
from .models import Mail, MailNote, MailReview, ThreadReview

log = logging.getLogger("simex.ai")

SCOPES = {"desk": "Persona desk", "leader": "Team leader", "excon": "ExCon"}
VERDICTS = {"good": "Good", "fair": "Fair", "poor": "Poor"}
NOTE_MAX = 4000  # characters of a note
BODY_MAX, MESSAGES_MAX, ASSESS_MAX = 2500, 16, 10
AUTO_WINDOW = timedelta(hours=6)  # automatic readings: conversations with mail this recent


# --- who reads and writes what ---------------------------------------------------------------------

class Access:
    """write: the scope of the notes this viewer writes here; read: the scopes shown here."""

    def __init__(self, write, read, user):
        self.write, self.read, self.user = write, read, user

    def can_edit(self, note):
        return note.user_id is not None and note.user_id == self.user.id

    def can_delete(self, note):
        from .access import is_excon
        return self.can_edit(note) or is_excon(self.user)


def access(v):
    """The notes and readings a views_inbox.Viewer may see (and write) beside its conversations,
    or None: the desk (its members and ExCon on it), a team leader in their own mailbox, their
    team's inbox and their members' (never another leader's), and ExCon viewing a team's
    mailboxes. Never a member, never the desk reading a team's mailboxes."""
    from .access import is_excon
    if v.peer:
        return None
    if v.desk:
        return Access("desk", ("desk",), v.user)
    if v.leader:  # their own mailbox, the team inbox, a member's (read-only): their team only
        return Access("leader", ("leader",), v.user)
    if v.base.startswith("/inbox/as/") and is_excon(v.user):
        return Access("excon", ("leader", "excon"), v.user)
    return None


def enabled():
    return ai.enabled() and conf.flag("mail_review")


def box_key(team_id, owner_id):
    return "desk" if team_id is None else f"{team_id}" if owner_id is None else f"{team_id}:{owner_id}"


# --- a conversation in one mailbox -------------------------------------------------------------------

def assessable(m, team_id):
    """An email the team side wrote: on the desk (team_id None), a team's message to a persona
    (not persona mail, not a calendar answer); in a team's mailbox, one its member or leader wrote."""
    if m.invite:
        return False
    if team_id is None:
        return m.folder == "inbox" and bool(m.desk_team_id) and not (m.recipients or {}).get("from")
    a = m.author if m.author_id else None
    return bool(a and a.team_id == team_id and a.role in ("member", "tl"))


def box_mails(team_id, owner_id, thread):
    """The conversation as its mailbox shows it, oldest first: no drafts, nothing in the trash,
    and on the desk persona mail once (its Inbox copy, as views_inbox.one_copy)."""
    mails = list(Mail.objects.filter(team_id=team_id, owner_id=owner_id, thread=thread, trashed_at__isnull=True)
                 .exclude(folder="drafts").select_related("author").prefetch_related("files").order_by("created_at", "id"))
    if team_id is None:
        got = {m.message_id for m in mails if m.folder == "inbox" and m.message_id}
        mails = [m for m in mails if not (m.folder == "sent" and m.message_id in got)]
    return mails


def review_of(team_id, owner_id, thread):
    return ThreadReview.objects.filter(box=box_key(team_id, owner_id), thread=thread).first()


def _latest(team_id, owner_id, thread):
    return (Mail.objects.filter(team_id=team_id, owner_id=owner_id, thread=thread, trashed_at__isnull=True)
            .exclude(folder="drafts").aggregate(x=Max("id"))["x"] or 0)


def ensure(team_id, owner_id, thread, mails, force=False):
    """Someone who may see the reading opened the conversation: queue it if it is out of date
    (a newer mail than it read) and the team side wrote in it. force: read it again anyway
    (Read again). Returns the ThreadReview, if any."""
    tr = review_of(team_id, owner_id, thread)
    if not enabled():
        return tr
    if tr and tr.status == "queued":
        return tr
    if not force:
        if tr and _latest(team_id, owner_id, thread) <= tr.upto:
            return tr
        if not any(assessable(m, team_id) for m in mails):
            return tr
    tr, _ = ThreadReview.objects.get_or_create(box=box_key(team_id, owner_id), thread=thread,
                                               defaults={"team_id": team_id, "owner_id": owner_id})
    tr.status = "queued"
    tr.save()
    return tr


# --- the model's reading -----------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "open": {"type": "array", "items": {"type": "string"}},
        "assessments": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "ref": {"type": "integer"},
                "verdict": {"type": "string", "enum": ["good", "fair", "poor"]},
                "assessment": {"type": "string"},
                "issues": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ref", "verdict", "assessment", "issues"],
            "additionalProperties": False,
        }},
    },
    "required": ["summary", "open", "assessments"],
    "additionalProperties": False,
}

RULES = """You read one email conversation from an exercise mailbox, for the people reviewing how the team \
communicates (Exercise Control and the team's leader). Messages are numbered [1], [2]...; those marked ASSESS \
were written by the team (its members or its leader).
- summary: the conversation to date in at most 80 words: who wrote to whom, what was asked, sent, agreed or \
decided, and where it stands now.
- open: what is still open (unanswered questions, requests, promised actions), at most 4 items of at most 12 \
words; empty if nothing is.
- assessments: one for each message marked ASSESS, and only those (ref = its number). Judge it as professional \
email from a humanitarian coordination team: a clear, specific subject line; a body that says why it is \
written, with the request, decision or information up front; structure (greeting, short paragraphs or lists, \
sign-off with name and role); attachments introduced and explained; clear asks with who and by when; tone and \
courtesy right for the recipient; consistent with the rest of the conversation; sent to the right people.
  - verdict: "good" (clear and professional), "fair" (does the job but with weaknesses), "poor" (unclear, \
incomplete or unprofessional).
  - assessment: one or two direct sentences, like a reviewer's comment in the margin, e.g. "Poorly structured \
email: no body text, attachments forwarded without explanation, unprofessional subject line." Say what works too \
when it does.
  - issues: at most 4 short tags of at most 5 words (e.g. "No body text", "Vague subject line", "Attachments not \
explained", "No clear ask"); empty when there are none.
Write in English. The scenario is fictional: judge the communication, never the scenario facts, and never invent \
what was said."""


def _who(m, team_id, teams):
    if team_id is None:  # the desk: a team's message to a persona, or a persona's
        team = teams.get(m.desk_team_id, "a team")
        if m.folder == "inbox" and m.desk_team_id and not (m.recipients or {}).get("from"):
            return f"written by {team} (the team) to the persona {m.persona or '?'}"
        return f"written by a persona (Exercise Control) to {team}" if m.desk_team_id else "persona to persona"
    a = m.author if m.author_id else None
    if a and a.team_id == team_id and a.role in ("member", "tl"):
        return f"written by {a.name} ({'the team leader' if a.role == 'tl' else 'a team member'})"
    return "from an exercise persona or Exercise Control"


def conversation(tr, mails, assess):
    """The text the model reads: the latest messages (and every one to assess), oldest first,
    with the earlier reading's summary when some are left out."""
    from .models import Team
    teams = dict(Team.objects.values_list("id", "name"))
    keep = {m.id for m in mails[-MESSAGES_MAX:]} | {m.id for m in assess}
    shown = [m for m in mails if m.id in keep]
    refs, parts = {}, []
    if len(shown) < len(mails):
        parts.append(f"({len(mails) - len(shown)} earlier messages are not shown."
                     + (f" The earlier summary: {tr.summary}" if tr.summary else "") + ")")
    want = {m.id for m in assess}
    for n, m in enumerate(shown, 1):
        refs[n] = m
        at = timezone.localtime(m.sim_at or m.created_at).strftime("%a %d %b %H:%M")
        body = (m.body or "").strip()
        if len(body) > BODY_MAX:
            body = body[:BODY_MAX] + " [...]"
        files = ", ".join(f.filename for f in m.files.all() if not f.cid)
        to = m.to_label + (f"; cc {m.cc_label}" if m.cc_label else "")
        parts.append(f"--- [{n}]{' ASSESS' if m.id in want else ''} {_who(m, tr.team_id, teams)}, {at}\n"
                     f"From: {m.from_name} <{m.from_addr}>\nTo: {to or '?'}\nSubject: {m.subject or '(no subject)'}\n"
                     f"{body or '(no body text)'}" + (f"\n[Attached: {files}]" if files else ""))
    return "\n\n".join(parts), refs


def review(tr):
    """Read one conversation (its new emails assessed); stores the reading on tr."""
    mails = box_mails(tr.team_id, tr.owner_id, tr.thread)
    if not mails:
        tr.delete()
        return None
    done = set(MailReview.objects.filter(mail__in=mails).values_list("mail_id", flat=True))
    assess = [m for m in mails if assessable(m, tr.team_id) and m.id not in done][-ASSESS_MAX:]
    text, refs = conversation(tr, mails, assess)
    latest = max(m.id for m in mails)
    content = [{"type": "text", "text": "THE CONVERSATION (the latest message last):\n\n" + text}]
    try:
        out, model = ai.call("review", f"r{tr.id}", "fast", ai.system("review"), content, SCHEMA, "low", 4000)
    except ai.AiError as e:
        tr.status, tr.error, tr.upto, tr.attempts = "failed", str(e)[:500], latest, tr.attempts + 1
        tr.save()
        return tr
    want = {m.id for m in assess}
    for a in out.get("assessments") or []:
        m = refs.get(a.get("ref"))
        if m is None or m.id not in want:
            continue
        MailReview.objects.update_or_create(mail=m, defaults={
            "verdict": a.get("verdict") if a.get("verdict") in VERDICTS else "fair",
            "assessment": (a.get("assessment") or "")[:1000],
            "issues": [str(x)[:60] for x in (a.get("issues") or [])][:4]})
    tr.summary = (out.get("summary") or "")[:1500]
    tr.open_points = [str(x)[:120] for x in (out.get("open") or [])][:4]
    tr.status, tr.error, tr.upto, tr.seen, tr.model, tr.read_at = "done", "", latest, len(mails), model, timezone.now()
    tr.save()
    return tr


def _skip_tests(qs):
    if conf.flag("ai_tests"):
        return qs
    return qs.exclude(is_test=True).exclude(team__is_test=True).exclude(desk_team__is_test=True)


def discover():
    """Automatic readings: conversations with mail in the last AUTO_WINDOW on the desk, in a
    team inbox or a team leader's mailbox, newer than their reading, in which the team side
    wrote (recently, or a reading exists), oldest first."""
    since = timezone.now() - AUTO_WINDOW
    qs = _skip_tests(Mail.objects.filter(created_at__gte=since, trashed_at__isnull=True).exclude(folder="drafts")
                     .filter(Q(owner=None) | Q(owner__role="tl")))
    latest, wrote = {}, set()
    for m in qs.select_related("author").only("id", "team_id", "owner_id", "thread", "folder", "desk_team_id", "invite",
                                             "recipients", "author__team_id", "author__role").order_by("id"):
        k = (m.team_id, m.owner_id, m.thread)
        latest[k] = m.id
        if assessable(m, m.team_id):
            wrote.add(k)
    if not latest:
        return []
    have = {(r.box, r.thread): r for r in ThreadReview.objects.filter(thread__in={k[2] for k in latest})}
    out = []
    for k, mid in sorted(latest.items(), key=lambda x: x[1]):
        tr = have.get((box_key(k[0], k[1]), k[2]))
        if tr is None and k not in wrote:
            continue
        if tr and (tr.status == "queued" or mid <= tr.upto):
            continue
        out.append(tr or ThreadReview(box=box_key(k[0], k[1]), team_id=k[0], owner_id=k[1], thread=k[2]))
    return out


def pending(limit):
    """Conversations to read: those opened out of date first, then (automatic mode) discover()."""
    out = list(ThreadReview.objects.filter(status="queued").order_by("updated_at")[:limit])
    if len(out) < limit and conf.flag("ai_auto"):
        out += discover()[:limit - len(out)]
    return out


def tick(limit):
    """Called from ai.tick: reads up to limit conversations. Returns the number of model calls."""
    if not enabled() or limit <= 0:
        return 0
    n = 0
    for tr in pending(limit):
        if not ai.under_cap():
            break
        if tr.pk is None:
            tr, _ = ThreadReview.objects.get_or_create(box=tr.box, thread=tr.thread,
                                                       defaults={"team_id": tr.team_id, "owner_id": tr.owner_id})
        try:
            review(tr)
        except Exception as e:  # one bad conversation never holds up the rest
            log.exception("conversation reading failed")
            ThreadReview.objects.filter(pk=tr.pk).update(
                status="failed", error=f"{type(e).__name__}: {e}"[:500], attempts=F("attempts") + 1,
                upto=_latest(tr.team_id, tr.owner_id, tr.thread))
        n += 1
    return n


def prune():
    """After clearing or withdrawing mail: readings of conversations no longer in their mailbox
    go, and those that lost a message are read again when next opened or written in."""
    left = {(r["team_id"], r["owner_id"], r["thread"]): r["n"] for r in
            Mail.objects.filter(trashed_at__isnull=True).exclude(folder="drafts").order_by()
            .values("team_id", "owner_id", "thread").annotate(n=Count("id"))}
    gone, again = [], []
    for tr in ThreadReview.objects.only("id", "team_id", "owner_id", "thread", "seen"):
        n = left.get((tr.team_id, tr.owner_id, tr.thread))
        if n is None:
            gone.append(tr.id)
        elif n < tr.seen:
            again.append(tr.id)
    ThreadReview.objects.filter(id__in=gone).delete()
    ThreadReview.objects.filter(id__in=again).update(upto=0, status="")


# --- what the thread view shows beside the conversation ------------------------------------------------

def panel(v, acc, thread, mails):
    """The side panel of a conversation: SIMEX AI's summary, and for each email (as shown, mails)
    its assessment and the notes this viewer may read."""
    ids = [m.id for m in mails]
    notes = {}
    for n in MailNote.objects.filter(scope__in=acc.read, team=v.team, owner=v.owner, thread=thread, mail_id__in=ids):
        n.editable, n.deletable = acc.can_edit(n), acc.can_delete(n)
        n.scope_name = SCOPES.get(n.scope, n.scope)
        notes.setdefault(n.mail_id, []).append(n)
    reviews = {r.mail_id: r for r in MailReview.objects.filter(mail_id__in=ids)}
    tr = review_of(v.team.id if v.team else None, v.owner.id if v.owner else None, thread)
    team_id = v.team.id if v.team else None
    groups = [{"m": m, "ai": reviews.get(m.id), "notes": notes.get(m.id, []), "team_side": assessable(m, team_id)}
              for m in mails]
    for g in groups:  # the count on each email's note button
        g["m"].nt = len(g["notes"]) + (1 if g["ai"] else 0)
    latest = max(ids) if ids else 0
    on = enabled()
    return {"review": tr, "groups": groups, "on": on,
            "pending": bool(on and tr and tr.status == "queued"),
            "stale": bool(tr and tr.status == "done" and latest > tr.upto),
            "n_notes": sum(len(x) for x in notes.values()), "n_ai": len(reviews), "write": acc.write,
            "list_url": list_url(v)}


def list_url(v):
    if v.desk:
        return "/inbox/desk/notes/"
    if v.leader:
        return "/inbox/notes/"
    return f"/notes/?team={v.team.id}" if v.team else "/notes/"


def author_name(user):
    from . import desk
    return desk.user_name(user)[:150]


# --- the lists of notes ------------------------------------------------------------------------------

def mail_url(m, who):
    """Where a mail opens for the list's reader: who "desk" | "leader" | "excon"."""
    if m.team_id is None:
        base = "/inbox/desk"
    elif who == "leader":
        base = "/inbox/team" if m.owner_id is None else "/inbox" if m.owner.role == "tl" else f"/inbox/members/{m.owner_id}"
    else:
        base = f"/inbox/as/{m.team_id}" + ("" if m.owner_id is None else "/leader" if m.owner.role == "tl"
                                           else f"/member/{m.owner_id}")
    return f"{base}/t/{m.thread}/#msg-{m.id}"


def box_name(m):
    if m.team_id is None:
        return "Persona desk"
    if m.owner_id is None:
        return f"{m.team.name} inbox"
    return f"{m.team.name} leader" if m.owner.role == "tl" else f"{m.owner.name}'s mail"


def entries(who, team=None, scopes=None, what="all", verdict="", q="", mine=None, limit=None):
    """Notes and SIMEX AI's assessments, newest first, as dicts for the lists. who: the list's
    reader ("desk": the desk's notes and its mailbox's assessments; "leader": a team's leader
    notes and its mailboxes' assessments, team required; "excon": everything). team: only
    about that team; scopes: (ExCon) only those; mine: a User, only their notes."""
    notes = MailNote.objects.select_related("mail", "mail__team", "mail__owner", "about_team")
    revs = MailReview.objects.select_related("mail", "mail__team", "mail__owner", "mail__desk_team")
    if who == "desk":
        notes, revs = notes.filter(scope="desk"), revs.filter(mail__team=None)
    elif who == "leader":
        notes, revs = notes.filter(scope="leader", team=team), revs.filter(mail__team=team)
    elif scopes:
        notes = notes.filter(scope__in=scopes)
    if team is not None and who != "leader":
        notes = notes.filter(about_team=team)
        revs = revs.filter(Q(mail__team=team) | Q(mail__team=None, mail__desk_team=team))
    if q:
        notes = notes.filter(Q(body__icontains=q) | Q(mail__subject__icontains=q) | Q(author_name__icontains=q))
        revs = revs.filter(Q(assessment__icontains=q) | Q(mail__subject__icontains=q))
    if verdict:
        revs = revs.filter(verdict=verdict)
        notes = notes.none()
    if mine is not None:
        notes, revs = notes.filter(user=mine), revs.none()
    out = []
    if what in ("all", "notes"):
        for n in notes.order_by("-created_at")[:limit]:
            m = n.mail
            out.append({"kind": "note", "note_id": n.id, "at": n.created_at, "text": n.body, "by": n.author_name,
                        "scope": SCOPES.get(n.scope, n.scope), "edited": n.edited_at, "verdict": "", "issues": [],
                        **_mail_cols(m, who, n.about_team)})
    if what in ("all", "ai"):
        for r in revs.order_by("-created_at")[:limit]:
            m = r.mail
            out.append({"kind": "ai", "at": r.created_at, "text": r.assessment, "by": "SIMEX AI", "scope": "",
                        "edited": None, "verdict": r.verdict, "verdict_name": VERDICTS.get(r.verdict, r.verdict),
                        "issues": r.issues, **_mail_cols(m, who, m.desk_team if m.team_id is None else m.team)})
    out.sort(key=lambda e: e["at"], reverse=True)
    return out[:limit] if limit else out


def _mail_cols(m, who, about):
    return {"team": about.name if about else "", "subject": m.subject or "(no subject)", "from": m.from_name or m.from_addr,
            "mail_at": m.created_at, "url": mail_url(m, who), "box": box_name(m), "thread": m.thread, "mail_id": m.id}


# --- Markdown exports (for reading, or to give another AI to analyse) -----------------------------------

TEXT_MAX = 6000  # characters of an email's text in an export


def _when(dt):
    return timezone.localtime(dt).strftime("%a %d %b %Y %H:%M") if dt else ""


def _quote(text):
    text = (text or "").strip()
    if len(text) > TEXT_MAX:
        text = text[:TEXT_MAX] + " [...]"
    return "\n".join("> " + line for line in text.split("\n")) if text else "> (no body text)"


def _mail_md(m, review=None, notes=(), body=True):
    files = ", ".join(f.filename for f in m.files.all() if not f.cid)
    sim = f" (scenario time {_when(m.sim_at)})" if m.sim_at and m.sim_at != m.created_at else ""
    out = [f"### {m.from_name or m.from_addr} <{m.from_addr}>, {_when(m.created_at)}{sim}",
           f"To: {m.to_label or '?'}" + (f"; cc {m.cc_label}" if m.cc_label else ""), f"Subject: {m.subject or '(no subject)'}"]
    if files:
        out.append(f"Attachments: {files}")
    if body:
        out += ["", _quote(m.body)]
    if review:
        out += ["", f"**SIMEX AI assessment: {VERDICTS.get(review.verdict, review.verdict)}.** {review.assessment}"]
        if review.issues:
            out.append("Issues: " + "; ".join(review.issues))
    for n in notes:
        out += ["", f"**Note by {n.author_name}** ({SCOPES.get(n.scope, n.scope)}, {_when(n.created_at)}"
                    f"{', edited' if n.edited_at else ''}):", "", n.body.strip()]
    return "\n".join(out)


def thread_markdown(v, acc, thread, mails):
    """One conversation as Markdown: SIMEX AI's summary, then every email (its text, its
    assessment, the notes this viewer may read), oldest first."""
    p = panel(v, acc, thread, mails)
    tr = p["review"]
    where = "Persona desk" if v.desk else box_name(mails[0]) if mails else ""
    out = [f"# {mails[0].subject or '(no subject)'}" if mails else "# Conversation",
           f"Mailbox: {where} · {len(mails)} emails · exported {_when(timezone.now())}", ""]
    if tr and tr.summary:
        out += ["## SIMEX AI: the conversation so far", "", tr.summary]
        if tr.open_points:
            out += ["", "Still open:"] + [f"- {o}" for o in tr.open_points]
        out.append("")
    out.append("## Emails")
    for g in p["groups"]:
        out += ["", _mail_md(g["m"], g["ai"], g["notes"])]
    return "\n".join(out) + "\n"


def list_markdown(rows, title):
    """A list's notes and assessments as Markdown, by conversation (latest first), each email
    once with its text, assessment and notes."""
    by_mail = {}
    for e in rows:
        by_mail.setdefault(e["mail_id"], []).append(e)
    mails = {m.id: m for m in Mail.objects.filter(id__in=by_mail).select_related("team", "owner", "desk_team")
             .prefetch_related("files")}
    reviews = {r.mail_id: r for r in MailReview.objects.filter(mail_id__in=by_mail)}
    want_notes = {e["note_id"] for e in rows if e["kind"] == "note"}
    notes = {}
    for n in MailNote.objects.filter(id__in=want_notes).order_by("created_at", "id"):
        notes.setdefault(n.mail_id, []).append(n)
    show_ai = {e["mail_id"] for e in rows if e["kind"] == "ai"}
    convs = {}
    for mid in by_mail:
        m = mails.get(mid)
        if m:
            convs.setdefault((m.team_id, m.owner_id, m.thread), []).append(m)
    latest = {k: max(by_mail[m.id][0]["at"] for m in ms) for k, ms in convs.items()}
    out = [f"# {title}", f"{len(rows)} entries in {len(convs)} conversations · exported {_when(timezone.now())}"]
    for k in sorted(convs, key=lambda k: latest[k], reverse=True):
        ms = sorted(convs[k], key=lambda m: (m.created_at, m.id))
        m0 = ms[0]
        about = m0.desk_team if m0.team_id is None else m0.team
        tr = ThreadReview.objects.filter(box=box_key(k[0], k[1]), thread=k[2]).first()
        out += ["", f"## {m0.subject or '(no subject)'}", f"Mailbox: {box_name(m0)}" + (f" · team: {about.name}" if about else "")]
        if tr and tr.summary:
            out += ["", f"SIMEX AI summary: {tr.summary}"]
        for m in ms:
            out += ["", _mail_md(m, reviews.get(m.id) if m.id in show_ai else None, notes.get(m.id, []))]
    return "\n".join(out) + "\n"


ai.INSTRUCTIONS["review"] = {
    "label": "Conversations: summary and each email's assessment",
    "default": RULES,
    "help": "Reads each conversation on the persona desk and in the team leaders' mailboxes: a summary to date, what "
            "is still open, and an assessment of each email the team wrote (shown beside it, with people's notes)."}

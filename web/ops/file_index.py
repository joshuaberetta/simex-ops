"""Files: every file sent in the exercise in one list (web/docs/MESSENGER_PLAN.md, "The Files
view"), computed from what is kept, nothing stored: what teams attached in SIMEX Mail, the files
of emails teams sent in, what the desk or ExCon attached to an answer, inject documents and
SIMEX Messenger's files. Grouped, filtered (in the URL only) and linked back to the conversation
each came from.

Three readers (who): "desk" (the persona desk and ExCon there: every team's files, persona-facing
and team-internal, as the desk reads the teams' mailboxes; never SIMEX AI's readings), "leader"
(one team's, with SIMEX AI's document readings) and "excon" (everything, readings included).
A copy of one file in several mailboxes of a conversation (the team inbox, members' copies, the
leader's Sent, the desk's copy) is one row, "in n mailboxes"."""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from . import ai_docs
from .models import DocumentAnalysis, Mail, MailAttachment, MessageAttachment, ResponseAttachment, Team

WHO = ("desk", "leader", "excon")
INJECT_KINDS = ("live", "test", "oneoff", "followup")
DIRECTIONS = {"to_persona": "Team → persona", "from_persona": "Persona → team", "internal": "Within the team",
              "to_team": "Team → team", "inject": "Inject document"}
SOURCES = {"mail": "SIMEX Mail", "email": "Email", "chat": "Messenger"}
TYPES = {"pdf": "PDF", "word": "Word", "excel": "Excel", "slides": "Slides", "image": "Picture", "text": "Text",
         "other": "Other"}
GROUPS = {"team": "Team", "conversation": "Conversation", "sender": "Sender", "inject": "Inject", "file": "File",
          "day": "Day", "none": "No grouping"}
AI_STATES = {"done": "Read by SIMEX AI", "queued": "Waiting to be read", "failed": "Failed", "skipped": "Not readable",
             "none": "Not read"}
EXT = {"pdf": "pdf", "doc": "word", "docx": "word", "odt": "word", "rtf": "word", "xls": "excel", "xlsx": "excel",
       "csv": "excel", "ods": "excel", "ppt": "slides", "pptx": "slides", "odp": "slides", "png": "image",
       "jpg": "image", "jpeg": "image", "gif": "image", "webp": "image", "heic": "image", "txt": "text", "md": "text"}
PAGE = 100
SCAN = 20000  # files read at most per source (newest first)


def file_type(name, ctype=""):
    ext = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    if ext in EXT:
        return EXT[ext]
    if (ctype or "").startswith("image/"):
        return "image"
    return "pdf" if ctype == "application/pdf" else "other"


@dataclass
class Filters:
    team: object = None
    direction: str = ""
    source: str = ""
    type: str = ""
    sender: str = ""  # a row's sender key: m:<pid> | l:<pid> | p:<username> | x (ExCon)
    inject: int = 0
    since: object = None  # dates, exercise timezone
    until: object = None
    ai: str = ""
    band: str = ""  # likely | possibly | low
    q: str = ""
    test: bool = False
    injects: bool = False  # show inject documents (each goes to every team)
    group: str = "team"
    page: int = 1
    errors: list = field(default_factory=list)

    def qs(self, **override):
        """The query string of these filters (paging and grouping links keep them)."""
        from urllib.parse import urlencode
        d = {"team": self.team.id if self.team else "", "dir": self.direction, "src": self.source, "type": self.type,
             "from": self.sender, "inject": self.inject or "", "since": self.since.isoformat() if self.since else "",
             "until": self.until.isoformat() if self.until else "", "ai": self.ai, "band": self.band, "q": self.q,
             "test": "1" if self.test else "", "injects": "1" if self.injects else "",
             "group": "" if self.group == "team" else self.group}
        d.update(override)
        return urlencode({k: v for k, v in d.items() if v not in ("", None, 0)})


def _date(s):
    try:
        return datetime.strptime((s or "").strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def filters(GET, teams):
    f = Filters()
    f.team = next((t for t in teams if str(t.id) == GET.get("team", "")), None)
    f.direction = GET.get("dir") if GET.get("dir") in DIRECTIONS else ""
    f.source = GET.get("src") if GET.get("src") in SOURCES else ""
    f.type = GET.get("type") if GET.get("type") in TYPES else ""
    f.sender = (GET.get("from") or "")[:130]
    f.inject = int(GET.get("inject")) if (GET.get("inject") or "").isdigit() else 0
    f.since, f.until = _date(GET.get("since")), _date(GET.get("until"))
    f.ai = GET.get("ai") if GET.get("ai") in AI_STATES else ""
    f.band = GET.get("band") if GET.get("band") in ("likely", "possibly", "low") else ""
    f.q = (GET.get("q") or "").strip()[:100]
    f.test = GET.get("test") == "1"
    f.injects = GET.get("injects") == "1" or f.direction == "inject"
    f.group = GET.get("group") if GET.get("group") in GROUPS else "team"
    f.page = max(1, int(GET.get("page"))) if (GET.get("page") or "").isdigit() else 1
    return f


# --- the rows ----------------------------------------------------------------------------------------

class Ctx:
    """What the rows are named and linked with, read once."""

    def __init__(self, who, team=None):
        from . import desk, render
        self.who, self.team = who, team
        self.teams = {t.id: t for t in Team.objects.all()}
        self.memo = {}
        self._desk, self._render = desk, render

    def persona(self, local, team_id=None):
        if not local:
            return "", ""
        d = self._desk.describe(local, self.teams.get(team_id), self.memo)
        return d["name"], d.get("colour", "")

    def is_leader_persona(self, local):
        return bool(local) and self._desk.is_leader_persona(local)


def _person_label(p, ctx):
    if p is None:
        return ""
    if p.role == "tl":
        return f"{p.name} (team leader)" if ctx.who != "leader" else "You (team leader)"
    return p.name


def _mail_box(m, ctx):
    if m.team_id is None:
        return "Persona desk"
    if m.owner_id is None:
        return f"{ctx.teams[m.team_id].name} inbox"
    return f"{m.owner.name}'s mail" if m.owner.role != "tl" else f"{ctx.teams[m.team_id].name} leader"


def _mail_url(m, ctx):
    from .mail_notes import mail_url
    return mail_url(m, "leader" if ctx.who == "leader" else "excon")


def _mail_rows(ctx, f):
    qs = (MailAttachment.objects.filter(cid="").filter(Q(data__isnull=False) | Q(attachment__isnull=False))
          .exclude(mail__folder="drafts").filter(mail__trashed_at__isnull=True)
          .select_related("mail", "mail__author", "mail__owner", "mail__message", "mail__inject").defer("data", "mail__body",
                                                                                                         "mail__html"))
    if ctx.who == "leader":
        qs = qs.filter(mail__team=ctx.team)
    elif f.team:
        qs = qs.filter(Q(mail__team=f.team) | Q(mail__team__isnull=True, mail__desk_team=f.team))
    if not f.injects:
        qs = qs.filter(attachment__isnull=True)
    if not f.test:
        qs = qs.filter(mail__is_test=False).exclude(mail__team__is_test=True).exclude(mail__desk_team__is_test=True)
    for a in qs.order_by("-id")[:SCAN]:
        m = a.mail
        team_id = m.team_id if m.team_id else m.desk_team_id
        if not team_id:
            continue  # persona mail between personas only
        author = m.author
        team_side = bool(author and author.role in ("member", "tl") and author.team_id)
        kind = m.message.kind if m.message_id else ""
        if a.attachment_id and kind in INJECT_KINDS:
            direction = "inject"
        elif m.team_id is None:  # the desk's copy: its Inbox is what a team wrote, its Sent what a persona sent
            direction = "to_persona" if m.folder == "inbox" else "from_persona"
        elif team_side:
            if m.to_team_id and m.to_team_id != m.team_id:
                direction = "to_team"
            elif m.persona and not ctx.is_leader_persona(m.persona):
                direction = "to_persona"
            else:
                direction = "internal"
        elif m.to_team_id and m.folder == "inbox" and kind == "":
            direction = "to_team"  # another team's mail landing here
        else:
            direction = "from_persona"
        if direction == "from_persona" and m.message_id and m.message.author_id and \
                m.message.author_id == getattr(author, "id", None) and team_side:
            direction = "internal"  # the leader writing as the team leader persona
        if direction in ("from_persona", "inject"):
            name, colour = ctx.persona(m.persona or (m.message.persona if m.message_id else ""), team_id)
            if m.message_id and m.message.persona and ctx.is_leader_persona(m.message.persona):
                direction = "internal"
            sender_key = f"p:{m.persona or (m.message.persona if m.message_id else '')}"
            sender = name or m.from_name or "Persona"
        else:
            colour = ""
            sender_key = (f"l:{author.id}" if author.role == "tl" else f"m:{author.id}") if author else ""
            sender = _person_label(author, ctx) or m.from_name or m.from_addr
        content = f"a{a.attachment_id}" if a.attachment_id else (a.sha256 or f"m{a.id}")
        yield {
            "group_key": (team_id, m.thread, content), "src": "m", "fid": a.id, "source": "mail",
            "filename": a.filename, "content_type": a.content_type, "size": a.size, "sha": a.sha256,
            "type": file_type(a.filename, a.content_type), "team_id": team_id, "direction": direction,
            "sender": sender, "sender_key": sender_key, "colour": colour,
            "to": m.to_label[:200], "at": m.created_at, "sim_at": m.sim_at, "inject_id": m.inject_id,
            "inject": m.inject.mel_id if m.inject_id else "", "subject": m.subject[:200],
            "box": _mail_box(m, ctx), "url": _mail_url(m, ctx), "thread": f"m:{m.thread}",
            "desk_copy": m.team_id is None, "is_inject_doc": bool(a.attachment_id)}


def _email_rows(ctx, f):
    """Files of emails teams sent in (the email channel), with the desk's copy as the link."""
    qs = (ResponseAttachment.objects.filter(response__team__isnull=False, response__fetched=True)
          .exclude(response__status="ignored").select_related("response", "response__person", "response__inject")
          .defer("data", "response__text", "response__html", "response__headers"))
    if ctx.who == "leader":
        qs = qs.filter(response__team=ctx.team)
    elif f.team:
        qs = qs.filter(response__team=f.team)
    if not f.test:
        qs = qs.filter(response__kind="live").exclude(response__team__is_test=True)
    rids = set()
    rows = list(qs.order_by("-id")[:SCAN])
    for a in rows:
        rids.add(a.response_id)
    desk = dict(Mail.objects.filter(team=None, desk_response_id__in=rids).values_list("desk_response_id", "thread"))
    for a in rows:
        r = a.response
        p = r.person
        name, _ = ctx.persona(r.persona, r.team_id)
        if ctx.who == "excon":
            url = f"/replies/{r.id}/"
        elif ctx.who == "desk" and r.id in desk:
            url = f"/inbox/desk/t/{desk[r.id]}/"
        else:
            url = ""
        yield {
            "group_key": ("r", a.id), "src": "r", "fid": a.id, "source": "email", "filename": a.filename,
            "content_type": a.content_type, "size": a.size, "sha": a.sha256, "stored": a.data is not None,
            "type": file_type(a.filename, a.content_type), "team_id": r.team_id, "direction": "to_persona",
            "sender": _person_label(p, ctx) or r.from_name or r.from_addr,
            "sender_key": (f"l:{p.id}" if p.role == "tl" else f"m:{p.id}") if p else "", "colour": "",
            "to": name, "at": r.received_at, "sim_at": None, "inject_id": r.inject_id,
            "inject": r.inject.mel_id if r.inject_id else "", "subject": r.subject[:200], "box": "Email",
            "url": url, "thread": f"r:{r.id}", "desk_copy": False, "is_inject_doc": False}


def _answer_rows(ctx, f):
    """Files the desk or ExCon attached to an answer sent by email (in-app, its Mail copies have them)."""
    qs = (MessageAttachment.objects.filter(cid="", message__channel="email", message__team__isnull=False)
          .exclude(message__status__in=["withdrawn", "failed"]).select_related("message", "message__inject")
          .defer("data", "message__text", "message__html", "message__body", "message__body_html"))
    if ctx.who == "leader":
        qs = qs.filter(message__team=ctx.team)
    elif f.team:
        qs = qs.filter(message__team=f.team)
    if not f.test:
        qs = qs.exclude(message__kind__in=["test", "oneoff"]).exclude(message__team__is_test=True)
    for a in qs.order_by("-id")[:SCAN]:
        m = a.message
        name, colour = ctx.persona(m.persona, m.team_id)
        yield {
            "group_key": ("x", a.id), "src": "x", "fid": a.id, "source": "email", "filename": a.filename,
            "content_type": a.content_type, "size": a.size, "sha": "", "type": file_type(a.filename, a.content_type),
            "team_id": m.team_id, "direction": "from_persona", "sender": name or m.from_addr,
            "sender_key": f"p:{m.persona}", "colour": colour, "to": ", ".join(m.to or [])[:200] if ctx.who == "excon" else "",
            "at": m.sent_at or m.created_at, "sim_at": None, "inject_id": m.inject_id,
            "inject": m.inject.mel_id if m.inject_id else "", "subject": m.subject[:200], "box": "Email",
            "url": f"/log/{m.id}/" if ctx.who == "excon" else "", "thread": f"x:{m.id}", "desk_copy": False,
            "is_inject_doc": False}


SOURCES_FNS = [_mail_rows, _email_rows, _answer_rows]  # Messenger adds its own (ops/messenger.py)


def register(fn):
    if fn not in SOURCES_FNS:
        SOURCES_FNS.append(fn)


def _merge(rows):
    """One row per file and conversation: the copies of a Mail's file in several mailboxes are one."""
    out = {}
    for r in rows:
        k = r["group_key"]
        have = out.get(k)
        if have is None:
            r["boxes"] = [r["box"]]
            out[k] = r
            continue
        if r["box"] not in have["boxes"]:
            have["boxes"].append(r["box"])
        # The earliest copy is when it was sent; prefer a team's own copy's link over the desk's.
        if r["at"] < have["at"]:
            have["at"], have["sim_at"] = r["at"], r["sim_at"] or have["sim_at"]
        if have["desk_copy"] and not r["desk_copy"] and r["url"]:
            have["url"], have["box"], have["fid"], have["src"], have["desk_copy"] = r["url"], r["box"], r["fid"], r["src"], False
    return list(out.values())


def _readings(rows):
    shas = {}
    for r in rows:
        if r.get("sha") and r["team_id"]:
            shas.setdefault(r["team_id"], set()).add(r["sha"])
    out = {}
    for team_id, s in shas.items():
        for d in DocumentAnalysis.objects.filter(team_id=team_id, sha256__in=list(s)).defer("passages", "facts"):
            out[(team_id, d.sha256)] = d
    return out


def _band(pct):
    if pct is None:
        return ""
    return "likely" if pct >= ai_docs.LIKELY else "possibly" if pct >= ai_docs.POSSIBLY else "low"


def rows(who, f, team=None):
    """Every file row the reader may see, filtered, newest first."""
    assert who in WHO
    ctx = Ctx(who, team)
    found = []
    for fn in SOURCES_FNS:
        found.extend(fn(ctx, f))
    found = _merge(found)
    if who != "desk":
        reads = _readings(found)
        for r in found:
            d = reads.get((r["team_id"], r.get("sha"))) if r.get("sha") else None
            r["ai"] = d
            r["ai_state"] = d.status if d else ("none" if not r["is_inject_doc"] and r["direction"] != "from_persona" else "")
            r["band"] = _band(d.ai_written) if d and d.status == "done" else ""
    copies = {}
    for r in found:  # the same content sent again by the team (in another conversation)
        if r.get("sha") and r["direction"] != "inject":
            copies.setdefault((r["team_id"], r["sha"]), set()).add(r["thread"])
    tz = timezone.get_current_timezone()
    since = timezone.make_aware(datetime.combine(f.since, time.min), tz) if f.since else None
    until = timezone.make_aware(datetime.combine(f.until, time.min), tz) + timedelta(days=1) if f.until else None
    q = f.q.lower()
    out = []
    for r in found:
        r["team"] = ctx.teams.get(r["team_id"])
        r["direction_name"] = DIRECTIONS[r["direction"]]
        r["source_name"] = SOURCES[r["source"]]
        r["copies"] = len(copies.get((r["team_id"], r.get("sha")), ())) if r.get("sha") else 1
        if f.direction and r["direction"] != f.direction or f.source and r["source"] != f.source:
            continue
        if f.type and r["type"] != f.type or f.sender and r["sender_key"] != f.sender:
            continue
        if f.inject and r["inject_id"] != f.inject:
            continue
        if since and r["at"] < since or until and r["at"] >= until:
            continue
        if who != "desk":
            if f.ai and r["ai_state"] != f.ai or f.band and r["band"] != f.band:
                continue
        if q:
            hay = [r["filename"], r["subject"], r["sender"]]
            if who != "desk" and r.get("ai"):
                hay += [r["ai"].summary, r["ai"].doc_type]
            if not any(q in (h or "").lower() for h in hay):
                continue
        out.append(r)
    out.sort(key=lambda r: r["at"], reverse=True)
    return out


def _group_of(r, group):
    if group == "team":
        return (r["team"].name if r["team"] else "—"), r["team_id"] or 0
    if group == "conversation":
        return (r["subject"] or "(no subject)") + (f" · {r['team'].name}" if r["team"] else ""), r["thread"]
    if group == "sender":
        return r["sender"] or "—", r["sender_key"]
    if group == "inject":
        return (r["inject"] or "No inject"), r["inject_id"] or 0
    if group == "file":
        return r["filename"], r.get("sha") or r["group_key"]
    if group == "day":
        day = timezone.localtime(r["at"]).date()
        return day.strftime("%a %d %b %Y"), day.isoformat()
    return "", ""


def grouped(found, group):
    """[(title, [rows], {n, size})] in the rows' order (newest group first); a single untitled
    group for "none"."""
    if group == "none":
        return [("", found, {"n": len(found), "size": sum(r["size"] or 0 for r in found)})]
    out, index = [], {}
    for r in found:
        title, key = _group_of(r, group)
        if key not in index:
            index[key] = len(out)
            out.append((title, [], {"n": 0, "size": 0}))
        g = out[index[key]]
        g[1].append(r)
        g[2]["n"] += 1
        g[2]["size"] += r["size"] or 0
    if group == "team":
        out.sort(key=lambda g: g[0])
    return out


def senders(found):
    """The senders in the rows, for the From filter: [(key, name)]."""
    seen = {}
    for r in found:
        if r["sender_key"] and r["sender_key"] not in seen:
            seen[r["sender_key"]] = r["sender"]
    return sorted(seen.items(), key=lambda kv: kv[1].lower())


def page(found, f):
    start = (f.page - 1) * PAGE
    return found[start:start + PAGE], len(found) > start + PAGE


# --- downloads ---------------------------------------------------------------------------------------

def visible(who, team, src, fid):
    """The file (model instance) if this reader may download it, else None."""
    if src == "m":
        a = MailAttachment.objects.select_related("mail").filter(pk=fid, cid="").first()
        if not a:
            return None
        if who == "leader" and a.mail.team_id != team.id:
            return None
        return a
    if src == "r":
        a = ResponseAttachment.objects.select_related("response").filter(pk=fid).first()
        if not a or a.data is None or not a.response.team_id or (who == "leader" and a.response.team_id != team.id):
            return None
        return a
    if src == "x":
        a = MessageAttachment.objects.select_related("message").filter(pk=fid).first()
        if not a or (who == "leader" and a.message.team_id != team.id):
            return None
        return a
    if src == "c":
        from .models import PostAttachment
        a = PostAttachment.objects.select_related("post__room").filter(pk=fid).first()
        if not a or (who == "leader" and team.id not in (a.post.room.teams or [])):
            return None
        return a
    return None


def content(src, a):
    if src == "m":
        return a.content()
    if src == "c":
        return a.content()
    return bytes(a.data) if a.data is not None else None


def export_csv(found, who):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    head = ["Sent", "Scenario time", "Team", "File", "Type", "Size", "Direction", "Source", "From", "To", "Inject",
            "Conversation", "Mailboxes", "Sent again"]
    if who != "desk":
        head += ["SIMEX AI", "Document type", "Summary", "AI-written (est. %)"]
    w.writerow(head)
    for r in found:
        row = [timezone.localtime(r["at"]).strftime("%Y-%m-%d %H:%M"),
               timezone.localtime(r["sim_at"]).strftime("%Y-%m-%d %H:%M") if r["sim_at"] else "",
               r["team"].name if r["team"] else "", r["filename"], TYPES[r["type"]], r["size"], r["direction_name"],
               r["source_name"], r["sender"], r["to"], r["inject"], r["subject"], "; ".join(r["boxes"]), r["copies"] - 1]
        if who != "desk":
            d = r.get("ai")
            row += [AI_STATES.get(r.get("ai_state") or "", ""), d.doc_type if d else "", d.summary if d else "",
                    d.ai_written if d and d.ai_written is not None else ""]
        w.writerow(row)
    return buf.getvalue()


def zip_stream(found, limit=500 * 1024 * 1024):
    """The rows' files as a zip, written as it goes (nothing on the server's disk, where files
    live in memory): folders by team, names made unique. Stops at limit bytes of files."""
    import zipfile

    class Sink:
        def __init__(self):
            self.chunks, self.pos = [], 0

        def write(self, b):
            self.chunks.append(bytes(b))
            self.pos += len(b)
            return len(b)

        def tell(self):
            return self.pos

        def flush(self):
            pass

        def take(self):
            out = b"".join(self.chunks)
            self.chunks = []
            return out

    sink = Sink()
    names, total = set(), 0
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for r in found:
            a = visible("excon", None, r["src"], r["fid"])
            data = content(r["src"], a) if a else None
            if data is None:
                continue
            total += len(data)
            if total > limit:
                break
            folder = (r["team"].name if r["team"] else "Other").replace("/", "-")
            name = f"{folder}/{r['filename'].replace('/', '-') or 'file'}"
            base, n = name, 2
            while name in names:
                stem, dot, ext = base.rpartition(".")
                name = f"{stem} ({n}).{ext}" if dot else f"{base} ({n})"
                n += 1
            names.add(name)
            z.writestr(name, data)
            yield sink.take()
    yield sink.take()

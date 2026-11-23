"""The knowledge library: the participant-facing documents of the exercise package (baseline
documents, inject documents, maps) as text (KbDocument), so that Claude can look things up when
it suggests a persona's answer (ops/suggest.py).

The documents come from the knowledge base (../undac-simex, scripts/export_ops.py), which
exports only what participants are given or may be given: never the MEL, expected outcomes,
role-player scripts or ExCon's management files. ExCon uploads the export on the Library page
(views_suggest.library) and can correct, per document, whether the participants have it.

Whether a team has a document (status): release "all" from the start; "with" once the inject
or role play `serial` reached that team (a live delivery of the inject to the team, or a sitting
of the role play for it that has started); "no" never, until ExCon changes it.
"""

import gzip
import json
import re
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import KB_RELEASES, Attachment, Inject, KbDocument, Message, audit

FORMAT = "simex-kb-1"
MAX_BYTES = 200 * 1024 * 1024
SERIAL = re.compile(r"^\d{1,2}-\d{1,3}[a-z]?$")


class LibraryError(ValueError):
    pass


# --- import ----------------------------------------------------------------------------------------

def read_bundle(data):
    """The export file (JSON, gzipped or not) -> the list of document dicts. Raises LibraryError."""
    if len(data) > MAX_BYTES:
        raise LibraryError("The file is too large.")
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError as e:
            raise LibraryError(f"Not a readable .gz file ({e}).") from e
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise LibraryError("Not a library export (JSON) from the knowledge base.") from e
    if not isinstance(doc, dict) or doc.get("format") != FORMAT or not isinstance(doc.get("documents"), list):
        raise LibraryError(f"Not a library export: expected format {FORMAT} (scripts/export_ops.py).")
    out = []
    for d in doc["documents"]:
        if not isinstance(d, dict) or not d.get("key") or not isinstance(d.get("text"), str):
            continue
        release = d.get("release") if d.get("release") in KB_RELEASES else "no"
        serial = str(d.get("serial") or "")[:40]
        if release == "with" and not serial:
            release = "no"
        out.append({"key": str(d["key"])[:40], "path": str(d.get("path") or "")[:500],
                    "title": str(d.get("title") or d.get("path") or d["key"])[:300],
                    "category": str(d.get("category") or "")[:40], "release": release, "serial": serial,
                    "text": d["text"]})
    if not out:
        raise LibraryError("The export holds no documents.")
    return out


@transaction.atomic
def apply(docs, delete_missing=False, user=""):
    """Adds and updates the documents; a release ExCon changed since the last import stays."""
    existing = {d.key: d for d in KbDocument.objects.all()}
    n = {"new": 0, "updated": 0, "kept": 0, "deleted": 0}
    for d in docs:
        snap = f"{d['release']}|{d['serial']}"
        row = existing.pop(d["key"], None)
        if row is None:
            KbDocument.objects.create(imported_release=snap, chars=len(d["text"]), **d)
            n["new"] += 1
            continue
        changed = False
        for f in ("path", "title", "category", "text"):
            if getattr(row, f) != d[f]:
                setattr(row, f, d[f])
                changed = True
        local = f"{row.release}|{row.serial}" != row.imported_release
        if local:
            n["kept"] += 1
        elif (row.release, row.serial) != (d["release"], d["serial"]):
            row.release, row.serial, changed = d["release"], d["serial"], True
        row.imported_release, row.chars = snap, len(row.text)
        row.save()
        n["updated"] += changed
    if delete_missing:
        n["deleted"] = len(existing)
        KbDocument.objects.filter(pk__in=[d.pk for d in existing.values()]).delete()
    audit("library_import", json.dumps(n), user)
    return n


# --- what a team has ---------------------------------------------------------------------------------

@dataclass
class TeamView:
    """What one team has been given so far: the injects delivered to it (serial -> when) and the
    role plays it has had (serial -> when), for the statuses below. Built once per suggestion."""
    team: object
    injects: dict = field(default_factory=dict)  # mel_id -> sent_at
    planned: dict = field(default_factory=dict)  # mel_id -> Inject still to come for this team
    roleplays: dict = field(default_factory=dict)  # serial -> started (datetime) for this team
    rp_planned: dict = field(default_factory=dict)  # serial -> next start
    files: dict = field(default_factory=dict)  # lower filename -> mel_id of the inject carrying it

    def inject_state(self, mel_id):
        if mel_id in self.injects:
            return "known"
        return "upcoming" if mel_id in self.planned else "not_given"


def delivered(team):
    """{mel_id: first sent_at} of the injects (and their follow-ups) that reached the team."""
    q = (Message.objects.filter(kind__in=["live", "followup"], status="sent", inject__isnull=False)
         .filter(Q(team=team) | Q(inbox_team=team) | Q(person__team=team))
         .exclude(inbox_owner__role="tl").values_list("inject__mel_id", "sent_at"))
    out = {}
    for mel, at in q.order_by("sent_at"):
        out.setdefault(mel, at)
    return out


def for_team(team, roleplays=None, rp_planned=None):
    """The TeamView of team. roleplays / rp_planned come from the role-play module (suggest.py)."""
    v = TeamView(team=team, injects=delivered(team), roleplays=roleplays or {}, rp_planned=rp_planned or {})
    for inj in Inject.objects.exclude(status__in=["cancelled"]).exclude(mel_id__in=list(v.injects)):
        if inj.team_ids is None or team.id in (inj.team_ids or []):
            v.planned[inj.mel_id] = inj
    for mel, name in Attachment.objects.filter(inject__isnull=False).values_list("inject__mel_id", "filename"):
        v.files.setdefault(name.lower(), mel)
    return v


def doc_status(doc, view):
    """(state, label) of a document for a team: state known | upcoming | not_given."""
    if doc.release == "all":
        return "known", "the participants have it (baseline / from the start)"
    if doc.release == "no":
        mel = view.files.get(doc.title.lower())
        if not mel:
            return "not_given", "not given to the participants"
        serial = mel
    else:
        serial = doc.serial
    if serial in view.injects:
        return "known", f"the team has it (with inject {serial}, sent {_hm(view.injects[serial])})"
    if serial in view.roleplays:
        return "known", f"the team has it (handed out in role play {serial}, {_hm(view.roleplays[serial])})"
    if serial in view.planned:
        inj = view.planned[serial]
        return "upcoming", (f"NOT YET given to the team: comes with inject {serial}"
                            + (f", scenario time {inj.scenario_dt}" if inj.scenario_dt else "")
                            + (f", due {_hm(inj.send_at)}" if inj.send_at else ""))
    if serial in view.rp_planned:
        return "upcoming", f"NOT YET given to the team: handed out in role play {serial} at {_hm(view.rp_planned[serial])}"
    return "not_given", f"not given to this team (goes with {serial}, which it hasn't had)"


def _hm(dt):
    return timezone.localtime(dt).strftime("%a %d %b %H:%M") if dt else "?"


# --- search ------------------------------------------------------------------------------------------

STOP = set("""a an and are as at be by for from has have how i in is it its of on or the this to was were what when
where which who why with you your our we they them their there not no any can do does did about into over than
then so if all also""".split())


def terms(query):
    words = re.findall(r"[\w'-]+", (query or "").lower())
    return list(dict.fromkeys(w for w in words if len(w) > 1 and w not in STOP))[:12]


def passages(text, words, width=700, limit=3):
    """The best passages of text for the words: (score, start, snippet)."""
    low = text.lower()
    hits = sorted(m.start() for w in words for m in re.finditer(re.escape(w), low))
    if not hits:
        return []
    found = []
    for h in hits:
        start = max(0, h - width // 3)
        window = low[start:start + width]
        score = sum(1 for w in words if w in window) * 10 + sum(window.count(w) for w in words)
        found.append((score, start))
    found.sort(key=lambda x: -x[0])
    out, used = [], []
    for score, start in found:
        if any(abs(start - u) < width for u in used):
            continue
        used.append(start)
        out.append((score, start, text[start:start + width].strip()))
        if len(out) >= limit:
            break
    return out


def search(query, limit=8):
    """[(doc, score, [snippet])] of the library documents best matching query."""
    words = terms(query)
    if not words:
        return []
    q = Q()
    for w in words:
        q |= Q(text__icontains=w) | Q(title__icontains=w)
    out = []
    for d in KbDocument.objects.filter(q).only("id", "key", "title", "path", "category", "release", "serial", "text"):
        ps = passages(d.text, words)
        title_hits = sum(1 for w in words if w in d.title.lower())
        score = title_hits * 25 + (sum(p[0] for p in ps[:2]) if ps else 0)
        if score:
            out.append((d, score, [p[2] for p in ps]))
    out.sort(key=lambda x: -x[1])
    return out[:limit]


def read(doc, part=1, size=12000):
    """One part of a document's text (1-based) and the number of parts."""
    n = max(1, -(-len(doc.text) // size))
    part = min(max(1, part), n)
    return doc.text[(part - 1) * size:part * size], part, n


def counts():
    rows = KbDocument.objects.values_list("release", flat=True)
    return {k: sum(1 for r in rows if r == k) for k in KB_RELEASES} | {"total": len(rows)}


def serial_ok(s):
    return bool(SERIAL.match(s or ""))

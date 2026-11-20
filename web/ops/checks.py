"""Rule checks on an unsent inject: what to fix before it goes out (Injects list, inject page).
The AI review (ai.review_inject) reads the same inject for what rules can't see."""

import re

from . import documents
from .importer import LOCKED

# XXXX, [INSERT TIME], TBC, or a bracketed per-team list to cut down ("[11:00-Team A, 11:20-Team B]")
PLACEHOLDER = re.compile(r"X{3,}|\[\s*insert|\bTBC\b|\bTBD\b|\[[^\]\n]*\bTeam [A-Z]\b[^\]\n]*\]", re.I)
FILE_SUBJECT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|png|jpe?g|mp4|zip)\s*$", re.I)


def inject_flags(inj, filenames, senders):
    """What to fix before an unsent inject goes out, as (level, label, detail): 'err' for
    something missing, 'check' for a likely problem to look at. Sent and cancelled: none."""
    if not unsent(inj):
        return []
    flags = []
    missing = documents.missing_docs(inj, filenames)
    if missing:
        flags.append(("err", f"{len(missing)} doc{'s' if len(missing) > 1 else ''} missing",
                      "Listed in the MEL but not attached: " + "; ".join(missing)))
    elif not filenames and re.search(r"attach", f"{inj.subject or ''} {inj.body or ''}", re.I):
        flags.append(("check", "no attachment", "Subject or body mentions an attachment, but nothing is attached."))
    if inj.status == "failed" and inj.last_error:
        flags.append(("err", "failed", inj.last_error))
    if not inj.sender_local:
        flags.append(("err", "no sender", "No sender set."))
    elif inj.sender_local not in senders:
        flags.append(("check", "unknown sender", f"Sender '{inj.sender_local}' is not in the Senders list."))
    if not (inj.subject or "").strip():
        flags.append(("err", "no subject", "Empty subject."))
    elif FILE_SUBJECT.search(inj.subject):
        flags.append(("check", "subject is a file name", inj.subject))
    if not (inj.body or "").strip():
        if not filenames:  # news articles and the like are just the attachment
            flags.append(("err", "no body", "Empty body and nothing attached."))
    else:
        if m := PLACEHOLDER.search(inj.body):
            flags.append(("check", "placeholder", f"Unfilled placeholder in the body: …{inj.body[max(0, m.start() - 30):m.end() + 30]}…"))
        if "***" in inj.body:
            flags.append(("check", "ExCon note", "The body contains '***', usually a note for ExCon left in."))
    if not inj.send_at:
        flags.append(("check", "no send time", "No send time: it can only be sent by hand."))
    return flags


def files_by_inject():
    """inject id -> attached filenames, without loading the files."""
    from .models import Attachment
    out = {}
    for iid, name in Attachment.objects.values_list("inject_id", "filename"):
        out.setdefault(iid, []).append(name)
    return out


def unsent(inj):
    return inj.status not in LOCKED and inj.status != "cancelled"

"""What's new: a short note on each new feature for the people it concerns (participants,
team leaders, the persona desk, role players, ExCon), in the notifications drawer of every
base template (templates/ops/_whatsnew.html, static/ops/whatsnew.js). The drawer opens by
itself on a login's first page load after a note ships, then the note is seen
(WhatsNewSeen) and stays listed without opening the drawer again.

Adding an entry to ENTRIES is the release note: add one, newest first, for any change people
will notice. key never changes once shipped (it is what "seen" remembers)."""

from datetime import date

from .access import desk_of, is_excon, leader_of, participant_of, rp_of

PARTICIPANT, LEADER, DESK, RP, EXCON = "participant", "leader", "desk", "rp", "excon"
MAIL = {PARTICIPANT, LEADER, DESK}  # everyone who writes in SIMEX Mail
EVERYONE = {PARTICIPANT, LEADER, DESK, RP, EXCON}
SHOWN = 8  # notes listed in the drawer (the newest)

ENTRIES = [
    {"key": "2026-11-desk-copy-personas", "date": date(2026, 11, 24), "who": {DESK, EXCON},
     "title": "Copy other personas on a message",
     "body": "When you write as a persona to a team, you can now put **other personas in To or Cc** too "
             "(type their name or address). The team sees them on the message, and when it answers with "
             "**Reply all**, the answer reaches them as well. They get no copy of their own: the persona "
             "desk already has it. ExCon's **Compose** has a **Cc other personas** list for the same."},
    {"key": "2026-11-rich-mail", "date": date(2026, 11, 24), "who": MAIL | {EXCON},
     "title": "Formatting and pictures in your messages",
     "body": "Messages in SIMEX Mail can now have **bold**, *italics*, underline, colours, lists, quotes and "
             "links: use the formatting bar under the message (the **T** button shows or hides it) or the "
             "usual shortcuts (Ctrl+B, Ctrl+I, Ctrl+U, Ctrl+K for a link).\n\n"
             "To put a **picture in the text**, paste it, drop it on the message, or use the picture button "
             "beside the paper clip. Big photos are made smaller for you. Pictures stay in drafts, and go "
             "out in the email too when the exercise runs by email."},
    {"key": "2026-11-whats-new", "date": date(2026, 11, 24), "who": EVERYONE,
     "title": "What's new",
     "body": "New features are announced here, in the notifications drawer (the bell at the top). It opens "
             "by itself once when there is something new."},
]


def kinds(user):
    """Which audiences a login belongs to."""
    if not user or not user.is_authenticated:
        return set()
    out = set()
    if is_excon(user):
        out.add(EXCON)
    if desk_of(user):
        out.add(DESK)
    if leader_of(user):
        out.add(LEADER)
    if rp_of(user):
        out.add(RP)
    if participant_of(user):
        out.add(PARTICIPANT)
    return out


def for_user(user, entries=None):
    """{"notes": [{key, date, title, body, new}], "new": how many unseen} for a login, or None."""
    mine = kinds(user)
    if not mine:
        return None
    items = [e for e in (ENTRIES if entries is None else entries) if e["who"] & mine][:SHOWN]
    if not items:
        return None
    from .models import WhatsNewSeen
    seen = set(WhatsNewSeen.objects.filter(user=user, key__in=[e["key"] for e in items]).values_list("key", flat=True))
    items = [{**e, "new": e["key"] not in seen} for e in items]
    return {"notes": items, "new": sum(1 for e in items if e["new"])}


def mark_seen(user, keys):
    from .models import WhatsNewSeen
    known = {e["key"] for e in ENTRIES}
    for k in set(keys) & known:
        WhatsNewSeen.objects.get_or_create(user=user, key=k)

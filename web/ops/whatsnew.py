"""What's new: a short note on each new feature for the people it concerns (participants,
team leaders, the persona desk, role players, ExCon), in the notifications drawer of every
base template (templates/ops/_whatsnew.html, static/ops/whatsnew.js). The drawer never opens by
itself: a small star on its button says there is something new, and opening it marks the
notes seen (WhatsNewSeen); they stay listed.

Adding an entry to ENTRIES is the release note: add one, newest first, for any change people
will notice. key never changes once shipped (it is what "seen" remembers)."""

from datetime import date

from .access import desk_of, is_excon, leader_of, participant_of, rp_of

PARTICIPANT, LEADER, DESK, RP, EXCON = "participant", "leader", "desk", "rp", "excon"
MAIL = {PARTICIPANT, LEADER, DESK}  # everyone who writes in SIMEX Mail
EVERYONE = {PARTICIPANT, LEADER, DESK, RP, EXCON}
SHOWN = 8  # notes listed in the drawer (the newest)

ENTRIES = [
    {"key": "2026-11-desk-ai-answers", "date": date(2026, 11, 25), "who": {DESK, EXCON},
     "title": "Leave it to the AI persona",
     "body": "When a team writes to an AI persona that is going to answer, the conversation goes to **Later** "
             "with *AI persona answers at 14:20 · no reply needed*, and your devices aren't pinged. If the AI "
             "persona isn't answering that one, it stays in **Needs attention** and says so: answer it yourself."},
    {"key": "2026-11-network-filters", "date": date(2026, 11, 25), "who": {DESK, EXCON},
     "title": "Filter the Network map",
     "body": "On **Network**, **All teams** shows only the teams you tick (or one alone), **All messages** which "
             "kinds count (injects, answers, teams' mail, leaders', persona to persona) and **Personas** all, AI "
             "only, people only or none. **Find** a name to bring its dot to the middle. **Clear filters** shows "
             "everything again."},
    {"key": "2026-11-desk-only-mine", "date": date(2026, 11, 25), "who": {DESK, EXCON},
     "title": "Hear only of your own conversations",
     "body": "The **gear** at the top (next to the bell) has your own settings. Tick **Only my conversations** to "
             "get notifications, alerts and pushes only for conversations assigned to you or that you follow. "
             "Being given one, and your follow-ups, still come."},
    {"key": "2026-11-desk-all-teams", "date": date(2026, 11, 25), "who": {DESK, EXCON},
     "title": "One message to every team",
     "body": "In **To**, pick **All teams** (or type it): a chip for each team but the Test team. Each team gets "
             "its own copy, as if written to it alone, and its reply is a conversation of its own. Remove the "
             "chips of teams it shouldn't go to."},
    {"key": "2026-11-desk-done-toolbar", "date": date(2026, 11, 25), "who": {DESK, EXCON},
     "title": "No reply needed, from the toolbar",
     "body": "In an open conversation, the ✓ next to Delete marks it as needing no reply (and opens the next one "
             "waiting). Tick several rows in the Inbox and the same ✓ marks them all at once, on a phone too."},
    {"key": "2026-11-list-filters", "date": date(2026, 11, 25), "who": {PARTICIPANT, LEADER},
     "title": "Show only unread mail",
     "body": "Above the list, **Unread**, **Starred**, **Has files** and **Not replied** show only those "
             "conversations, with how many there are. Click again to see everything. They combine, and stay on "
             "while you search and move from one conversation to the next."},
    {"key": "2026-11-desk-filters", "date": date(2026, 11, 25), "who": {DESK, EXCON},
     "title": "Filter the desk's list",
     "body": "Above the desk's list: **Unread**, **Needs attention**, **Urgent**, **Mine**, **Nobody has it**, "
             "**Waiting 30 min+** and more, one click each. **More filters** picks personas, teams, who has a "
             "conversation, how long a team has waited, how recent it is and live or test traffic. The filters "
             "in use show as tags you can remove; bookmark a filtered list to come back to it."},
    {"key": "2026-11-persona-mail", "date": date(2026, 11, 25), "who": {DESK, EXCON},
     "title": "Personas can write to each other",
     "body": "Put another persona in **To**, with or without teams: it lands on the persona desk for them, and an "
             "**AI persona** in To answers on its own. With no team, no team sees it: try out an AI persona, or play "
             "partners talking among themselves. With teams in **Cc**, they read the whole exchange as background "
             "traffic and can join with Reply all. AI personas answering each other stop after 3 answers in a row. "
             "ExCon's **Compose** has **To other personas** for the same."},
    {"key": "2026-11-suggest-desk-mail", "date": date(2026, 11, 25), "who": {DESK, EXCON},
     "title": "Suggested replies stay consistent across teams",
     "body": "**Suggest a reply** now reads the persona's other conversations with the team, what the other "
             "personas have been saying to it, and what this persona already told the **other teams**, so a "
             "persona gives every team the same facts and positions and doesn't contradict itself. What another "
             "team asked or was told is never passed on: a fact taken from another team's mail is flagged under "
             "**Check before sending**."},
    {"key": "2026-11-desk-injects", "date": date(2026, 11, 25), "who": {DESK, EXCON},
     "title": "See the injects coming up",
     "body": "The persona desk has an **Injects** tab (read-only): every inject by time, the next one "
             "highlighted, with the persona it comes from, who gets it and how. Open one to read it as each "
             "team will (names filled in), with its files, and, once sent, a link to each team's conversation. "
             "*Coming up*, *Sent* and *All*, by persona, team or words. Don't pass on an inject before it "
             "goes out. An inject in a conversation now links to it too."},
    {"key": "2026-11-persona-voice-role-play", "date": date(2026, 11, 25), "who": {DESK, EXCON},
     "title": "Personas sound like their role-play character",
     "body": "When a persona is also played face to face, by phone or VTC, **Suggest a reply** and "
             "**ChatSIMEX** now take their temperament, attitude and way of speaking from the role-play script, "
             "so the team meets the same person in the mail and in the room. It works whoever answers the mail, "
             "and whether or not anyone is assigned to play the role play yet. Facts still come only from the "
             "sittings the team has had. ExCon can link a role play to its persona(s) on the role play's page; "
             "otherwise it is found by the persona's name."},
    {"key": "2026-11-picture-size", "date": date(2026, 11, 24), "who": MAIL | {EXCON},
     "title": "Make a picture in your message smaller or bigger",
     "body": "**Click a picture** in the message you are writing to change its size: choose **Small**, "
             "**Medium**, **Large** or **Original size** in the bar over it, or drag its blue corner. "
             "**Remove** takes it out. New pictures now go in at a size that fits the message."},
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

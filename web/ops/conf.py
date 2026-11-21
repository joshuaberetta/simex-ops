"""Operator settings stored in the database (Settings tab)."""

from .models import Setting

DEFAULTS = {
    "footer": "EXERCISE EXERCISE EXERCISE\nThis message is part of the UNDAC SIMEX. It does not describe a real emergency.",
    "subject_prefix": "",
    "subject_inject_code": "0",  # "[<inject code>] " before every inject's subject
    "default_reply_to": "",  # email channel: leave empty so replies are tracked
    "excon_bcc": "",  # email channel: copy of every live email
    "test_address": "",
    "test_redirect": "delivered+{key}@resend.dev",
    "grace_minutes": "10",
    "live_armed": "0",
    "paused": "0",
    "team_to_team": "0",  # participants may write to other teams' inboxes
    # Each team member also has their own mailbox (besides the team's shared inbox), which gets a
    # copy of the team's mail, and members and their leader write to each other there.
    "personal_mailboxes": "1",
    "max_upload_mb": "10",  # participants' attachments
    # How injects are delivered: "email" (Resend, the main flow), "inapp" (SIMEX Mail, the
    # backup if email has problems) or "per_inject" (each inject's own channel field).
    "channel_mode": "email",
    # Claude analysis of replies (ops/ai.py): off until ExCon turns it on.
    "ai_enabled": "0",
    "ai_auto": "1",  # analyse every reply as it arrives (else only on request)
    "ai_tests": "1",  # include Test team replies
    "ai_injects": "1",  # review unsent injects (missing attachments, placeholders...) when they change
    "ai_daily_usd": "5",  # spend cap per day (Nairobi), estimated
}
CHANNEL_MODES = {"email": "Email (Resend)", "inapp": "SIMEX Mail (in-app inboxes)", "per_inject": "Per inject"}


def get(key):
    # A fresh query every time: a long-running send must see a pause straight away.
    value = Setting.objects.filter(key=key).values_list("value", flat=True).first()
    return value if value is not None else DEFAULTS.get(key, "")


def get_all():
    out = dict(DEFAULTS)
    for key, value in Setting.objects.values_list("key", "value"):
        if value is not None:
            out[key] = value
    return out


def put(key, value):
    Setting.objects.update_or_create(key=key, defaults={"value": value})


def flag(key):
    return get(key) == "1"


def channel_for(inj):
    """The channel an inject is delivered by right now (the global switch wins)."""
    mode = get("channel_mode")
    return inj.channel if mode == "per_inject" else ("inapp" if mode == "inapp" else "email")

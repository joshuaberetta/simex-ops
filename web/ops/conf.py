"""Operator settings stored in the database (Settings tab)."""

from . import reqcache
from .models import Setting

DEFAULTS = {
    "footer": "EXERCISE EXERCISE EXERCISE\nThis message is part of the UNDAC SIMEX. It does not describe a real emergency.",
    "subject_prefix": "",
    "subject_inject_code": "0",  # "[<inject code>] " before every inject's subject
    "default_reply_to": "",  # email channel: leave empty so replies are tracked
    "excon_bcc": "",  # email channel: copy of every live email
    "test_address": "",
    "login_reply_to": "",  # login emails (People & logins): where answers to them go
    "test_redirect": "delivered+{key}@resend.dev",
    "grace_minutes": "10",
    "desk_wait_minutes": "20",
    "cal_lead": "10",  # SIMEX Calendar: remind the people of a calendar this many minutes before an event (0: never)  # a Notice when a team waits this long for a persona's answer on the desk (0: never)
    "live_armed": "0",
    "paused": "0",
    "team_to_team": "0",  # participants may write to other teams' inboxes
    # Each team member also has their own mailbox (besides the team's shared inbox), which gets a
    # copy of the team's mail, and members and their leader write to each other there.
    "personal_mailboxes": "1",
    # SIMEX Mail addresses of the teams and members' own mailboxes (ops/addresses.py; Senders tab):
    # empty = the defaults, <team>@teams.<MAIL_DOMAIN> and <name>@<team>.teams.<MAIL_DOMAIN>.
    "addr_team_local": "",
    "addr_team_domain": "",
    "addr_member_local": "",
    "addr_member_domain": "",
    "max_upload_mb": "10",  # participants' attachments
    # How injects are delivered: "email" (Resend, the main flow), "inapp" (SIMEX Mail, the
    # backup if email has problems) or "per_inject" (each inject's own channel field).
    "channel_mode": "email",
    # Claude analysis of replies (ops/ai.py): off until ExCon turns it on.
    "ai_enabled": "0",
    "ai_auto": "1",  # analyse every reply as it arrives (else only on request)
    "ai_tests": "1",  # include Test team replies
    "ai_injects": "1",  # review unsent injects (missing attachments, placeholders...) when they change
    "ai_documents": "1",  # read the files teams send (ops/ai_docs.py): what they are, AI-written estimate
    "ai_documents_deep": "0",  # with the deep model (else the fast one)
    "tl_suggest": "1",  # team leaders' Suggest a reply in their own mailbox (ops/suggest_leader.py)
    "desk_injects": "1",  # the persona desk's Injects tab, read-only (ops/views_desk_injects.py)
    "desk_chat": "1",  # ChatSIMEX: the persona desk's chat with SIMEX AI (ops/desk_chat.py)
    "desk_triage": "1",  # read the persona desk's waiting conversations: needs an answer? (ops/desk_ai.py)
    "ai_daily_usd": "5",  # spend cap per day (Nairobi), estimated
    # AI personas (ops/npc.py): personas whose answers Claude writes. Off, and for the Test team
    # only ("test") until ExCon widens it to every team ("all").
    "npc_enabled": "0",
    "npc_scope": "test",
    # The simulation clock (ops/simclock.py; Settings → Simulation clock): SIMEX Mail shows the
    # scenario's date and time. sim_anchor (scenario time, exercise timezone) was the time at
    # sim_anchor_real (real, UTC ISO); it runs sim_rate times real speed.
    "sim_enabled": "0",
    "sim_anchor": "",
    "sim_anchor_real": "",
    "sim_rate": "1",
    "sim_follow": "1",  # a live inject delivered with a later Scenario time moves the clock forward to it
    "reset_at": "",  # Settings → Start afresh (ops/reset.py): received email from before it is not synced back
}
CHANNEL_MODES = {"email": "Email (Resend)", "inapp": "SIMEX Mail (in-app inboxes)", "per_inject": "Per inject"}


# Read fresh every time, even within a request: a long-running send must see a pause (or
# disarming) straight away; the scheduler's heartbeat changes under a request.
FRESH = {"paused", "live_armed", "scheduler_tick"}


def get(key):
    if key not in FRESH and reqcache.active():  # once per web request (ops/reqcache.py)
        return _all().get(key, DEFAULTS.get(key, ""))
    value = Setting.objects.filter(key=key).values_list("value", flat=True).first()
    return value if value is not None else DEFAULTS.get(key, "")


def _read_all():
    out = dict(DEFAULTS)
    for key, value in Setting.objects.values_list("key", "value"):
        if value is not None:
            out[key] = value
    return out


def _all():
    return reqcache.memo("conf", _read_all)


def get_all():
    """Every setting (a copy: callers may change it). Within a web request, as the request
    began; get() reads the FRESH ones (pause, arming) anew each time."""
    return dict(_all()) if reqcache.active() else _read_all()


def put(key, value):
    Setting.objects.update_or_create(key=key, defaults={"value": value})
    reqcache.clear()


def flag(key):
    return get(key) == "1"


def channel_for(inj):
    """The channel an inject is delivered by right now (the global switch wins)."""
    mode = get("channel_mode")
    return inj.channel if mode == "per_inject" else ("inapp" if mode == "inapp" else "email")

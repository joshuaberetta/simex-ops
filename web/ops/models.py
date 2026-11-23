"""Data model. Times are timezone-aware (stored UTC, shown in TIME_ZONE = Africa/Nairobi).

Two delivery channels share one pipeline: every inject delivery creates Message rows
(one per team / person / everyone, as for email). For the in-app channel each Message
also puts a Mail in the target team's simulated inbox; for the email channel it goes to
Resend. Participants' answers (in-app or by email) are Responses, which ExCon grades.
"""

import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

STATUSES = ["draft", "scheduled", "sending", "sent", "partial", "failed", "overdue", "cancelled"]
DELIVERY_MODES = {
    "each_team": "One message per team",
    "each_person": "One message per participant",
    "all_bcc": "Everyone (email: one email, all in Bcc; in-app: a copy in every team's inbox)",
    "custom": "Only the addresses below (email only)",
}
CHANNELS = {"inapp": "In-app inbox", "email": "Email (Resend)"}
# Fields an import may update; their last imported values are kept in Inject.imported so
# a re-import can tell local edits apart from MEL changes.
IMPORT_FIELDS = ["seq", "method", "mailbox", "scenario_dt", "send_at", "sender_local", "recipients_label",
                 "delivery", "to_leaders", "subject", "body", "supporting_documents"]
# A team member is a participant: one of the few people sharing the team's SIMEX Mail inbox.
# The team leader is the ExCon person who monitors the team (one per team): never a
# participant, with their own mailbox, a read-only view of the team's and the team Dashboard.
# The persona desk (ExCon, no team) shares one mailbox in SIMEX Mail where the teams' mail to
# the personas arrives and from which it writes as any persona (Senders tab); it reads every
# team's inbox and team leader's mailbox read-only, and has no access to the ExCon screens.
ROLES = {"member": "Team member", "tl": "Team leader (ExCon)", "desk": "Persona desk (ExCon)",
         "rp": "Role player (ExCon)"}
# A role player (ExCon, no team) plays a character face to face, by phone or VTC at set times
# (RolePlay below); they reach only their own schedule and briefs in SIMEX Mail (/inbox/rp/).
# Within a team, members may also hold a team role (TeamRole: Deputy Team Leader, Logistics...),
# set by ExCon on People & logins, so the Dashboard and reports can break behaviour down by it.


class Team(models.Model):
    code = models.CharField(max_length=40, unique=True)  # fills {team}, e.g. "a"
    name = models.CharField(max_length=120)  # fills {team_name}
    email = models.CharField(max_length=320, blank=True, default="")  # shared inbox for the email channel
    sort = models.IntegerField(default=0)
    active = models.BooleanField(default=True)
    is_test = models.BooleanField(default=False)  # the "Test team": test runs deliver here

    class Meta:
        ordering = ["sort", "name"]

    def __str__(self):
        return self.name

    def leader(self):
        """The team's active leader (a Person, role tl), or None."""
        return self.people.filter(role="tl", active=True).first()


class TeamRole(models.Model):
    """A function a team member holds in the team (e.g. Deputy Team Leader). Only a label for
    the figures: a member with one is still a participant sharing the team's inbox."""
    name = models.CharField(max_length=80, unique=True)
    sort = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort", "name"]

    def __str__(self):
        return self.name


class PersonQuerySet(models.QuerySet):
    def participants(self):
        """Team members only: the leaders are ExCon and never get a participant's copy."""
        return self.filter(role="member")


class Person(models.Model):
    """A team member (participant) or the team's leader (ExCon), see ROLES. Has a login (user)
    for SIMEX Mail, an email for the email channel and a personal email outside the exercise."""
    name = models.CharField(max_length=200)
    email = models.CharField(max_length=320, blank=True, default="")
    # Their own address: login emails (ops/logins.py) and the email copies of in-app injects
    # marked "also by email" (Inject.also_email). `email` stays the email channel's address.
    personal_email = models.CharField(max_length=320, blank=True, default="")
    login_shared_at = models.DateTimeField(null=True, blank=True)  # their current password was given out
    login_shared_via = models.CharField(max_length=320, blank=True, default="")  # the address emailed, or "by hand"
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL, related_name="people")
    role = models.CharField(max_length=20, default="member")  # member | tl | desk | rp (see ROLES)
    team_role = models.ForeignKey(TeamRole, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="people")  # members only
    active = models.BooleanField(default=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="person")
    lead_seen = models.IntegerField(default=0)  # leader: the last team-inbox Mail id they looked at
    last_seen_at = models.DateTimeField(null=True, blank=True)  # last signed-in request (ops/activity.py)

    objects = PersonQuerySet.as_manager()

    class Meta:
        ordering = ["team__sort", "-role", "name"]

    @property
    def is_leader(self):
        return self.role == "tl"

    @property
    def is_desk(self):
        return self.role == "desk"

    @property
    def is_rp(self):
        return self.role == "rp"

    def __str__(self):
        return self.name

    @property
    def first_name(self):
        return self.name.split()[0] if self.name.split() else self.name


class Sender(models.Model):
    local_part = models.CharField(max_length=120, unique=True)  # may contain {team}
    display_name = models.CharField(max_length=200, blank=True, default="")
    reply_to = models.CharField(max_length=320, blank=True, default="")  # email channel only
    # In-app (SIMEX Mail) only: the address participants see, e.g. news@cnn.com, when it isn't
    # <local_part>@MAIL_DOMAIN. local_part stays the persona's unique key (and its email username:
    # email always goes from MAIL_DOMAIN, the domain Resend can send from), so news@cnn.com and
    # news@reuters.com can be two personas, e.g. cnn-news and reuters-news.
    inapp_local = models.CharField(max_length=120, blank=True, default="")  # empty = local_part
    domain = models.CharField(max_length=200, blank=True, default="")  # empty = MAIL_DOMAIN
    active = models.BooleanField(default=True)
    # The team leader persona (e.g. undac-{team}-tl): played by each team's leader in SIMEX Mail.
    # What is sent as it shows in the leader's Sent; what members write to it, in their Inbox.
    team_leader = models.BooleanField(default=False)
    # Its colour on the persona desk (one of views_inbox.LABEL_COLOURS; empty = one picked from its username).
    colour = models.CharField(max_length=7, blank=True, default="")
    # ExCon's notes (who it is, what it's used for) and a category (UN, NGO, GoE...): the Senders
    # tab and its import/export only, never shown to participants.
    notes = models.TextField(blank=True, default="")
    category = models.CharField(max_length=60, blank=True, default="")

    class Meta:
        ordering = ["local_part"]

    def __str__(self):
        return f"{self.display_name} <{self.inapp_address}>"

    @property
    def inapp_address(self):
        """The SIMEX Mail address (with {team} unfilled)."""
        return f"{self.inapp_local or self.local_part}@{self.domain or settings.MAIL_DOMAIN}".lower()


class Inject(models.Model):
    mel_id = models.CharField(max_length=40, unique=True)
    seq = models.IntegerField(default=0)
    method = models.CharField(max_length=200, blank=True, null=True)
    mailbox = models.CharField(max_length=60, blank=True, null=True)
    scenario_dt = models.CharField(max_length=20, blank=True, null=True)
    send_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default="draft")
    channel = models.CharField(max_length=10, default="inapp")
    sender_local = models.CharField(max_length=120, blank=True, null=True)
    from_name = models.CharField(max_length=200, blank=True, null=True)
    reply_to = models.CharField(max_length=320, blank=True, null=True)
    delivery = models.CharField(max_length=20, default="each_team")
    team_ids = models.JSONField(null=True, blank=True)  # None = all teams
    to_extra = models.TextField(blank=True, null=True)
    cc = models.TextField(blank=True, null=True)
    bcc = models.TextField(blank=True, null=True)
    recipients_label = models.CharField(max_length=200, blank=True, null=True)
    subject = models.TextField(blank=True, null=True)
    body = models.TextField(blank=True, null=True)
    footer_mode = models.CharField(max_length=10, default="default")  # default | custom | none
    footer = models.TextField(blank=True, null=True)
    supporting_documents = models.TextField(blank=True, null=True)
    imported = models.JSONField(null=True, blank=True)
    attempt = models.IntegerField(default=0)
    last_error = models.TextField(blank=True, null=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    expects_reply = models.BooleanField(default=False)
    critical = models.BooleanField(default=False)  # ExCon's key inject: no reply in time is a critical miss (reports)
    to_leaders = models.BooleanField(default=False)  # also a copy to each team's leader
    # In-app: also emailed (Resend) to each recipient's personal email (Person.personal_email).
    also_email = models.BooleanField(default=False)
    reply_due_minutes = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["send_at", "seq"]

    def __str__(self):
        return self.mel_id

    @property
    def effective_channel(self):
        from . import conf
        return conf.channel_for(self)


class Attachment(models.Model):
    inject = models.ForeignKey(Inject, on_delete=models.CASCADE, related_name="attachments")
    filename = models.CharField(max_length=300)
    content_type = models.CharField(max_length=120, default="application/octet-stream")
    size = models.IntegerField(default=0)
    data = models.BinaryField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["filename"]


class Setting(models.Model):
    key = models.CharField(max_length=60, primary_key=True)
    value = models.TextField(null=True, blank=True)


class TestRun(models.Model):
    __test__ = False
    name = models.CharField(max_length=200)
    status = models.CharField(max_length=20, default="active")  # active | paused | done | cancelled
    redirect = models.CharField(max_length=320, blank=True, default="")  # email channel: you+{key}@...
    dry_run = models.BooleanField(default=False)
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    channel = models.CharField(max_length=10, blank=True, default="")  # "" = each inject's own | inapp | email

    def channel_for(self, inj):
        return self.channel or inj.effective_channel


class TestRunItem(models.Model):
    __test__ = False
    run = models.ForeignKey(TestRun, on_delete=models.CASCADE, related_name="items")
    inject = models.ForeignKey(Inject, on_delete=models.CASCADE)
    due_at = models.DateTimeField()
    status = models.CharField(max_length=20, default="pending")
    error = models.TextField(blank=True, null=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["due_at"]


class Message(models.Model):
    """One delivery: an email handed to Resend, or a Mail put in an in-app inbox."""
    inject = models.ForeignKey(Inject, null=True, blank=True, on_delete=models.SET_NULL)
    run = models.ForeignKey(TestRun, null=True, blank=True, on_delete=models.SET_NULL)
    kind = models.CharField(max_length=10)  # live | test | oneoff | reply | followup | leader | desk | login
    channel = models.CharField(max_length=10, default="inapp")
    target = models.CharField(max_length=200, blank=True, default="")
    idem_key = models.CharField(max_length=200, unique=True)
    from_addr = models.CharField(max_length=400)
    reply_to = models.CharField(max_length=320, blank=True, null=True)
    to = models.JSONField(default=list)
    cc = models.JSONField(default=list)
    bcc = models.JSONField(default=list)
    original_to = models.JSONField(null=True, blank=True)
    subject = models.TextField(default="")
    text = models.TextField(default="")
    html = models.TextField(default="")
    attachment_ids = models.JSONField(default=list)
    status = models.CharField(max_length=10, default="pending")  # pending | sent | failed | dry | withdrawn
    resend_id = models.CharField(max_length=100, blank=True, null=True)
    error = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)  # pending -> sending
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL)
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL)
    reply_token = models.CharField(max_length=20, unique=True, null=True, blank=True)
    delivery = models.CharField(max_length=30, blank=True, null=True)  # delivered, bounced... (never overwritten by a late "delivered" after a bounce)
    delivery_at = models.DateTimeField(null=True, blank=True)
    # Engagement: email = Resend open/click tracking; in-app = the team opening the
    # conversation (opened = read) and downloading its attachments (clicked). Details in Event.
    opened_at = models.DateTimeField(null=True, blank=True)
    open_count = models.IntegerField(default=0)
    clicked_at = models.DateTimeField(null=True, blank=True)
    click_count = models.IntegerField(default=0)
    extra_headers = models.JSONField(null=True, blank=True)
    response = models.ForeignKey("Response", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="answers")  # ExCon reply: the response it answers
    # In-app: which team inbox it goes to and the thread it continues.
    inbox_team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    inbox_owner = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")  # a leader's box
    author = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")  # the leader who wrote it
    thread = models.CharField(max_length=40, blank=True, default="")
    banner = models.TextField(blank=True, default="")
    body = models.TextField(blank=True, default="")  # in-app: text without footer/banner
    footer = models.TextField(blank=True, default="")
    persona = models.CharField(max_length=120, blank=True, default="")  # the sender's username (Sender.local_part, filled)
    # Follow-up (kind followup): the delivery of the inject it continues, in that thread.
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="followups")
    message_id_header = models.CharField(max_length=300, blank=True, default="")  # email: its Message-ID, from Resend
    # A follow-up or answer ExCon sent from an AI draft (Injects → Draft with AI), and whether
    # they changed its text first. Injects made from a draft link through InjectDraft.created.
    ai_draft = models.ForeignKey("InjectDraft", null=True, blank=True, on_delete=models.SET_NULL, related_name="messages")
    ai_edited = models.BooleanField(null=True, blank=True)
    npc = models.BooleanField(default=False)  # an answer an AI persona wrote and sent (ops/npc.py)

    class Meta:
        ordering = ["-id"]


class MessageAttachment(models.Model):
    """A file ExCon attached to an answer (kind reply) by uploading it: its own copy, sent by
    email or put on the in-app Mail. Inject documents go in Message.attachment_ids instead."""
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="files")
    filename = models.CharField(max_length=300)
    content_type = models.CharField(max_length=120, default="application/octet-stream")
    size = models.IntegerField(default=0)
    data = models.BinaryField()


class Event(models.Model):
    """Every delivery/engagement event about one of our messages: Resend's (delivered,
    bounced, opened, clicked...) and in-app ones (read, download)."""
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="events")
    type = models.CharField(max_length=30)
    at = models.DateTimeField(default=timezone.now)
    link = models.TextField(blank=True, null=True)  # clicked URL / downloaded file
    user_agent = models.CharField(max_length=400, blank=True, null=True)
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL)  # in-app reader

    class Meta:
        ordering = ["at", "id"]


class Mail(models.Model):
    """An item in a mailbox (folder inbox, sent, drafts). A mailbox is (team, owner): owner None
    is the team's shared inbox, owner set is that team leader's own mailbox. A conversation
    between a team and its leader has the same thread id in both. Team None (and owner None) is
    the persona desk's shared mailbox (ops/desk.py): a copy of what each team writes to the
    personas (Inbox) and of what the personas send (Sent), desk_team being the team on the other side."""
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.CASCADE, related_name="mails")
    owner = models.ForeignKey(Person, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    folder = models.CharField(max_length=10, default="inbox")  # inbox | sent
    thread = models.CharField(max_length=40, default="")
    from_name = models.CharField(max_length=200, default="")
    from_addr = models.CharField(max_length=320, default="")
    to_label = models.TextField(default="")  # shown in the To line (every recipient)
    to_person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    persona = models.CharField(max_length=120, blank=True, default="")  # the persona on the other side
    to_team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")  # team-to-team
    subject = models.TextField(default="")
    body = models.TextField(default="")
    footer = models.TextField(blank=True, default="")
    banner = models.TextField(blank=True, default="")  # test deliveries
    author = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    message = models.ForeignKey(Message, null=True, blank=True, on_delete=models.SET_NULL, related_name="mails")
    inject = models.ForeignKey(Inject, null=True, blank=True, on_delete=models.SET_NULL)
    in_reply_to = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    is_test = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    # Its date and time in the scenario (ops/simclock.py), stamped when it lands; None: the clock was off.
    sim_at = models.DateTimeField(null=True, blank=True)
    # Mail-client state (per team): star, archive (out of the Inbox, still in All mail), trash.
    starred = models.BooleanField(default=False)
    archived = models.BooleanField(default=False)
    trashed_at = models.DateTimeField(null=True, blank=True)
    cc_label = models.TextField(blank=True, default="")
    # Address-book keys chosen in compose ({"to": ["p:cnn", "t:3"], "cc": [...], "bcc": [...]}),
    # kept on drafts and sent items.
    recipients = models.JSONField(default=dict, blank=True)
    forward_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # The persona desk's copies: the team on the other side, and for what a team wrote, its Response.
    desk_team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    desk_response = models.ForeignKey("Response", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["team", "folder", "thread"])]

    def save(self, *args, **kwargs):
        if not self.thread:
            self.thread = uuid.uuid4().hex
        if self._state.adding and self.sim_at is None:
            from . import simclock
            self.sim_at = simclock.stamp(self)
        super().save(*args, **kwargs)


class MailLabel(models.Model):
    """A mailbox's own label in SIMEX Mail (Gmail's "folders"): applied to whole conversations, which
    may carry several. "Move to" a label = label it and take it out of the Inbox."""
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.CASCADE, related_name="mail_labels")  # None: the desk
    owner = models.ForeignKey(Person, null=True, blank=True, on_delete=models.CASCADE, related_name="+")  # as Mail.owner
    name = models.CharField(max_length=60)
    colour = models.CharField(max_length=7, blank=True, default="")  # one of views_inbox.LABEL_COLOURS
    # Put on by the app (views_inbox.AUTO_LABELS): in a team leader's mailbox, mail from the team's
    # address ("team") or from a member's own ("member"). The leader may rename or recolour it.
    auto = models.CharField(max_length=10, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name", "id"]
        constraints = [models.UniqueConstraint(fields=["team", "owner", "name"], name="mail_label_unique_name")]

    def __str__(self):
        return self.name


class DeskConversation(models.Model):
    """What the persona desk keeps about one of its conversations (Mail.thread, team None), so
    the people sharing it can tell what still needs an answer (ops/desk.py states): marked
    as needing no reply (the teams' mail up to done_upto, a Mail id: a newer message from a team
    opens it again), to follow up (until a persona writes in it after follow_upto, due at
    follow_at or now), taken by one of them, and Claude's reading of whether the teams' latest
    message needs an answer (ops/desk_ai.py: ai_upto, the last Mail id it read)."""
    thread = models.CharField(max_length=40, unique=True)
    done_upto = models.IntegerField(default=0)
    done_by = models.CharField(max_length=150, blank=True, default="")
    done_at = models.DateTimeField(null=True, blank=True)
    follow_upto = models.IntegerField(default=0)  # 0: no follow-up
    follow_at = models.DateTimeField(null=True, blank=True)  # None: due now
    follow_note = models.CharField(max_length=300, blank=True, default="")
    follow_by = models.CharField(max_length=150, blank=True, default="")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    owner_name = models.CharField(max_length=150, blank=True, default="")
    assigned_by = models.CharField(max_length=150, blank=True, default="")  # who gave it to the owner ("" : they took it)
    assigned_at = models.DateTimeField(null=True, blank=True)
    follow_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    follow_told = models.IntegerField(default=0)  # the follow_upto its "follow-up due" notice was for
    ai_upto = models.IntegerField(default=0)
    ai_status = models.CharField(max_length=10, blank=True, default="")  # "" | queued | done | failed
    ai_need = models.CharField(max_length=10, blank=True, default="")  # reply | optional | none
    ai_urgency = models.CharField(max_length=10, blank=True, default="")  # high | normal | low
    ai_ask = models.CharField(max_length=300, blank=True, default="")
    ai_reason = models.CharField(max_length=400, blank=True, default="")
    ai_error = models.TextField(blank=True, default="")
    ai_at = models.DateTimeField(null=True, blank=True)
    # The owner came from the default assignments (DeskDefault, desk.auto_assign); auto_done:
    # they were applied, or someone on the desk gave, took or released it, so they never are again.
    auto = models.BooleanField(default=False)
    auto_done = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)


class DeskDefault(models.Model):
    """Who on the persona desk has a persona's conversations by default (the desk's Assign tab,
    Default assignments): with one team, or (team None) every team without its own rule. Given
    once, to a conversation nobody has (desk.auto_assign: its first desk copy, or Apply now).
    Setup like the Senders: kept through Start afresh."""
    sender = models.ForeignKey(Sender, on_delete=models.CASCADE, related_name="desk_defaults")
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    set_by = models.CharField(max_length=150, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["sender", "team"], condition=models.Q(team__isnull=False), name="desk_default_team"),
            models.UniqueConstraint(fields=["sender"], condition=models.Q(team__isnull=True), name="desk_default_all"),
        ]


class DeskWatch(models.Model):
    """Someone on the persona desk (a login: a desk member or ExCon staff) involved in one of
    its conversations (Mail.thread, team None): they answered in it, took it or were given it,
    set a follow-up, or follow it (reason follow). off: they stopped following it, and nothing
    they do in it starts it again (but following it on purpose)."""
    thread = models.CharField(max_length=40, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    reason = models.CharField(max_length=10)  # answered | owner | follow_up | follow
    off = models.BooleanField(default=False)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["thread", "user"], name="desk_watch_once")]


class DeskNotice(models.Model):
    """One update for one person on the persona desk (ops/desk.py notify; the bell in SIMEX
    Mail): a conversation given to them, a follow-up due, a team writing again in a conversation
    they are involved in, someone else answering it, a team waiting too long, an AI persona's
    answer held. One per event and person (key)."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    key = models.CharField(max_length=120)  # e.g. in:<mail id>, fu:<thread>:<follow_upto>
    kind = models.CharField(max_length=10)  # assign | follow_up | reply | answer | wait | npc
    title = models.CharField(max_length=300)
    body = models.CharField(max_length=500, blank=True, default="")
    thread = models.CharField(max_length=40, blank=True, default="", db_index=True)
    url = models.CharField(max_length=300, blank=True, default="")
    test = models.BooleanField(default=False)  # about the Test team
    at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["user", "key"], name="desk_notice_once")]


class ThreadLabel(models.Model):
    """A label on a conversation (Mail.thread) of the label's mailbox."""
    label = models.ForeignKey(MailLabel, on_delete=models.CASCADE, related_name="threads")
    thread = models.CharField(max_length=40, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["label", "thread"], name="thread_label_unique")]


class MailAction(models.Model):
    """Something a participant did in their SIMEX Mail (star, archive, label, search, draft,
    send...): Mail only keeps the inbox as it is now; this keeps when. For the end-of-exercise
    reports (how the team managed its inbox). Reading and downloads are Events."""
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="+")
    owner = models.ForeignKey(Person, null=True, blank=True, on_delete=models.CASCADE, related_name="+")  # as Mail.owner
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")  # who did it
    action = models.CharField(max_length=20)  # see views_inbox.TRACKED
    threads = models.JSONField(default=list, blank=True)
    detail = models.JSONField(default=dict, blank=True)  # label name, search text, draft minutes...
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["team", "action"])]


class LoginEvent(models.Model):
    """A sign-in (password or QR code), a sign-out or a failed sign-in, with the device it came
    from (ops/activity.py, from Django's auth signals). Kept through Start afresh, like the logins."""
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.CASCADE, related_name="login_events")
    username = models.CharField(max_length=150, blank=True, default="")
    kind = models.CharField(max_length=10)  # signin | signout | failed
    method = models.CharField(max_length=10, blank=True, default="")  # password | qr (sign-ins)
    at = models.DateTimeField(default=timezone.now)
    ip = models.CharField(max_length=64, blank=True, default="")
    user_agent = models.CharField(max_length=400, blank=True, default="")
    host = models.CharField(max_length=10, blank=True, default="")  # mail | ops

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["person", "at"])]


class PushSubscription(models.Model):
    """A participant's (or leader's) device that gets a push notification for new mail (Web Push: a phone's
    home-screen app or a browser). Not in backups: devices subscribe again on their next visit."""
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.URLField(max_length=1000, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    user_agent = models.CharField(max_length=400, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    last_ok_at = models.DateTimeField(null=True, blank=True)


def file_hash(data):
    import hashlib
    return hashlib.sha256(bytes(data)).hexdigest()


class MailAttachment(models.Model):
    """A file on a Mail: either a link to an inject Attachment (no copy) or its own data."""
    mail = models.ForeignKey(Mail, on_delete=models.CASCADE, related_name="files")
    attachment = models.ForeignKey(Attachment, null=True, blank=True, on_delete=models.SET_NULL)
    filename = models.CharField(max_length=300)
    content_type = models.CharField(max_length=120, default="application/octet-stream")
    size = models.IntegerField(default=0)
    data = models.BinaryField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True, default="", db_index=True)  # of its own data (ops/ai_docs.py)

    def save(self, *args, **kwargs):
        if self.data is not None and not self.sha256:
            self.sha256 = file_hash(self.data)
        super().save(*args, **kwargs)

    def content(self):
        if self.data is not None:
            return bytes(self.data)
        return bytes(self.attachment.data) if self.attachment else None


class Response(models.Model):
    """A participant's message to a persona: an in-app reply/compose or a received email."""
    channel = models.CharField(max_length=10, default="inapp")  # inapp | email
    resend_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    mail = models.OneToOneField(Mail, null=True, blank=True, on_delete=models.SET_NULL, related_name="response")
    received_at = models.DateTimeField(default=timezone.now)
    from_addr = models.CharField(max_length=320, default="")
    from_name = models.CharField(max_length=200, blank=True, null=True)
    to = models.JSONField(default=list)
    cc = models.JSONField(default=list)
    subject = models.TextField(default="")
    text = models.TextField(blank=True, null=True)
    html = models.TextField(blank=True, null=True)
    headers = models.JSONField(null=True, blank=True)
    message_id_header = models.CharField(max_length=400, blank=True, null=True)
    persona = models.CharField(max_length=120, blank=True, null=True)
    reply_token = models.CharField(max_length=20, blank=True, null=True)
    message = models.ForeignKey(Message, null=True, blank=True, on_delete=models.SET_NULL, related_name="responses")
    inject = models.ForeignKey(Inject, null=True, blank=True, on_delete=models.SET_NULL, related_name="responses")
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL)
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL)
    match = models.CharField(max_length=10, default="none")  # exact | token | guess | manual | leader | none
    kind = models.CharField(max_length=10, default="live")  # live | test
    status = models.CharField(max_length=10, default="new")  # new | reviewed | ignored
    fetched = models.BooleanField(default=False)
    error = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["received_at", "id"]


class ResponseAttachment(models.Model):
    response = models.ForeignKey(Response, on_delete=models.CASCADE, related_name="attachments")
    filename = models.CharField(max_length=300)
    content_type = models.CharField(max_length=120, default="application/octet-stream")
    size = models.IntegerField(default=0)
    data = models.BinaryField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True, default="", db_index=True)

    def save(self, *args, **kwargs):
        if self.data is not None and not self.sha256:
            self.sha256 = file_hash(self.data)
        super().save(*args, **kwargs)


class RubricCriterion(models.Model):
    inject = models.ForeignKey(Inject, on_delete=models.CASCADE, related_name="rubric")
    label = models.CharField(max_length=200)
    description = models.TextField(blank=True, null=True)
    max_points = models.IntegerField(default=3)
    sort = models.IntegerField(default=0)
    critical = models.BooleanField(default=False)  # a must-have point: a low score is a critical miss (reports)

    class Meta:
        ordering = ["sort", "id"]


class Grade(models.Model):
    inject = models.ForeignKey(Inject, on_delete=models.CASCADE)
    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    scores = models.JSONField(default=dict)  # criterion id (str) -> points
    comment = models.TextField(blank=True, null=True)
    grader = models.CharField(max_length=120, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("inject", "team")]

    def total(self):
        return sum(float(v) for v in (self.scores or {}).values() if v not in (None, ""))


class ImportBatch(models.Model):
    filename = models.CharField(max_length=300)
    payload = models.JSONField()
    applied = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)


class Audit(models.Model):
    at = models.DateTimeField(default=timezone.now)
    user = models.CharField(max_length=150, blank=True, default="")
    action = models.CharField(max_length=60)
    detail = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["-id"]


def audit(action, detail=None, user=""):
    Audit.objects.create(action=action, detail=detail, user=str(user or ""))


# --- Claude analysis (ops/ai.py). Suggestions for ExCon only: never shown to participants,
# never counted in Grade totals. ------------------------------------------------------------

class ReplyAnalysis(models.Model):
    """One participant message read by the fast model: summary, tone, quality, flags."""
    response = models.OneToOneField(Response, on_delete=models.CASCADE, related_name="analysis")
    status = models.CharField(max_length=10, default="done")  # queued | done | failed
    model = models.CharField(max_length=120, default="")
    prompt_version = models.IntegerField(default=0)
    input_hash = models.CharField(max_length=64, default="")
    attempts = models.IntegerField(default=0)
    error = models.TextField(blank=True, null=True)
    summary = models.TextField(blank=True, default="")
    sentiment = models.IntegerField(null=True, blank=True)  # -2 (very negative) .. 2 (very positive)
    tone = models.CharField(max_length=20, blank=True, default="")  # calm | urgent | stressed | ...
    clarity = models.IntegerField(null=True, blank=True)  # 1..5
    completeness = models.IntegerField(null=True, blank=True)
    actionability = models.IntegerField(null=True, blank=True)
    asks = models.JSONField(default=list, blank=True)  # what the team requests
    commitments = models.JSONField(default=list, blank=True)  # decisions / actions it commits to
    facts = models.JSONField(default=list, blank=True)  # figures, places, times it states
    flags = models.JSONField(default=list, blank=True)  # see ai.FLAGS
    ai_written = models.IntegerField(null=True, blank=True)  # 0..100, the model's estimate
    ai_signals = models.TextField(blank=True, default="")
    language = models.CharField(max_length=20, blank=True, default="")
    words = models.IntegerField(default=0)
    wpm = models.FloatField(null=True, blank=True)  # words per minute since the team first read the inject
    updated_at = models.DateTimeField(auto_now=True)


class AiGrade(models.Model):
    """The deep model's suggested rubric scores for one inject x team thread."""
    inject = models.ForeignKey(Inject, on_delete=models.CASCADE, related_name="+")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="+")
    status = models.CharField(max_length=10, default="done")  # queued | done | failed
    model = models.CharField(max_length=120, default="")
    prompt_version = models.IntegerField(default=0)
    input_hash = models.CharField(max_length=64, default="")
    attempts = models.IntegerField(default=0)
    error = models.TextField(blank=True, null=True)
    scores = models.JSONField(default=dict, blank=True)  # criterion id (str) -> points
    notes = models.JSONField(default=dict, blank=True)  # criterion id (str) -> {"rationale", "evidence"}
    overall = models.TextField(blank=True, default="")
    strengths = models.JSONField(default=list, blank=True)
    gaps = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("inject", "team")]

    def total(self):
        return sum(float(v) for v in (self.scores or {}).values() if v not in (None, ""))


class InjectReview(models.Model):
    """The fast model's read of an unsent inject before it goes out: what looks missing, unfilled
    or inconsistent. ExCon-only; dismissed until the inject changes."""
    inject = models.OneToOneField(Inject, on_delete=models.CASCADE, related_name="review")
    status = models.CharField(max_length=10, default="queued")  # queued | done | failed
    model = models.CharField(max_length=120, default="")
    prompt_version = models.IntegerField(default=0)
    input_hash = models.CharField(max_length=64, default="")
    attempts = models.IntegerField(default=0)
    error = models.TextField(blank=True, null=True)
    issues = models.JSONField(default=list, blank=True)  # [{"severity": fix|check, "kind": ..., "detail": ...}]
    dismissed = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)


class DocumentAnalysis(models.Model):
    """A file a team sent (attached by a member or the leader, in SIMEX Mail or by email) read
    by the model (ops/ai_docs.py): what it is and an estimate of how likely it was written with
    an AI assistant. One per team and file content (sha256): the copies of a file in several
    mailboxes, and a file sent again, are read once. ExCon and the team's leader only."""
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="+")
    sha256 = models.CharField(max_length=64)
    filename = models.CharField(max_length=300, default="")
    content_type = models.CharField(max_length=120, default="")
    size = models.IntegerField(default=0)
    kind = models.CharField(max_length=10, default="live")  # live | test (the Test team)
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")  # who first sent it
    mail_file = models.ForeignKey(MailAttachment, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    response_file = models.ForeignKey(ResponseAttachment, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    response = models.ForeignKey(Response, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")  # its covering message
    inject = models.ForeignKey(Inject, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    sent_at = models.DateTimeField(default=timezone.now)  # when the team first sent it
    status = models.CharField(max_length=10, default="queued")  # queued | done | failed | skipped (not readable)
    model = models.CharField(max_length=120, default="")
    prompt_version = models.IntegerField(default=0)
    attempts = models.IntegerField(default=0)
    error = models.TextField(blank=True, null=True)
    facts = models.JSONField(default=dict, blank=True)  # from the file itself (ops/docscan.py): app, author, editing time...
    words = models.IntegerField(default=0)
    pages = models.IntegerField(null=True, blank=True)
    summary = models.TextField(blank=True, default="")
    doc_type = models.CharField(max_length=60, blank=True, default="")
    language = models.CharField(max_length=20, blank=True, default="")
    answers_inject = models.CharField(max_length=10, blank=True, default="")  # yes | partly | no | unknown
    ai_written = models.IntegerField(null=True, blank=True)  # 0..100, the model's estimate
    ai_signals = models.TextField(blank=True, default="")
    passages = models.JSONField(default=list, blank=True)  # [{"quote", "why"}]: the parts that read most like AI
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("team", "sha256")]
        ordering = ["-sent_at", "-id"]

    def file(self):
        """A stored copy of the file (any of the team's: the first one may have been cleared)."""
        if self.mail_file_id and self.mail_file and self.mail_file.data is not None:
            return self.mail_file
        if self.response_file_id and self.response_file and self.response_file.data is not None:
            return self.response_file
        return (MailAttachment.objects.filter(sha256=self.sha256).first()
                or ResponseAttachment.objects.filter(sha256=self.sha256).first())


class AiReport(models.Model):
    """An exercise-wide write-up (Dashboard → Exercise summary)."""
    kind = models.CharField(max_length=10, default="live")  # live | test
    status = models.CharField(max_length=10, default="running")  # running | done | failed
    model = models.CharField(max_length=120, default="")
    body = models.JSONField(default=dict)
    error = models.TextField(blank=True, null=True)
    user = models.CharField(max_length=150, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)


class ExerciseReport(models.Model):
    """One compile of the end-of-exercise reports (Dashboard → Reports, ops/report.py): a
    Report for each team and one for ExCon."""
    kind = models.CharField(max_length=10, default="live")  # live | test
    include_ai_written = models.BooleanField(default=False)  # the AI-written estimate in the team reports
    user = models.CharField(max_length=150, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]


class Report(models.Model):
    """A team's report (written for the team, read at the debrief) or, with no team, ExCon's.
    figures are computed in code and kept as they were when it was compiled; body is
    Claude's write-up; sections is what is shown and printed, ExCon's edits included."""
    exercise = models.ForeignKey(ExerciseReport, on_delete=models.CASCADE, related_name="reports")
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=10, default="queued")  # queued | running | done | failed
    model = models.CharField(max_length=120, default="")
    figures = models.JSONField(default=dict)
    body = models.JSONField(default=dict)
    sections = models.JSONField(default=dict)  # section key -> Markdown
    edited = models.JSONField(default=list)  # section keys ExCon changed
    error = models.TextField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["team__sort", "team__name"]


class InjectDraft(models.Model):
    """A message Claude drafted on ExCon's request (Injects → Draft with AI): a new inject, a
    follow-up in a delivered inject's conversation, or a persona's answer to a reply. Nothing
    is sent from here: ExCon turns it into a draft inject or copies it into the follow-up or
    answer form."""
    mode = models.CharField(max_length=10, default="new")  # new | followup | answer
    prompt = models.TextField(blank=True, default="")
    inject = models.ForeignKey(Inject, null=True, blank=True, on_delete=models.SET_NULL, related_name="ai_drafts")
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL)
    response = models.ForeignKey(Response, null=True, blank=True, on_delete=models.SET_NULL)
    previous = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL)  # the draft this revises
    status = models.CharField(max_length=10, default="running")  # running | done | failed
    model = models.CharField(max_length=120, default="")
    body = models.JSONField(default=dict)
    error = models.TextField(blank=True, null=True)
    created = models.ForeignKey(Inject, null=True, blank=True, on_delete=models.SET_NULL, related_name="drafted_from")
    user = models.CharField(max_length=150, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)


class AiCall(models.Model):
    """Every model call, for the spend cap and the Settings usage line."""
    at = models.DateTimeField(default=timezone.now)
    purpose = models.CharField(max_length=10)  # reply | grade | summary | inject | draft
    ref = models.CharField(max_length=40, blank=True, default="")
    model = models.CharField(max_length=120, default="")
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    cache_read_tokens = models.IntegerField(default=0)
    cost_usd = models.FloatField(default=0)
    ok = models.BooleanField(default=True)
    error = models.TextField(blank=True, null=True)


class AttachmentImport(models.Model):
    """A zip of inject documents being matched to injects, in a thread of the web process
    (ops/documents.py) so a large upload doesn't hold up a request."""
    filename = models.CharField(max_length=300)
    user = models.CharField(max_length=150, blank=True, default="")
    status = models.CharField(max_length=10, default="running")  # running | done | failed
    total = models.IntegerField(default=0)  # injects to look at
    done = models.IntegerField(default=0)
    attached = models.IntegerField(default=0)
    unused = models.JSONField(default=list)  # files in the zip that matched no inject
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]

    @property
    def interrupted(self):
        """Still 'running' but no progress for a while: the process was restarted (a deploy)."""
        return self.status == "running" and self.updated_at < timezone.now() - timedelta(minutes=5)

    @property
    def active(self):
        return self.status == "running" and not self.interrupted


class Notice(models.Model):
    """An update in ExCon's Notifications panel (ops/notices.py): a reply, an AI flag, an inject
    that failed or went overdue, a background job that finished, the scheduler stopping. One per
    event (key); read and dismissed by each ExCon user on their own (NoticeState)."""
    key = models.CharField(max_length=120, unique=True)  # e.g. reply:12, overdue:4:<send_at>
    kind = models.CharField(max_length=10)  # reply | flag | delivery | system
    title = models.CharField(max_length=300)
    body = models.TextField(blank=True, default="")
    note = models.TextField(blank=True, default="")  # a muted line: a reply's opening words
    url = models.CharField(max_length=300, blank=True, default="")
    test = models.BooleanField(default=False)  # about the Test team
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # gone with what it is about (a cleared inbox, a deleted inject)
    response = models.ForeignKey(Response, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    inject = models.ForeignKey(Inject, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]


class NoticeState(models.Model):
    """What one ExCon user has done with a Notice."""
    notice = models.ForeignKey(Notice, on_delete=models.CASCADE, related_name="states")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    read_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["notice", "user"], name="notice_state_once")]


# --- AI personas (ops/npc.py): personas whose answers Claude writes in character, sent after a
# delay drawn from their timing. ------------------------------------------------------------

class NpcProfile(models.Model):
    """The character behind a persona (Sender): who they are, what they know and may share
    with the teams, how they write and how fast they answer. Only what the teams may learn
    goes here, never control-side material."""
    sender = models.OneToOneField(Sender, on_delete=models.CASCADE, related_name="npc")
    enabled = models.BooleanField(default=False)  # answers on its own
    organisation = models.CharField(max_length=200, blank=True, default="")
    role = models.CharField(max_length=200, blank=True, default="")
    personality = models.TextField(blank=True, default="")
    knowledge = models.TextField(blank=True, default="")
    boundaries = models.TextField(blank=True, default="")
    timing = models.CharField(max_length=20, default="responsive")  # a key of npc.TIMINGS, or custom
    min_minutes = models.FloatField(default=5)
    max_minutes = models.FloatField(default=20)
    quick_chance = models.FloatField(default=0)  # 0..1: answers within a few minutes instead
    no_reply_chance = models.FloatField(default=0)  # 0..1: doesn't answer at all
    office_from = models.IntegerField(null=True, blank=True)  # hour, exercise time; empty = any time
    office_to = models.IntegerField(null=True, blank=True)
    auto_send = models.BooleanField(default=True)  # else each answer waits for ExCon
    # Generate with AI: the brief, and the run filling this profile in (a web-process thread).
    brief = models.TextField(blank=True, default="")
    gen_status = models.CharField(max_length=10, blank=True, default="")  # "" | running | done | failed
    gen_error = models.TextField(blank=True, default="")
    gen_notes = models.JSONField(default=dict, blank=True)  # rationale, new facts to check
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sender__display_name", "sender__local_part"]


class NpcReply(models.Model):
    """An AI persona's answer to a team's conversation: due at a time drawn on arrival, written
    when due, then sent (or held for ExCon). Messages that arrive while it is queued in the same
    conversation move it on to the latest (one answer to all of them)."""
    npc = models.ForeignKey(NpcProfile, on_delete=models.CASCADE, related_name="replies")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="+")
    response = models.ForeignKey(Response, on_delete=models.CASCADE, related_name="npc_replies")  # the latest it answers
    thread_key = models.CharField(max_length=300, default="")
    status = models.CharField(max_length=10, default="queued")  # queued | writing | held | sent | skipped | failed
    due_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=300, blank=True, default="")  # why skipped, held...
    body = models.TextField(blank=True, default="")
    out = models.JSONField(default=dict, blank=True)  # shared, flags, note for ExCon
    model = models.CharField(max_length=120, default="")
    attempts = models.IntegerField(default=0)
    error = models.TextField(blank=True, null=True)
    message = models.ForeignKey(Message, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]


class NpcSandbox(models.Model):
    """ExCon trying an AI persona out (its page → Try it): a conversation nothing delivers."""
    npc = models.ForeignKey(NpcProfile, on_delete=models.CASCADE, related_name="sandboxes")
    user = models.CharField(max_length=150, blank=True, default="")
    turns = models.JSONField(default=list)  # [{"who": "team" | "npc", "text": ...}]
    status = models.CharField(max_length=10, default="done")  # running | done | failed
    error = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)


# --- role play (ops/roleplay.py) ----------------------------------------------------------------
# Control-side by nature (scripts, expected outcomes): read only by ExCon (views_roleplay.py), by
# the role players assigned (views_rp.py) and by Suggest a reply (ops/suggest.py, its notes for
# the persona desk and ExCon only), never by participants' or leaders' mail views, the other AI
# readings, the AI personas or the reports (tests/test_roleplay.py scans those modules).

RP_STATUSES = {"pending": "Pending", "ready": "In position", "done": "Done", "skipped": "Skipped"}


class RolePlay(models.Model):
    """One event of the role-play schedule (a MEL serial: 1-25 Immigration), played by role
    players (or team leaders) at one or more sittings (slots)."""
    key = models.CharField(max_length=40, unique=True)  # the serial; -b, -c for more rows of one serial
    serial = models.CharField(max_length=40, default="")
    seq = models.IntegerField(default=0)
    day = models.CharField(max_length=40, blank=True, default="")  # as the sheet says: "Monday"
    time_text = models.CharField(max_length=200, blank=True, default="")  # "1440 - A", "18:00 + 18:20"
    duration = models.CharField(max_length=120, blank=True, default="")  # "10 min max per team"
    title = models.TextField(blank=True, default="")  # the sheet's Description / Event
    role = models.CharField(max_length=200, blank=True, default="")  # "Immigration officers x 2"
    players_text = models.CharField(max_length=300, blank=True, default="")  # the sheet's Role player cell
    location = models.CharField(max_length=200, blank=True, default="")
    method = models.CharField(max_length=60, blank=True, default="")  # F2F | Phone | VTC...
    script = models.TextField(blank=True, default="")
    outcomes = models.TextField(blank=True, default="")
    documents = models.TextField(blank=True, default="")  # to bring / print, one per line
    notes = models.TextField(blank=True, default="")  # ExCon's notes and FAQ for this event
    optional = models.BooleanField(default=False)
    players = models.ManyToManyField(Person, blank=True, related_name="role_plays")
    imported = models.JSONField(null=True, blank=True)  # last imported values (local edits kept)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["seq", "key"]

    def __str__(self):
        return f"{self.serial or self.key} {self.title[:40]}"


class RolePlaySlot(models.Model):
    """One sitting of a role play: a time, its length and the teams it is for (none = all
    teams together)."""
    play = models.ForeignKey(RolePlay, on_delete=models.CASCADE, related_name="slots")
    start = models.DateTimeField(null=True, blank=True)
    minutes = models.IntegerField(null=True, blank=True)
    teams = models.ManyToManyField(Team, blank=True, related_name="+")
    status = models.CharField(max_length=10, default="pending")  # see RP_STATUSES
    status_at = models.DateTimeField(null=True, blank=True)
    status_by = models.CharField(max_length=200, blank=True, default="")
    reminded = models.CharField(max_length=10, blank=True, default="")  # "" | soon | start
    changed_at = models.DateTimeField(null=True, blank=True)  # moved or reassigned (players told)
    was_start = models.DateTimeField(null=True, blank=True)  # the time before the last change
    imported_start = models.DateTimeField(null=True, blank=True)  # as last imported

    class Meta:
        ordering = ["start", "id"]

    def end(self):
        return self.start + timedelta(minutes=self.minutes or 0) if self.start else None


class RolePlayFile(models.Model):
    play = models.ForeignKey(RolePlay, on_delete=models.CASCADE, related_name="files")
    filename = models.CharField(max_length=300)
    content_type = models.CharField(max_length=120, default="application/octet-stream")
    size = models.IntegerField(default=0)
    data = models.BinaryField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["filename"]


class RolePlaySeen(models.Model):
    """A role player has seen their schedule since its last change (ExCon's board ticks them)."""
    person = models.OneToOneField(Person, on_delete=models.CASCADE, related_name="rp_seen")
    at = models.DateTimeField(default=timezone.now)


# --- the knowledge library and suggested answers (ops/kb.py, ops/suggest.py) -------------------

KB_RELEASES = {"all": "Participants have it", "with": "Released with an inject or role play", "no": "Not given to participants"}


class KbDocument(models.Model):
    """A participant-facing document from the exercise package (baseline documents, inject
    documents, maps), as text, exported from the knowledge base (../undac-simex,
    scripts/export_ops.py) and uploaded on ExCon's Library page. Searched when Claude suggests a
    persona's answer (ops/suggest.py), with whether the team has it yet: release "all" (the
    participants have it from the start), "with" (it comes with the inject or role play
    `serial`, so a team has it once that reached them) or "no" (not given, until ExCon says)."""
    key = models.CharField(max_length=40, unique=True)  # the knowledge base's document id
    path = models.CharField(max_length=500)
    title = models.CharField(max_length=300)
    category = models.CharField(max_length=40, blank=True, default="")
    release = models.CharField(max_length=10, default="no")  # see KB_RELEASES
    serial = models.CharField(max_length=40, blank=True, default="")
    imported_release = models.CharField(max_length=60, blank=True, default="")  # release|serial as last imported
    text = models.TextField(blank=True, default="")
    chars = models.IntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "path"]

    def __str__(self):
        return self.title


class ReplySuggestion(models.Model):
    """Claude's suggested answer to a team's message to a persona, for the persona desk or ExCon:
    an answer the team may see and notes for the desk (sources, what the team has and hasn't
    been given yet, what not to get ahead of). Nothing is sent from here; the desk copies the
    answer into its reply. The notes may quote role-play scripts and planned injects, so they
    are shown to the desk and ExCon only."""
    response = models.ForeignKey(Response, on_delete=models.CASCADE, related_name="suggestions")
    thread = models.CharField(max_length=40, blank=True, default="")  # the desk's conversation, if asked there
    prompt = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, default="running")  # running | done | failed
    model = models.CharField(max_length=120, default="")
    body = models.JSONField(default=dict)
    error = models.TextField(blank=True, null=True)
    steps = models.IntegerField(default=0)  # model calls
    user = models.CharField(max_length=150, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]

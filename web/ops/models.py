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
ROLES = {"member": "Team member", "tl": "Team leader (ExCon)"}
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
    for SIMEX Mail, an email for the email channel."""
    name = models.CharField(max_length=200)
    email = models.CharField(max_length=320, blank=True, default="")
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL, related_name="people")
    role = models.CharField(max_length=20, default="member")  # member | tl (see ROLES)
    team_role = models.ForeignKey(TeamRole, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="people")  # members only
    active = models.BooleanField(default=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="person")
    lead_seen = models.IntegerField(default=0)  # leader: the last team-inbox Mail id they looked at

    objects = PersonQuerySet.as_manager()

    class Meta:
        ordering = ["team__sort", "-role", "name"]

    @property
    def is_leader(self):
        return self.role == "tl"

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
    kind = models.CharField(max_length=10)  # live | test | oneoff | reply | followup | leader
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
    status = models.CharField(max_length=10, default="pending")  # pending | sent | failed | dry
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
    between a team and its leader has the same thread id in both."""
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="mails")
    owner = models.ForeignKey(Person, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    folder = models.CharField(max_length=10, default="inbox")  # inbox | sent
    thread = models.CharField(max_length=40, default="")
    from_name = models.CharField(max_length=200, default="")
    from_addr = models.CharField(max_length=320, default="")
    to_label = models.CharField(max_length=500, default="")  # shown in the To line
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
    # Mail-client state (per team): star, archive (out of the Inbox, still in All mail), trash.
    starred = models.BooleanField(default=False)
    archived = models.BooleanField(default=False)
    trashed_at = models.DateTimeField(null=True, blank=True)
    cc_label = models.CharField(max_length=500, blank=True, default="")
    # Address-book keys chosen in compose ({"to": ["p:cnn", "t:3"], "cc": [...], "bcc": [...]}),
    # kept on drafts and sent items.
    recipients = models.JSONField(default=dict, blank=True)
    forward_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["team", "folder", "thread"])]

    def save(self, *args, **kwargs):
        if not self.thread:
            self.thread = uuid.uuid4().hex
        super().save(*args, **kwargs)


class MailLabel(models.Model):
    """A mailbox's own label in SIMEX Mail (Gmail's "folders"): applied to whole conversations, which
    may carry several. "Move to" a label = label it and take it out of the Inbox."""
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="mail_labels")
    owner = models.ForeignKey(Person, null=True, blank=True, on_delete=models.CASCADE, related_name="+")  # as Mail.owner
    name = models.CharField(max_length=60)
    colour = models.CharField(max_length=7, blank=True, default="")  # one of views_inbox.LABEL_COLOURS
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name", "id"]
        constraints = [models.UniqueConstraint(fields=["team", "owner", "name"], name="mail_label_unique_name")]

    def __str__(self):
        return self.name


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


class MailAttachment(models.Model):
    """A file on a Mail: either a link to an inject Attachment (no copy) or its own data."""
    mail = models.ForeignKey(Mail, on_delete=models.CASCADE, related_name="files")
    attachment = models.ForeignKey(Attachment, null=True, blank=True, on_delete=models.SET_NULL)
    filename = models.CharField(max_length=300)
    content_type = models.CharField(max_length=120, default="application/octet-stream")
    size = models.IntegerField(default=0)
    data = models.BinaryField(null=True, blank=True)

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

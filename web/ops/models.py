"""Data model. Times are timezone-aware (stored UTC, shown in TIME_ZONE = Africa/Nairobi).

Two delivery channels share one pipeline: every inject delivery creates Message rows
(one per team / person / everyone, as for email). For the in-app channel each Message
also puts a Mail in the target team's simulated inbox; for the email channel it goes to
Resend. Participants' answers (in-app or by email) are Responses, which ExCon grades.
"""

import uuid

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
                 "delivery", "subject", "body", "supporting_documents"]


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


class Person(models.Model):
    """A participant. Has a login (user) for the in-app inbox, an email for the email channel."""
    name = models.CharField(max_length=200)
    email = models.CharField(max_length=320, blank=True, default="")
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL, related_name="people")
    role = models.CharField(max_length=20, default="member")  # member | tl
    active = models.BooleanField(default=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="person")

    class Meta:
        ordering = ["team__sort", "-role", "name"]

    def __str__(self):
        return self.name

    @property
    def first_name(self):
        return self.name.split()[0] if self.name.split() else self.name


class Sender(models.Model):
    local_part = models.CharField(max_length=120, unique=True)  # may contain {team}
    display_name = models.CharField(max_length=200, blank=True, default="")
    reply_to = models.CharField(max_length=320, blank=True, default="")  # email channel only
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["local_part"]

    def __str__(self):
        return f"{self.display_name} <{self.local_part}@{settings.MAIL_DOMAIN}>"


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
    kind = models.CharField(max_length=10)  # live | test | oneoff | reply
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
    thread = models.CharField(max_length=40, blank=True, default="")
    banner = models.TextField(blank=True, default="")
    body = models.TextField(blank=True, default="")  # in-app: text without footer/banner
    footer = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-id"]


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
    """An item in a team's simulated inbox (folder inbox) or sent folder (folder sent)."""
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="mails")
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


class PushSubscription(models.Model):
    """A participant's device that gets a push notification for new mail (Web Push: a phone's
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
    match = models.CharField(max_length=10, default="none")  # exact | token | guess | manual | none
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


class AiReport(models.Model):
    """An exercise-wide write-up (Dashboard → Exercise summary)."""
    kind = models.CharField(max_length=10, default="live")  # live | test
    status = models.CharField(max_length=10, default="running")  # running | done | failed
    model = models.CharField(max_length=120, default="")
    body = models.JSONField(default=dict)
    error = models.TextField(blank=True, null=True)
    user = models.CharField(max_length=150, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)


class AiCall(models.Model):
    """Every model call, for the spend cap and the Settings usage line."""
    at = models.DateTimeField(default=timezone.now)
    purpose = models.CharField(max_length=10)  # reply | grade | summary
    ref = models.CharField(max_length=40, blank=True, default="")
    model = models.CharField(max_length=120, default="")
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    cache_read_tokens = models.IntegerField(default=0)
    cost_usd = models.FloatField(default=0)
    ok = models.BooleanField(default=True)
    error = models.TextField(blank=True, null=True)

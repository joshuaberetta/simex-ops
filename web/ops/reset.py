"""Clearing test traffic before the exercise goes live: one team's (or every team's) mailboxes
(Team inboxes → Clear mailboxes) and the whole exercise's activity (Settings → Start afresh)."""

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import conf
from .models import (AiGrade, AiReport, ExerciseReport, Grade, Inject, Mail, MailAction, MailLabel, Message, Notice,
                     Person, Response, TestRun)

SCOPES = {"all": "Every mailbox: the team inbox, the leader's and each member's own",
          "members": "Members' own mailboxes only"}
# Injects that went out (or tried to) during testing; Start afresh puts them back on the schedule.
SENT_STATUSES = ("sent", "partial", "failed", "overdue")


def _members(team):
    return Person.objects.filter(team=team, role="member")


def clear_team(team, scope="all", deliveries=False):
    """Empty a team's mailboxes. scope "all": every mailbox of the team (the shared inbox, the
    leader's and each member's own: received, sent, drafts, labels, what was done in them), its
    replies with their AI analysis, the persona answers to them, its grades and the desk's copies;
    "members": the members' own mailboxes only (the replies, whose copy is in the team inbox,
    stay). deliveries: also the team's deliveries in the Log, with their read and download
    events, so its delivery and reading figures on the Dashboard start from zero.
    Returns a dict of counts."""
    n = {"mails": 0, "replies": 0, "answers": 0, "grades": 0, "deliveries": 0}
    with transaction.atomic():
        if scope == "members":
            owners = _members(team)
            mails = Mail.objects.filter(team=team, owner__in=owners)
            n["mails"] = mails.count()
            mails.delete()
            MailLabel.objects.filter(team=team, owner__in=owners).delete()
            MailAction.objects.filter(team=team, owner__in=owners).delete()
        else:
            reps = Response.objects.filter(team=team)
            n["answers"] = Message.objects.filter(kind="reply", response__in=reps).delete()[0]
            n["replies"] = reps.count()
            reps.delete()
            n["mails"] = Mail.objects.filter(team=team).count()
            Mail.objects.filter(team=team).delete()
            Mail.objects.filter(team=None, desk_team=team).delete()  # the persona desk's copies of its mail
            MailLabel.objects.filter(team=team).delete()
            MailAction.objects.filter(team=team).delete()
            n["grades"] = Grade.objects.filter(team=team).delete()[0] + AiGrade.objects.filter(team=team).delete()[0]
        if deliveries:
            msgs = Message.objects.filter(Q(team=team) | Q(inbox_team=team)).exclude(kind="login")
            if scope == "members":
                msgs = msgs.filter(inbox_owner__in=_members(team))
            n["deliveries"] = msgs.count()
            msgs.delete()  # with their Events
    return n


def start_afresh(reschedule=True):
    """Everything that happened in the exercise so far, gone, for every team: every mailbox
    (teams, leaders, members, the persona desk), replies, answers, grades, AI analyses, the
    exercise summary and reports, test runs, notifications, and every delivery in the Log with
    its read/download events (login emails are kept). reschedule: injects already sent (or
    failed, or overdue) go back to scheduled, or draft without a send time, so they go out
    live. Injects, attachments, rubrics, personas, people, logins, settings and the audit trail
    stay. Returns a dict of counts."""
    now = timezone.now()
    with transaction.atomic():
        n = {"mails": Mail.objects.count(), "replies": Response.objects.count(),
             "deliveries": Message.objects.exclude(kind="login").count(),
             "grades": Grade.objects.count(), "test_runs": TestRun.objects.count(), "injects": 0}
        Response.objects.all().delete()  # with attachments, AI analyses, queued AI persona answers, notices
        Mail.objects.all().delete()
        MailLabel.objects.all().delete()
        MailAction.objects.all().delete()
        Message.objects.exclude(kind="login").delete()  # with their Events
        Grade.objects.all().delete()
        AiGrade.objects.all().delete()
        AiReport.objects.all().delete()
        ExerciseReport.objects.all().delete()
        TestRun.objects.all().delete()
        Notice.objects.all().delete()
        if reschedule:
            sent = Inject.objects.filter(status__in=SENT_STATUSES)
            n["injects"] = sent.count()
            sent.filter(send_at__isnull=False).update(status="scheduled", attempt=0, sent_at=None, last_error=None)
            sent.filter(send_at__isnull=True).update(status="draft", attempt=0, sent_at=None, last_error=None)
        # Received email from before now must not come back with the next Sync (inbound.sync).
        conf.put("reset_at", now.isoformat())
    return n

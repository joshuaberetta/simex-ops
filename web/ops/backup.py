"""Whole-database backup as one JSON file (Django serialization; binary data base64), and
a full restore. Includes user accounts, so participant logins survive a restore."""

import json

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import serializers
from django.db import connection, transaction
from django.utils import timezone

from . import models as m

FORMAT = "simex-inbox-backup-1"
# Dependency order for loading; deleted in reverse.
MODELS = [Group, get_user_model(), m.Setting, m.Team, m.TeamRole, m.Person, m.Sender, m.Inject, m.Attachment, m.TestRun,
          m.TestRunItem, m.Message, m.MessageAttachment, m.Event, m.Mail, m.MailAttachment, m.MailLabel, m.ThreadLabel, m.Response, m.ResponseAttachment, m.RubricCriterion,
          m.Grade, m.ImportBatch, m.Audit, m.ReplyAnalysis, m.AiGrade, m.AiReport, m.InjectDraft, m.AiCall,
          m.ExerciseReport, m.Report, m.MailAction, m.NpcProfile, m.NpcReply, m.NpcSandbox,
          m.RolePlay, m.RolePlaySlot, m.RolePlayFile, m.RolePlaySeen, m.LoginEvent, m.DocumentAnalysis, m.DeskConversation,
          m.KbDocument, m.ReplySuggestion, m.DeskWatch, m.DeskDefault,
          m.CalendarEvent, m.CalendarAttendee]


# Rows serialized at a time: few for tables holding files, so a large exercise's backup (hundreds
# of MB) is streamed out instead of built in memory (the web instance has 1 GB).
BATCH, FILE_BATCH = 500, 5
FILE_MODELS = {m.Attachment, m.MessageAttachment, m.MailAttachment, m.ResponseAttachment, m.RolePlayFile}


def export_stream():
    """The backup as chunks of JSON text, the same file as export() gives."""
    yield json.dumps({"format": FORMAT, "exported_at": timezone.now().isoformat()})[:-1] + ', "objects": ['
    first = True
    for model in MODELS:
        size = FILE_BATCH if model in FILE_MODELS else BATCH
        pks = list(model.objects.order_by("pk").values_list("pk", flat=True))
        for i in range(0, len(pks), size):
            text = serializers.serialize("json", model.objects.filter(pk__in=pks[i:i + size]).order_by("pk"))[1:-1]
            if text.strip():
                yield ("" if first else ", ") + text
                first = False
    yield "]}"


def export():
    return json.loads("".join(export_stream()))


def restore(data):
    if data.get("format") != FORMAT:
        raise ValueError("Not a simex-inbox backup file.")
    payload = json.dumps(data["objects"])
    objs = list(serializers.deserialize("json", payload))  # parse fully before deleting anything
    with transaction.atomic():
        # Message.response and Response.message point at each other: break the cycle first.
        m.Message.objects.update(response=None)
        for model in reversed(MODELS):
            model.objects.all().delete()
        with connection.constraint_checks_disabled():
            for o in objs:
                o.save()
        connection.check_constraints()
        if connection.vendor == "postgresql":
            from django.core.management.color import no_style
            for sql in connection.ops.sequence_reset_sql(no_style(), MODELS):
                with connection.cursor() as c:
                    c.execute(sql)
        m.Setting.objects.update_or_create(key="live_armed", defaults={"value": "0"})  # never come back armed
    counts = {}
    for o in objs:
        counts[o.object._meta.model_name] = counts.get(o.object._meta.model_name, 0) + 1
    m.audit("restore", json.dumps(counts))
    return counts

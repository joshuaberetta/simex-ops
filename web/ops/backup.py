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
MODELS = [Group, get_user_model(), m.Setting, m.Team, m.Person, m.Sender, m.Inject, m.Attachment, m.TestRun,
          m.TestRunItem, m.Message, m.Mail, m.MailAttachment, m.MailLabel, m.ThreadLabel, m.Response, m.ResponseAttachment, m.RubricCriterion,
          m.Grade, m.ImportBatch, m.Audit, m.ReplyAnalysis, m.AiGrade, m.AiReport, m.InjectDraft, m.AiCall]


def export():
    return {"format": FORMAT, "exported_at": timezone.now().isoformat(),
            "objects": json.loads(serializers.serialize("json", [o for model in MODELS
                                                                 for o in model.objects.all().order_by("pk")]))}


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

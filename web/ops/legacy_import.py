"""Bring the configuration of the first (FastAPI) simex-ops app into this one, from its
Settings > Download backup file (format "simex-ops-backup-1"): settings, senders, teams,
people, injects with attachments, rubrics and grades. Logs, sent emails and test runs stay
behind. Existing rows are updated by natural key (sender username, team code, person email,
inject id), so it can be run again."""

import base64
from datetime import datetime, timezone as dt_tz

from django.db import transaction

from . import conf
from . import models as m

FORMAT = "simex-ops-backup-1"
INJECT_FIELDS = ["seq", "method", "mailbox", "scenario_dt", "status", "sender_local", "from_name", "reply_to",
                 "delivery", "to_extra", "cc", "bcc", "recipients_label", "subject", "body", "footer_mode", "footer",
                 "supporting_documents", "imported", "attempt", "last_error", "expects_reply", "reply_due_minutes"]
SETTINGS = ["footer", "subject_prefix", "default_reply_to", "excon_bcc", "test_address", "test_redirect",
            "grace_minutes"]


def _dt(v):
    return datetime.fromisoformat(v).replace(tzinfo=dt_tz.utc) if v else None


@transaction.atomic
def import_backup(data):
    if data.get("format") != FORMAT:
        raise ValueError("Not a backup from the email app (simex-ops-backup-1).")
    t = data["tables"]
    counts = {}
    for row in t.get("settings", []):
        if row["key"] in SETTINGS and row.get("value") is not None:
            conf.put(row["key"], row["value"])
    for row in t.get("senders", []):
        m.Sender.objects.update_or_create(local_part=row["local_part"], defaults={
            "display_name": row.get("display_name") or "", "reply_to": row.get("reply_to") or "",
            "active": row.get("active", True)})
    counts["senders"] = len(t.get("senders", []))
    team_map = {}
    for row in t.get("teams", []):
        team, _ = m.Team.objects.update_or_create(code=row["code"], defaults={
            "name": row["name"], "email": row.get("email") or "", "sort": row.get("sort") or 0,
            "active": row.get("active", True)})
        team_map[row["id"]] = team
    counts["teams"] = len(team_map)
    people = t.get("people", [])
    if any(not p["email"].endswith("placeholder.invalid") for p in people):
        # A real roster replaces this app's placeholders (only those without a login).
        m.Person.objects.filter(email__endswith="placeholder.invalid", user__isnull=True).delete()
    for row in people:
        team = team_map.get(row.get("team_id"))
        existing = m.Person.objects.filter(email__iexact=row["email"]).first() if row.get("email") else None
        p = existing or m.Person(email=row["email"])
        # The first app's "tl" was a participant leading their team: here a team member.
        p.name, p.team, p.role, p.active = row["name"], team, "member", row.get("active", True)
        p.save()
    counts["people"] = len(people)
    inj_map = {}
    for row in t.get("injects", []):
        inj = m.Inject.objects.filter(mel_id=row["mel_id"]).first() or m.Inject(mel_id=row["mel_id"])
        for f in INJECT_FIELDS:
            if f in row:
                setattr(inj, f, row[f])
        if inj.status == "sending":
            inj.status = "scheduled"
        inj.send_at, inj.sent_at = _dt(row.get("send_at")), _dt(row.get("sent_at"))
        inj.team_ids = [team_map[i].id for i in (row.get("team_ids") or []) if i in team_map] or None
        inj.channel = "email"  # it came from the email app; the global switch decides anyway
        inj.save()
        inj_map[row["id"]] = inj
    counts["injects"] = len(inj_map)
    atts = [a for a in t.get("attachments", []) if a.get("inject_id") in inj_map]
    for inj in {inj_map[a["inject_id"]] for a in atts}:
        inj.attachments.all().delete()
    for a in atts:
        data_ = base64.b64decode(a["data"]) if a.get("data") else b""
        m.Attachment.objects.create(inject=inj_map[a["inject_id"]], filename=a["filename"],
                                    content_type=a.get("content_type") or "application/octet-stream",
                                    size=len(data_), data=data_)
    counts["attachments"] = len(atts)
    crit_map = {}
    for row in t.get("rubric", []):
        inj = inj_map.get(row["inject_id"])
        if inj:
            c, _ = m.RubricCriterion.objects.update_or_create(inject=inj, label=row["label"], defaults={
                "description": row.get("description"), "max_points": row.get("max_points") or 3,
                "sort": row.get("sort") or 0})
            crit_map[str(row["id"])] = str(c.id)
    counts["rubric"] = len(crit_map)
    for row in t.get("grades", []):
        inj, team = inj_map.get(row["inject_id"]), team_map.get(row["team_id"])
        if inj and team:
            m.Grade.objects.update_or_create(inject=inj, team=team, defaults={
                "scores": {crit_map.get(k, k): v for k, v in (row.get("scores") or {}).items()},
                "comment": row.get("comment"), "grader": row.get("grader")})
    counts["grades"] = len(t.get("grades", []))
    conf.put("live_armed", "0")
    m.audit("import_email_app_backup", str(counts))
    return counts

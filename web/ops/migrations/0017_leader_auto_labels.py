from django.conf import settings
from django.db import migrations, models

LABELS = {"team": ("Team account", "#4a86e8"), "member": ("Personal account", "#16a766")}


def label_existing(apps, schema_editor):
    """Leaders' mailboxes: label what their members already wrote to them (views_inbox.auto_label)."""
    Mail, MailLabel, ThreadLabel = apps.get_model("ops", "Mail"), apps.get_model("ops", "MailLabel"), apps.get_model("ops", "ThreadLabel")
    domain = f"teams.{settings.MAIL_DOMAIN}".lower()
    for m in Mail.objects.filter(owner__role="tl", folder="inbox", author__role="member"):
        addr = (m.from_addr or "").lower()
        kind = "team" if addr.endswith("@" + domain) else "member" if addr.endswith("." + domain) else None
        if not kind:
            continue
        name, colour = LABELS[kind]
        label = (MailLabel.objects.filter(team_id=m.team_id, owner_id=m.owner_id, auto=kind).first()
                 or MailLabel.objects.filter(team_id=m.team_id, owner_id=m.owner_id, name__iexact=name).first()
                 or MailLabel.objects.create(team_id=m.team_id, owner_id=m.owner_id, name=name, colour=colour, auto=kind))
        if label.auto != kind:
            MailLabel.objects.filter(pk=label.pk).update(auto=kind)
        ThreadLabel.objects.get_or_create(label=label, thread=m.thread)


class Migration(migrations.Migration):

    dependencies = [
        ('ops', '0016_team_roles'),
    ]

    operations = [
        migrations.AddField(
            model_name='maillabel',
            name='auto',
            field=models.CharField(blank=True, default='', max_length=10),
        ),
        migrations.RunPython(label_existing, migrations.RunPython.noop),
    ]

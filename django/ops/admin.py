"""Django admin: a secondary tool for ExCon (raw records). The custom tabs are the main UI."""

from django.contrib import admin

from . import models as m

for model in (m.Team, m.Person, m.Sender, m.Setting, m.TestRun, m.RubricCriterion, m.Grade, m.Audit):
    admin.site.register(model)


@admin.register(m.Inject)
class InjectAdmin(admin.ModelAdmin):
    list_display = ("mel_id", "send_at", "status", "channel", "sender_local", "subject")
    list_filter = ("status", "channel")
    search_fields = ("mel_id", "subject")


@admin.register(m.Mail)
class MailAdmin(admin.ModelAdmin):
    list_display = ("created_at", "team", "folder", "from_name", "subject", "read_at")
    list_filter = ("team", "folder")


@admin.register(m.Response)
class ResponseAdmin(admin.ModelAdmin):
    list_display = ("received_at", "channel", "team", "persona", "inject", "match", "status")
    list_filter = ("channel", "status", "match")


@admin.register(m.Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "channel", "target", "subject", "status")
    list_filter = ("kind", "channel", "status")

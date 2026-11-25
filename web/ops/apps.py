from django.apps import AppConfig


class OpsConfig(AppConfig):
    name = "ops"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import activity  # noqa: F401  (auth signals: sign-in log)
        from . import reqcache
        reqcache.connect()  # a request's memo empties when a Setting, Sender, Team or Person changes

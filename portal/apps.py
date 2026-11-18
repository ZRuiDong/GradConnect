from django.apps import AppConfig

class PortalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "portal"
    verbose_name = "研究生事务"

    def ready(self):
        from . import signals


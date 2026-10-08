from django.apps import AppConfig


class PushDataConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.push_data"
    verbose_name = "push-notification-service (read-only)"

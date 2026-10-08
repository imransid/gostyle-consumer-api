from django.apps import AppConfig


class PlatformDataConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.platform_data'
    verbose_name = 'gostyle-platform (read-only)'
from django.apps import AppConfig


class ReviewDataConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.review_data"
    verbose_name = "review-service (read-only)"

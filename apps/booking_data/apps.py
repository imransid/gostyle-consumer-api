from django.apps import AppConfig


class BookingDataConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.booking_data"
    verbose_name = "booking-api (read-only)"

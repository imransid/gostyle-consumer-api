from .base import *  # noqa

DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

# Print OTP emails to the console instead of sending them in local dev.
# EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# No Redis dependency for local dev or the test runner: the OTP rate limits and
# throttles run against an in-process cache instead.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# Log OTP codes to the console rather than calling WhatsApp or SMTP.
# OTP_SENDER = "console"

# JSON log lines are for Loki, not for a human reading a terminal. Set
# DJANGO_LOG_FORMAT=json locally to see exactly what production emits.
LOGGING["handlers"]["console"]["formatter"] = env(  # noqa: F405
    "DJANGO_LOG_FORMAT", default="plain"
)

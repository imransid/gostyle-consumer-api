from .base import *  # noqa

DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

# Print OTP emails to the console instead of sending them in local dev.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# No Redis dependency for local dev or the test runner: the OTP rate limits and
# throttles run against an in-process cache instead.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# Log OTP codes to the console rather than calling WhatsApp or SMTP.
OTP_SENDER = "console"

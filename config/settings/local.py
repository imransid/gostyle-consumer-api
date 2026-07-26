from .base import *  # noqa

DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

# Print OTP emails to the console instead of sending them in local dev.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
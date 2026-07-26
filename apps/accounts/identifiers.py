import re

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email

PHONE_RE = re.compile(r"^\+?[1-9]\d{7,14}$")

PHONE = "phone"
EMAIL = "email"


class InvalidIdentifier(ValueError):
    """Raised when a value is neither a valid phone number nor email."""


def normalize_phone(value):
    cleaned = re.sub(r"[\s\-()]", "", value or "")
    if not PHONE_RE.match(cleaned):
        raise InvalidIdentifier("Enter a valid phone number.")
    return cleaned if cleaned.startswith("+") else f"+{cleaned}"


def normalize_email(value):
    cleaned = (value or "").strip().lower()
    try:
        validate_email(cleaned)
    except DjangoValidationError:
        raise InvalidIdentifier("Enter a valid email address.")
    return cleaned


def resolve_identifier(value):
    """Return (kind, normalized_value) for an email-or-phone identifier.

    Login and registration accept a single "Email/Phone Number" field, so this
    is the one place that decides which channel a value belongs to.
    """
    raw = (value or "").strip()
    if "@" in raw:
        return EMAIL, normalize_email(raw)
    return PHONE, normalize_phone(raw)


def channel_for_kind(kind):
    """Delivery channel for an identifier kind. Phone defaults to SMS."""
    return "email" if kind == EMAIL else "sms"


def mask_destination(kind, value):
    """Mask a destination for display, e.g. k***@gmail.com / +8801***678."""
    if kind == EMAIL:
        local, _, domain = value.partition("@")
        head = local[0] if local else ""
        return f"{head}***@{domain}"
    # phone: keep the first 3 chars (e.g. "+88") and last 3 digits, mask the rest.
    if len(value) <= 6:
        return value
    return f"{value[:3]}{'*' * (len(value) - 6)}{value[-3:]}"

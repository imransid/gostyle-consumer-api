import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberType, number_type

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email

PHONE = "phone"
EMAIL = "email"

# Region used to interpret national-format phone numbers (e.g. "01712345678").
# Numbers already in E.164 ("+8801712345678") parse regardless of this.
DEFAULT_REGION = "BD"

# A registerable phone must be reachable on a mobile handset. libphonenumber
# reports FIXED_LINE_OR_MOBILE for ranges it cannot split into fixed vs mobile,
# so we accept it alongside a definite MOBILE.
_MOBILE_TYPES = frozenset({PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE})


class InvalidIdentifier(ValueError):
    """Raised when a value is not a valid identifier for its channel."""


def _normalize_phone(raw, region):
    # Parse against a region so a plus-less national number resolves to the
    # right country. We never prepend "+" ourselves: "1712345678" with region
    # BD must become "+8801712345678", not "+1712345678".
    try:
        parsed = phonenumbers.parse(raw or "", region)
    except phonenumbers.NumberParseException:
        raise InvalidIdentifier("Enter a valid phone number.")

    if not phonenumbers.is_valid_number(parsed):
        raise InvalidIdentifier("Enter a valid phone number.")

    if number_type(parsed) not in _MOBILE_TYPES:
        raise InvalidIdentifier("Enter a valid mobile phone number.")

    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)


def _normalize_email(raw):
    cleaned = (raw or "").strip().lower()
    try:
        validate_email(cleaned)
    except DjangoValidationError:
        raise InvalidIdentifier("Enter a valid email address.")
    return cleaned


def normalize_identifier(raw, channel, region=DEFAULT_REGION):
    """Normalize a phone/email identifier to its canonical stored form.

    Phone -> E.164 via libphonenumber (validated, mobile-capable).
    Email -> trimmed, lowercased, validated.
    Raises InvalidIdentifier on anything that does not pass.
    """
    if channel == PHONE:
        return _normalize_phone(raw, region)
    if channel == EMAIL:
        return _normalize_email(raw)
    raise InvalidIdentifier("Unknown channel.")

import phonenumbers

DEFAULT_REGION = "AE"


def to_e164(raw, region=DEFAULT_REGION):
    """Return +971XXXXXXXXX, or None if the number is not a real one."""
    if not raw:
        return None
    try:
        num = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
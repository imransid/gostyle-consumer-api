import secrets

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from .identifiers import EMAIL
from .models import ConsumerAccount, OtpCode
from .notifications import get_sender

RESEND_COOLDOWN_SECONDS = 60
OTP_TTL_SECONDS = 300


def find_account(kind, destination):
    """Look up an account by the identifier used to reach it."""
    if kind == EMAIL:
        return ConsumerAccount.objects.filter(email=destination).first()
    return ConsumerAccount.objects.filter(phone=destination).first()


def contact_verified(user, kind):
    """Whether the contact matching `kind` has been OTP-verified."""
    stamp = user.email_verified_at if kind == EMAIL else user.phone_verified_at
    return stamp is not None


def revoke_refresh_tokens(user):
    """Blacklist every outstanding refresh token for a user (kills all sessions)."""
    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)


@transaction.atomic
def reset_password(user, raw_password, kind):
    """Set a new password after a verified reset code.

    Passing the reset OTP also proves control of the contact, so mark it
    verified, (re)activate the account, and revoke existing sessions.
    """
    user.set_password(raw_password)
    field = "email_verified_at" if kind == EMAIL else "phone_verified_at"
    if getattr(user, field) is None:
        setattr(user, field, timezone.now())
    user.is_active = True
    user.save()
    revoke_refresh_tokens(user)


@transaction.atomic
def request_otp(destination, channel="sms", purpose=OtpCode.Purpose.VERIFY):
    """Issue a one-time code for (destination, purpose) and deliver it.

    Codes are scoped by purpose so an in-flight "verify" code and a
    "reset" code for the same address never invalidate each other.
    """
    active = OtpCode.objects.filter(
        destination=destination, purpose=purpose, consumed_at__isnull=True
    )

    latest = active.order_by("-created_at").first()
    if latest:
        age = (timezone.now() - latest.created_at).total_seconds()
        if age < RESEND_COOLDOWN_SECONDS:
            wait = int(RESEND_COOLDOWN_SECONDS - age)
            raise ValidationError({"detail": f"Wait {wait}s before requesting again."})

    active.update(consumed_at=timezone.now())

    code = f"{secrets.randbelow(1_000_000):06d}"
    otp = OtpCode(destination=destination, channel=channel, purpose=purpose)
    otp.set_code(code, ttl_seconds=OTP_TTL_SECONDS)
    otp.save()

    get_sender(channel).send(destination, code)


def verify_otp(destination, code, purpose=OtpCode.Purpose.VERIFY):
    """Consume the latest usable code for (destination, purpose).

    Returns None on success. Raises ValidationError on any failure. Account
    creation / password changes are the caller's responsibility so this stays
    reusable for both contact verification and password reset.
    """
    with transaction.atomic():
        otp = (
            OtpCode.objects.select_for_update()
            .filter(destination=destination, purpose=purpose, consumed_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if otp is None:
            raise ValidationError({"detail": "No active code. Request a new one."})

        if not otp.is_usable:
            raise ValidationError({"detail": "Code expired or too many attempts."})

        if otp.check_code(code):
            otp.consumed_at = timezone.now()
            otp.save(update_fields=["consumed_at"])
            return

        # Wrong code: record the attempt so the lockout counter advances. We
        # must not raise inside the atomic block here — a ValidationError would
        # propagate out and roll the increment back, so `attempts` would never
        # grow and the account could never lock. Let the block commit the
        # increment, then reject below.
        otp.attempts += 1
        otp.save(update_fields=["attempts"])

    raise ValidationError({"detail": "Invalid code."})

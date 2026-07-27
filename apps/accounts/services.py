import secrets

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from rest_framework_simplejwt.tokens import RefreshToken

from . import ratelimit
from .identifiers import EMAIL
from .models import ConsumerAccount, OtpCode, Purpose, Verification
from .notifications import get_sender

OTP_TTL_SECONDS = 300
VERIFICATION_TTL_SECONDS = Verification.TTL_SECONDS


def _account_field(destination_type):
    return "email" if destination_type == EMAIL else "phone"


def find_account(destination, destination_type):
    """Look up an account by the destination used to reach it."""
    field = _account_field(destination_type)
    return ConsumerAccount.objects.filter(**{field: destination}).first()


def account_exists(destination, destination_type):
    field = _account_field(destination_type)
    return ConsumerAccount.objects.filter(**{field: destination}).exists()


def contact_verified(user, destination_type):
    """Whether the contact matching `destination_type` has been OTP-verified."""
    stamp = user.email_verified_at if destination_type == EMAIL else user.phone_verified_at
    return stamp is not None


def tokens_for(user):
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def request_otp(destination, destination_type, purpose, ip):
    """Issue and deliver a one-time code for (destination, purpose).

    Rate limits are enforced in Redis before any database work. The behaviour
    and return value never depend on whether an account exists for the
    destination, so this cannot be used to enumerate accounts.
    """
    ratelimit.enforce_request_otp(destination, purpose, ip)

    raw_code = f"{secrets.randbelow(1_000_000):06d}"

    with transaction.atomic():
        live = list(
            OtpCode.objects.select_for_update()
            .filter(destination=destination, purpose=purpose, consumed_at__isnull=True)
            .order_by("-created_at")
        )
        if live:
            OtpCode.objects.filter(pk__in=[c.pk for c in live]).update(
                consumed_at=timezone.now()
            )

        otp = OtpCode(
            destination=destination, destination_type=destination_type, purpose=purpose
        )
        otp.set_code(raw_code, ttl_seconds=OTP_TTL_SECONDS)
        otp.save()

        # Deliver only after the row is committed, and never while holding the
        # row lock: a provider call inside the transaction would keep the lock
        # for the length of a network round trip.
        sender = get_sender(destination_type)
        transaction.on_commit(
            lambda: sender.send(destination, raw_code, destination_type)
        )


def verify_otp(destination, destination_type, purpose, code, ip):
    """Check a submitted code and, on success, record a Verification.

    Returns account_exists. Raises ValidationError on any failure.

    A wrong code must advance the attempts counter durably. A write followed by
    a raise inside one atomic block is rolled back, so the counter would never
    move. We therefore compute `matched` and do the increment inside the block,
    let the block commit, then raise afterwards.
    """
    ratelimit.enforce_verify_otp(ip)

    matched = False
    with transaction.atomic():
        otp = (
            OtpCode.objects.select_for_update()
            .filter(destination=destination, purpose=purpose, consumed_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        # These guards write nothing, so raising inside the block is safe.
        if otp is None:
            raise ValidationError({"detail": "No active code. Request a new one."})
        if not otp.is_usable:
            raise ValidationError({"detail": "Code expired or too many attempts."})

        matched = otp.check_code(code)
        if matched:
            otp.consumed_at = timezone.now()
            otp.save(update_fields=["consumed_at"])
        else:
            OtpCode.objects.filter(pk=otp.pk).update(attempts=F("attempts") + 1)

    # Outside the atomic block: the increment above is now committed.
    if not matched:
        raise ValidationError({"detail": "Invalid code."})

    Verification.issue(destination, destination_type, purpose)

    return account_exists(destination, destination_type)


def register(destination, destination_type, purpose, full_name, password, accept_terms):
    """Create an account for a destination that was just OTP-verified.

    Instead of a client-held token, register looks up the newest usable
    Verification for (destination, destination_type, register), locks and
    consumes it in the same transaction that creates the account. So the
    account cannot be created without a prior verify, the verification backs at
    most one account, and a failed create never burns it.
    """
    if purpose != Purpose.REGISTER:
        raise ValidationError({"purpose": "Only register is valid at this endpoint."})

    with transaction.atomic():
        verification = (
            Verification.objects.select_for_update()
            .filter(
                destination=destination,
                destination_type=destination_type,
                purpose=Purpose.REGISTER,
                consumed_at__isnull=True,
            )
            .order_by("-created_at")
            .first()
        )
        if verification is None or not verification.is_usable:
            raise ValidationError(
                {"detail": "This contact has not been verified. Verify a code first."}
            )

        field = _account_field(destination_type)
        if ConsumerAccount.objects.filter(**{field: destination}).exists():
            raise ValidationError(
                {"detail": "An account already exists for this contact. Please log in."}
            )

        verification.consumed_at = timezone.now()
        verification.save(update_fields=["consumed_at"])

        now = timezone.now()
        account = ConsumerAccount(
            full_name=full_name,
            accepted_terms_at=now,
            is_active=True,
        )
        setattr(account, field, destination)
        if destination_type == EMAIL:
            account.email_verified_at = now
        else:
            account.phone_verified_at = now
        account.set_password(password)
        account.save()

    return tokens_for(account)

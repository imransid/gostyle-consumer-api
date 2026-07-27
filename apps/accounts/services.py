import secrets

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from rest_framework_simplejwt.tokens import RefreshToken

from . import ratelimit
from .identifiers import EMAIL
from .models import ConsumerAccount, OtpCode, Purpose, VerificationToken, _hash_token
from .notifications import get_sender

OTP_TTL_SECONDS = 300
VERIFICATION_TOKEN_TTL_SECONDS = VerificationToken.TTL_SECONDS


def find_account(identifier, channel):
    """Look up an account by the identifier used to reach it."""
    field = "email" if channel == EMAIL else "phone"
    return ConsumerAccount.objects.filter(**{field: identifier}).first()


def account_exists(identifier, channel):
    field = "email" if channel == EMAIL else "phone"
    return ConsumerAccount.objects.filter(**{field: identifier}).exists()


def contact_verified(user, channel):
    """Whether the contact matching `channel` has been OTP-verified."""
    stamp = user.email_verified_at if channel == EMAIL else user.phone_verified_at
    return stamp is not None


def tokens_for(user):
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def request_otp(identifier, channel, purpose, ip):
    """Issue and deliver a one-time code for (identifier, purpose).

    Rate limits are enforced in Redis before any database work. The behaviour
    and return value never depend on whether an account exists for the
    identifier, so this cannot be used to enumerate accounts.
    """
    ratelimit.enforce_request_otp(identifier, purpose, ip)

    raw_code = f"{secrets.randbelow(1_000_000):06d}"

    with transaction.atomic():
        live = list(
            OtpCode.objects.select_for_update()
            .filter(identifier=identifier, purpose=purpose, consumed_at__isnull=True)
            .order_by("-created_at")
        )
        if live:
            OtpCode.objects.filter(pk__in=[c.pk for c in live]).update(
                consumed_at=timezone.now()
            )

        otp = OtpCode(identifier=identifier, channel=channel, purpose=purpose)
        otp.set_code(raw_code, ttl_seconds=OTP_TTL_SECONDS)
        otp.save()

        # Deliver only after the row is committed, and never while holding the
        # row lock: a provider call inside the transaction would keep the lock
        # for the length of a network round trip.
        sender = get_sender(channel)
        transaction.on_commit(
            lambda: sender.send(identifier, raw_code, channel)
        )


def verify_otp(identifier, channel, purpose, code, ip):
    """Check a submitted code and, on success, issue a VerificationToken.

    Returns (raw_token, account_exists). Raises ValidationError on any failure.

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
            .filter(identifier=identifier, purpose=purpose, consumed_at__isnull=True)
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

    raw_token = secrets.token_urlsafe(32)
    token = VerificationToken(identifier=identifier, channel=channel, purpose=purpose)
    token.set_token(raw_token)
    token.save()

    return raw_token, account_exists(identifier, channel)


def register(verification_token, full_name, password, accept_terms):
    """Create an account from a verified-register token and return JWTs.

    The token is looked up under a row lock, checked, and consumed inside the
    same transaction that creates the account, so a token can back at most one
    account and a failed create never burns the token.
    """
    with transaction.atomic():
        token = (
            VerificationToken.objects.select_for_update()
            .filter(token_hash=_hash_token(verification_token))
            .first()
        )
        if token is None or not token.is_usable:
            raise ValidationError(
                {"verification_token": "Invalid or expired verification token."}
            )
        if token.purpose != Purpose.REGISTER:
            raise ValidationError(
                {"verification_token": "This token cannot be used to register."}
            )

        token.consumed_at = timezone.now()
        token.save(update_fields=["consumed_at"])

        field = "email" if token.channel == EMAIL else "phone"
        if ConsumerAccount.objects.filter(**{field: token.identifier}).exists():
            raise ValidationError(
                {"detail": "An account already exists for this contact. Please log in."}
            )

        now = timezone.now()
        account = ConsumerAccount(
            full_name=full_name,
            accepted_terms_at=now,
            is_active=True,
        )
        setattr(account, field, token.identifier)
        if token.channel == EMAIL:
            account.email_verified_at = now
        else:
            account.phone_verified_at = now
        account.set_password(password)
        account.save()

    return tokens_for(account)

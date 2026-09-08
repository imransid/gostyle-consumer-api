import secrets

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from datetime import timedelta
from rest_framework_simplejwt.tokens import RefreshToken

from . import ratelimit
from .identifiers import EMAIL
from .models import ConsumerAccount, OtpCode, Purpose, Verification
from .notifications import get_sender

OTP_TTL_SECONDS = 300
VERIFICATION_TTL_SECONDS = Verification.TTL_SECONDS


def _account_field(destination_type):
    return "email" if destination_type == EMAIL else "phone"


def _verified_field(destination_type):
    return "email_verified_at" if destination_type == EMAIL else "phone_verified_at"


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


def request_otp(destination, destination_type, purpose, ip, enforce_limits=True):
    """Issue and deliver a one-time code for (destination, purpose).

    Rate limits are enforced in Redis before any database work. The behaviour
    and return value never depend on whether an account exists for the
    destination, so this cannot be used to enumerate accounts.

    `enforce_limits=False` is for callers that have ALREADY called
    enforce_request_otp for this same (destination, purpose). Without it the
    limiter runs twice in one request: the first call writes the cooldown key
    and the second trips over it, so the request 429s itself every time.
    """
    if enforce_limits:
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


def request_otp_for_user(user, destination_type, purpose, ip):
    """Like request_otp, but the destination is always the caller's own
    contact on file — never a client-supplied value. Prevents an
    authenticated user from spamming OTPs to someone else's email/phone.
    """
    field = _account_field(destination_type)
    destination = getattr(user, field)
    if not destination:
        raise ValidationError(
            {"detail": f"No {destination_type} on file for this account."}
        )
    request_otp(
        destination=destination,
        destination_type=destination_type,
        purpose=purpose,
        ip=ip,
    )


def resend_otp_for_user(destination, destination_type, purpose, ip):
    """Like request_otp, but the destination is always the caller's own
    contact on file — never a client-supplied value. Prevents an
    authenticated user from spamming OTPs to someone else's email/phone.
    """
    request_otp(
        destination=destination,
        destination_type=destination_type,
        purpose=purpose,
        ip=ip,
    )


def verify_otp_for_user(user, destination_type, purpose, code, ip):
    """Check a submitted code against the caller's own contact and, on
    success, mark that user's row verified (and active).

    Raises ValidationError on any failure.
    """
    field = _account_field(destination_type)
    destination = getattr(user, field)
    if not destination:
        raise ValidationError(
            {"detail": f"No {destination_type} on file for this account."}
        )

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

    ConsumerAccount.objects.filter(pk=user.pk).update(
        **{_verified_field(destination_type): timezone.now()},
        account_verified=True,
        is_active=True,
    )


def register(destination, destination_type, full_name, password):
    """Create the account directly. No OTP/Verification required here —
    verification happens later via request_otp_for_user/verify_otp_for_user
    against this account.
    """
    field = _account_field(destination_type)

    with transaction.atomic():
        if ConsumerAccount.objects.filter(**{field: destination}).exists():
            raise ValidationError(
                {"detail": "An account already exists for this contact. Please log in."}
            )

        now = timezone.now()
        account = ConsumerAccount(
            full_name=full_name,
            accepted_terms_at=now,
            is_active=True,
        )
        setattr(account, field, destination)
        account.set_password(password)
        account.save()

    tokens = tokens_for(account)
    return {
        "detail": "Registration successful. Please verify your account to continue.",
        **tokens,
    }


def change_password(user, old_password, new_password):
    """Change the password of an authenticated caller.

    Deliberately does NOT blacklist other sessions. The user knows their old
    password, so there is no reason to believe another session is hostile, and
    logging someone out of their other devices for a routine change is
    surprising. reset_password below takes the opposite view, for the opposite
    reason.
    """
    if not user.check_password(old_password):
        # Same generic message as login. A distinct "wrong old password" is
        # harmless here (the caller is already authenticated as this user) but
        # keeping one phrasing means one string to get right.
        raise ValidationError({"detail": "Invalid credentials."})

    user.set_password(new_password)
    user.save(update_fields=["password"])


def request_password_reset(destination, destination_type, ip):
    """Step 1: send a reset code.

    ALWAYS the same outcome whether the account exists or not. An attacker
    who can tell registered numbers from unregistered ones has a customer list,
    so the code is issued and the response returned identically either way;
    only delivery is skipped when there is nobody to deliver to.

    Rate limiting runs BEFORE the account lookup, so timing does not leak the
    answer either. request_otp is then called with enforce_limits=False: the
    limit for this request has already been spent above, and letting it run a
    second time would 429 every registered address.
    """
    ratelimit.enforce_request_otp(destination, Purpose.PASSWORD_RESET, ip)

    if not account_exists(destination, destination_type):
        # Silent no-op. Not an error, not a different response.
        return

    request_otp(
        destination=destination,
        destination_type=destination_type,
        purpose=Purpose.PASSWORD_RESET,
        ip=ip,
        enforce_limits=False,
    )


def verify_password_reset(destination, destination_type, code, ip):
    """Step 2: exchange a correct code for a single-use reset token.

    The token is a Verification row. Its id is a UUID4, so it cannot be
    guessed; it expires in ten minutes; and consumed_at makes it single-use.
    That is why this returns a token rather than just "ok": without one, step
    three would have to trust a destination supplied by the client.
    """
    ratelimit.enforce_verify_otp(ip)

    now = timezone.now()
    matched = False

    with transaction.atomic():
        codes = (
            OtpCode.objects.select_for_update()
            .filter(
                destination=destination,
                destination_type=destination_type,
                purpose=Purpose.PASSWORD_RESET,
                consumed_at__isnull=True,
                expires_at__gt=now,
            )
            .order_by("-created_at")
        )

        for otp in codes:
            if otp.check_code(code):
                otp.consumed_at = now
                otp.save(update_fields=["consumed_at"])
                matched = True
                break

    if not matched:
        raise ValidationError({"detail": "Invalid code."})

    verification = Verification.objects.create(
        destination=destination,
        destination_type=destination_type,
        purpose=Purpose.PASSWORD_RESET,
        expires_at=now + timedelta(seconds=VERIFICATION_TTL_SECONDS),
    )

    return {
        "reset_token": str(verification.id),
        "expires_in": VERIFICATION_TTL_SECONDS,
    }


def reset_password(reset_token, new_password):
    """Step 3: spend the token and set the password.

    The whole thing is one transaction with select_for_update on the
    Verification row. Two requests arriving with the same token cannot both
    succeed: the second blocks, then finds consumed_at already set.

    Every refresh token is blacklisted afterwards. Somebody resetting a
    password may be doing it BECAUSE their account was stolen, so any session
    the attacker holds has to die with the old password. change_password does
    not do this, and the difference is deliberate.
    """
    now = timezone.now()

    with transaction.atomic():
        verification = (
            Verification.objects.select_for_update()
            .filter(
                id=reset_token,
                purpose=Purpose.PASSWORD_RESET,
                consumed_at__isnull=True,
                expires_at__gt=now,
            )
            .first()
        )

        if verification is None:
            raise ValidationError({"detail": "Invalid or expired reset token."})

        account = find_account(
            verification.destination, verification.destination_type
        )
        if account is None:
            # The account was deleted between step two and step three.
            raise ValidationError({"detail": "Invalid or expired reset token."})

        verification.consumed_at = now
        verification.save(update_fields=["consumed_at"])

        account.set_password(new_password)
        account.save(update_fields=["password"])

        _blacklist_all_refresh_tokens(account)


def _blacklist_all_refresh_tokens(account):
    """Kill every outstanding session for this account.

    Imported here rather than at module scope: token_blacklist models are only
    loadable once apps are ready, and services.py is imported early.
    """
    from rest_framework_simplejwt.token_blacklist.models import (
        BlacklistedToken,
        OutstandingToken,
    )

    tokens = OutstandingToken.objects.filter(user=account)
    for token in tokens:
        BlacklistedToken.objects.get_or_create(token=token)
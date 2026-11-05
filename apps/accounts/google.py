"""
Sign in with Google, and one account for both ways in (GOOGLE_SIGN_IN_V1,
docs/GOOGLE_SIGN_IN_API.md).

The app's Google SDK gives it an ID token: a JWT that Google signed. It is
checked here against Google's public keys with PyJWT (already a dependency):
no call to Google is needed to sign in, and no client secret.

Then the account (the brief's section 2.1), in this order:
  1. the Google account is linked to one of ours (its `sub`): that one;
  2. an account has the same email and Google says the email is verified:
     link it. If its email was never verified on our side, it is taken over
     first (take_over): the email verified, the password removed, every
     session ended;
  3. else a new account, with no password.

What follows is what /auth/login does: 2-step on means the challenge, else
the token pair with a session. The ID token is never stored or logged.
"""

import logging
import time
from dataclasses import dataclass

import jwt
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, ErrorDetail, PermissionDenied, ValidationError

from . import geoip, services, sessions
from .models import ConsumerAccount, SocialIdentity, UserSession
from .notifications import send_security_notice, verified_email

logger = logging.getLogger(__name__)

GOOGLE = SocialIdentity.Provider.GOOGLE

ISSUERS = ("accounts.google.com", "https://accounts.google.com")
KEYS_URL = "https://www.googleapis.com/oauth2/v3/certs"
# How long Google's public keys are kept before they are read again. A token
# signed with a key not in the cache makes PyJWKClient read them at once.
KEYS_SECONDS = 3600
# Delete Account's "Confirm with Google": the token must be this fresh.
FRESH_SECONDS = 600

NAME_MAX = ConsumerAccount._meta.get_field("full_name").max_length


def enabled():
    return settings.GOOGLE_SIGN_IN_V1


def client_ids():
    """The only audiences a token may carry: the web and iOS client ids."""
    return [part.strip() for part in settings.GOOGLE_CLIENT_IDS.split(",") if part.strip()]


# --- errors -----------------------------------------------------------------------


class _Refusal(APIException):
    """An error of the whole request: its code at the top and an empty
    errors list (exceptions.py, envelope_code), as the brief's section 4
    shows it."""

    def __init__(self):
        super().__init__(self.default_detail, self.default_code)


class InvalidToken(_Refusal):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "We couldn’t sign you in with Google. Please try again."
    default_code = envelope_code = "invalid_token"


class EmailInUse(_Refusal):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "An account already uses this email. Sign in with your password instead."
    default_code = envelope_code = "email_in_use"


class GoogleAlreadyLinked(_Refusal):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This account is already linked to a different Google account."
    default_code = envelope_code = "google_already_linked"


class GoogleUnavailable(_Refusal):
    """503: Google's public keys could not be read. Not the customer's
    fault, and not in the brief's table: the generic 503 code."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Google sign-in isn’t available right now. Please try again."
    default_code = envelope_code = "service_unavailable"


class PasswordNotSet(_Refusal):
    """401 on POST /auth/login for an account made with Google that has no
    password yet (section 4)."""

    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = (
        "This account signs in with Google. Continue with Google, or set a password in Settings."
    )
    default_code = envelope_code = "password_not_set"


class PasswordAlreadySet(_Refusal):
    """409 on POST /auth/password/set: Change password is the way (section 5)."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "This account already has a password. Use Change password instead."
    default_code = envelope_code = "password_already_set"


class StaleIdToken(APIException):
    """401 on POST /auth/me/delete: the id_token is bad, expired, or older
    than 10 minutes (section 6). On its field, and `invalid_token` at the top
    too, so the app never reads it as its session ending."""

    status_code = status.HTTP_401_UNAUTHORIZED
    top_level_code = "invalid_token"

    def __init__(self):
        super().__init__(
            {
                "id_token": [
                    ErrorDetail(
                        "Google couldn’t confirm it’s you. Please try again.", "invalid_token"
                    )
                ]
            }
        )


def wrong_google_account():
    """403 on POST /auth/me/delete: a Google account that is not the linked one."""
    raise PermissionDenied(
        {
            "id_token": [
                ErrorDetail(
                    "That isn’t the Google account linked to your Go Style account.",
                    code="wrong_google_account",
                )
            ]
        }
    )


def _disabled():
    """What /auth/login answers a disabled account, word for word."""
    raise ValidationError({"detail": "Account disabled."})


# --- the ID token -------------------------------------------------------------------


@dataclass(frozen=True)
class GoogleUser:
    """What is kept from a checked ID token."""

    subject: str
    email: str
    email_verified: bool
    name: str
    issued_at: int | None


_key_client = None


def _keys():
    """Google's public keys, read over HTTPS and cached in this process."""
    global _key_client
    if _key_client is None:
        _key_client = jwt.PyJWKClient(
            KEYS_URL, cache_jwk_set=True, lifespan=KEYS_SECONDS, timeout=10
        )
    return _key_client


def _true(value):
    """Google sends email_verified as a JSON boolean; a string is taken too."""
    return value is True or str(value).lower() == "true"


def verify(id_token):
    """
    Check a Google ID token and return its GoogleUser, the brief's checks
    and only those: Google's signature, `iss` Google, `exp` not passed, `aud`
    one of our two client ids. Anything off is InvalidToken; Google's keys
    unreadable is GoogleUnavailable.
    """
    try:
        signing_key = _keys().get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_ids(),
            options={"require": ["iss", "sub", "aud", "exp"], "verify_iat": False},
        )
    except jwt.PyJWKClientConnectionError as exc:
        logger.error("google sign-in: Google's keys could not be read: %s", exc)
        raise GoogleUnavailable() from exc
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        logger.info("google sign-in: token refused: %s", exc.__class__.__name__)
        raise InvalidToken() from exc

    if claims["iss"] not in ISSUERS or not str(claims["sub"]).strip():
        raise InvalidToken()
    email = claims.get("email")
    iat = claims.get("iat")
    return GoogleUser(
        subject=str(claims["sub"]),
        email=email.strip().lower() if isinstance(email, str) else "",
        email_verified=_true(claims.get("email_verified")),
        name=(claims.get("name") or "").strip() if isinstance(claims.get("name"), str) else "",
        issued_at=iat if isinstance(iat, int) and not isinstance(iat, bool) else None,
    )


def verify_fresh(id_token):
    """Section 6: a token checked as `verify` does, and issued in the last
    10 minutes. Else StaleIdToken, on the id_token field."""
    try:
        google_user = verify(id_token)
    except InvalidToken as exc:
        raise StaleIdToken() from exc
    if google_user.issued_at is None or time.time() - google_user.issued_at > FRESH_SECONDS:
        raise StaleIdToken()
    return google_user


# --- the account --------------------------------------------------------------------


def linked(account):
    """The account's Google link, or None."""
    return SocialIdentity.objects.filter(account=account, provider=GOOGLE).first()


@dataclass
class Outcome:
    account: ConsumerAccount
    is_new_user: bool


def sign_in(google_user, request):
    """
    The account this Google user signs in to (section 2.1), linking or
    making it as needed. Raises EmailInUse, GoogleAlreadyLinked, or what
    /auth/login answers a disabled account. Two sign-ins of one person at
    the same moment: the second finds what the first made.
    """
    for attempt in range(2):
        try:
            with transaction.atomic():
                return _sign_in(google_user, request)
        except IntegrityError:
            # A parallel sign-in linked this Google account, or made its
            # account, first. Once more finds it by its sub.
            if attempt:
                raise
    raise AssertionError("unreachable")


def _sign_in(google_user, request):
    now = timezone.now()

    identity = (
        SocialIdentity.objects.select_for_update()
        .filter(provider=GOOGLE, subject=google_user.subject)
        .first()
    )
    if identity is not None:
        account = ConsumerAccount.objects.filter(pk=identity.account_id).first()
        if account is not None:
            # 1. Linked: that account. Its Google address is refreshed.
            if not account.is_active:
                _disabled()
            SocialIdentity.objects.filter(pk=identity.pk).update(
                email=google_user.email, last_used_at=now
            )
            return Outcome(account, is_new_user=False)
        # A link left on a purged account (the purge deletes them): stale.
        identity.delete()

    match = _account_with_email(google_user.email)
    if match is not None:
        # 2. The same email.
        if not google_user.email_verified:
            raise EmailInUse()
        if not match.is_active:
            _disabled()
        if SocialIdentity.objects.filter(account=match, provider=GOOGLE).exists():
            raise GoogleAlreadyLinked()
        if match.email_verified_at is None:
            take_over(match)
        _link(match, google_user, now)
        _tell_linked(match, request)
        return Outcome(match, is_new_user=False)

    # 3. Neither: a new account.
    account = _create(google_user, now)
    _link(account, google_user, now)
    return Outcome(account, is_new_user=True)


def _account_with_email(email):
    """The living account with this email, case ignored, or None. Should two
    differ only by case, the verified one, then the oldest."""
    if not email:
        return None
    return (
        ConsumerAccount.objects.select_for_update(of=("self",))
        .filter(email__iexact=email)
        .order_by(F("email_verified_at").asc(nulls_last=True), "created_at")
        .first()
    )


def take_over(account):
    """
    Section 2.1, an email never verified. Someone may have signed up with
    this address before its owner did; Google has just proved who owns it.
    The email is verified now, its password is removed, and its other
    sessions end (their refresh tokens revoked). The new sign-in's session
    starts after this.
    """
    now = timezone.now()
    account.email_verified_at = now
    account.account_verified = True
    account.set_unusable_password()
    account.password_changed_at = None  # no password any more
    account.save(
        update_fields=[
            "email_verified_at",
            "account_verified",
            "password",
            "password_changed_at",
            "updated_at",
        ]
    )
    services._blacklist_all_refresh_tokens(account)
    ended = list(
        UserSession.objects.filter(user=account, ended_at__isnull=True).values_list("pk", flat=True)
    )
    if ended:
        # After the commit: ending one also calls the push service, never
        # inside a transaction. The refresh tokens are revoked already.
        transaction.on_commit(
            lambda: sessions.end(UserSession.objects.filter(pk__in=ended), remove_push=True)
        )
    logger.warning(
        "google sign-in: unverified account %s taken over by its email's owner", account.pk
    )


def _link(account, google_user, now):
    SocialIdentity.objects.create(
        account=account,
        provider=GOOGLE,
        subject=google_user.subject,
        email=google_user.email,
        last_used_at=now,
    )


def _create(google_user, now):
    """Section 2.1, step 3: the name from Google (else the email before the
    @), the email verified now when Google verified it, no password, no
    gender. Terms accepted as at register: by signing up."""
    email = google_user.email or None
    if email and not google_user.email_verified:
        # Never linked by an unverified email, and never made a second
        # account with it either (the email is unique).
        if ConsumerAccount.all_objects.filter(email__iexact=email).exists():
            raise EmailInUse()
    name = google_user.name or (email or "").partition("@")[0]
    account = ConsumerAccount(
        full_name=name[:NAME_MAX],
        email=email,
        accepted_terms_at=now,
        is_active=True,
    )
    if email and google_user.email_verified:
        account.email_verified_at = now
        # Verified as a code would verify it (services.verify_otp_for_user).
        account.account_verified = True
    account.set_unusable_password()
    account.save()
    return account


# --- emails -------------------------------------------------------------------------


def _where(request):
    """The words " on iPhone 15 in Dubai, UAE": the device the app names and
    the place of the request's address, each only when known."""
    device = sessions.read_device(request).get("device_name")
    place = geoip.location(sessions.client_ip(request))
    words = f" on {device}" if device else ""
    if isinstance(place, str) and place:
        words += f" in {place}"
    return words


def _tell_linked(account, request):
    """Section 7: Google was linked to an existing account. To its verified
    email, once the link is committed."""
    destination = verified_email(account)
    if destination is None:
        return
    message = (
        f"Google sign-in was added to your Go Style account{_where(request)}. "
        "From now on, Continue with Google signs you in to it.\n\n"
        "If this was not you, contact support straight away."
    )
    transaction.on_commit(
        lambda: send_security_notice(
            destination,
            subject="Go Style: Google sign-in was added to your account",
            message=message,
            headline="Google sign-in was added to your account",
        )
    )

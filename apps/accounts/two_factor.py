"""
2-step verification and the authenticator app (docs/TWO_FACTOR_API.md),
behind TWO_FACTOR_V1, and only with DEVICE_SESSIONS_V1 on too: a
verification token belongs to a session.

Everything that makes, sends, checks or spends a 2-step code is here. The
views only read the request and answer.

  * hints: the account's email and phone, masked here so the full ones never
    travel just to be hidden again (section 2).
  * codes: six digits to the account's OWN verified email or phone, never to
    an address from the request (3.1). Phone codes go by WhatsApp, the way
    the OTP codes do; the brief says SMS, and this API has no SMS sender.
  * verification tokens: what a passed Verify it's you answers, and what the
    change it was asked for spends (3.2).
  * the authenticator: its secret, AES-GCM at rest, and its TOTP codes (5).
  * notices: an email when 2-step goes on or off, or an authenticator is
    added or removed, naming the device and place it was done from (4, 5.3;
    the one on adding is ours), and when sign-in alerts are turned off.
  * limits: one send per 30 seconds per purpose, and 5 wrong codes per 15
    minutes per account across every call (5.4).
  * the sign-in challenge: what login answers instead of tokens, and the
    code that finishes it (6).
  * the sign-in alerts switch, PATCH /me/security (docs/SIGN_IN_ALERTS_API.md).
    The alert it turns on and off is sessions.start's.

An error that belongs to no field answers in the shape of
auth-error-response.md section 4: its own code at the top, and the same
error as the one entry in `errors` (exceptions.py, top_level_code).
"""

import base64
import binascii
import hashlib
import hmac
import logging
import os
import secrets
from datetime import timedelta

import phonenumbers
import pyotp
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    AuthenticationFailed,
    ErrorDetail,
    ValidationError,
)

from . import services, sessions
from .identifiers import EMAIL, PHONE
from .models import (
    ConsumerAccount,
    LoginChallenge,
    TwoFactorMethod,
    TwoFactorPurpose,
    UserAuthenticator,
    UserSession,
    VerificationCode,
    VerificationToken,
)
from .notifications import get_sender, send_security_notice

logger = logging.getLogger(__name__)

HOUR = 3600
DAY = 86400

CODE_TTL_SECONDS = 600
SETUP_TTL_SECONDS = 600  # an authenticator's pending setup
TOKEN_TTL_SECONDS = 300
RESEND_SECONDS = 30

# 5.4: wrong codes per account, across every call that checks one.
WRONG_CODES = 5
WRONG_CODES_WINDOW = 900

# Sends. The brief gives only the 30 seconds; these keep a stolen session
# (or a sign-in with a stolen password) from running up email and WhatsApp.
SENDS_PER_ACCOUNT_HOURLY = 10
SENDS_PER_ACCOUNT_DAILY = 20
SENDS_PER_IP_HOURLY = 20

ISSUER = "Go Style"

# An emailed code's subject. Every other word of the email is the OTP
# email's ("Confirm it's you").
SIGN_IN_SUBJECT = "Your Go Style sign-in code"
SECURITY_SUBJECT = "Your Go Style security code"

# Not a 2-step purpose: a notice's name for PATCH /me/security turning
# sign-in alerts off.
SIGN_IN_ALERTS_OFF = "sign_in_alerts_off"

# Each change's notice: its subject, and what happened.
_NOTICES = {
    TwoFactorPurpose.TWO_STEP_ON: (
        "Go Style: 2-step verification turned on",
        "2-step verification was turned on for your Go Style account",
    ),
    TwoFactorPurpose.TWO_STEP_OFF: (
        "Go Style: 2-step verification turned off",
        "2-step verification was turned off for your Go Style account",
    ),
    # Not in the brief (Rafa, 2026-10-27): adding an authenticator is the one
    # change an intruder would make to keep access, so the owner hears of it.
    TwoFactorPurpose.AUTHENTICATOR_ADD: (
        "Go Style: authenticator app added",
        "An authenticator app was added to your Go Style account",
    ),
    TwoFactorPurpose.AUTHENTICATOR_REMOVE: (
        "Go Style: authenticator app removed",
        "The authenticator app was removed from your Go Style account",
    ),
    # Turning them back on sends nothing: an intruder hides by turning
    # them off.
    SIGN_IN_ALERTS_OFF: (
        "Go Style: sign-in alerts turned off",
        "Sign-in alerts were turned off for your Go Style account",
    ),
}
TOKEN_PREFIX = "vt_"
_NONCE_BYTES = 12

CHALLENGE_PREFIX = "ch_"
CHALLENGE_TTL_SECONDS = 600
CHALLENGE_WRONG_CODES = 5

_TRIES_KEY = "2fa:tries:{}"


def enabled():
    return settings.TWO_FACTOR_V1 and sessions.enabled()


# --- errors -------------------------------------------------------------------


class _RequestError(APIException):
    """An error of the whole request (field null). Each subclass names its
    code twice, as default_code and as top_level_code, the way the sessions
    errors name envelope_code."""

    def __init__(self):
        super().__init__({"detail": ErrorDetail(self.default_detail, code=self.default_code)})


class VerificationRequired(_RequestError):
    """403: no verification token, or a used, expired or wrong-purpose one."""

    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "Please verify it’s you first."
    default_code = top_level_code = "verification_required"


class AlreadyAdded(_RequestError):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Remove your current authenticator app first."
    default_code = top_level_code = "already_added"


class NoSetup(_RequestError):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Start setting up your authenticator app again."
    default_code = top_level_code = "no_setup"


class TooManyAttempts(_RequestError):
    """429: too many wrong codes, or a send too soon. `wait`, when known,
    goes out as Retry-After."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = "Too many tries. Wait a few minutes and try again."
    default_code = top_level_code = "too_many_attempts"

    def __init__(self, wait=None):
        super().__init__()
        self.wait = wait


class ChallengeExpired(_RequestError):
    """401: a sign-in challenge expired or used up."""

    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Your sign-in timed out. Please sign in again."
    default_code = top_level_code = "challenge_expired"


class NotSignedIn(AuthenticationFailed):
    """401 on GET and PATCH /me/security for no token or an invalid one, in
    the words of docs/SIGN_IN_ALERTS_API.md. The other 2-step routes keep
    the 401s they shipped with."""

    default_detail = "Please sign in again."
    default_code = "not_authenticated"


class AuthenticatorUnavailable(_RequestError):
    """503: TWO_FACTOR_ENCRYPTION_KEY is missing, or does not open a secret.
    Logged where it is raised. Not in the brief."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "The authenticator app can’t be used right now. Please try again later."
    default_code = top_level_code = "authenticator_unavailable"


# The brief's 422s, each on its field.
WRONG_CODE = ("code", "invalid", "That code isn’t right. Check it and try again.")
EXPIRED_CODE = ("code", "expired", "This code has expired. Send a new one.")
METHOD_UNAVAILABLE = ("method", "unavailable", "That way to verify isn’t set up on your account.")


def _field_error(field, code, message):
    raise ValidationError({field: [ErrorDetail(message, code=code)]})


# --- hints --------------------------------------------------------------------


def email_hint(email):
    """k•••@gmail.com: the first letter, •••, then the domain."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}•••@{domain}"


def phone_hint(phone):
    """+971 •• ••• 4567: the country code and the last 4 digits."""
    try:
        parsed = phonenumbers.parse(phone)
    except phonenumbers.NumberParseException:
        return f"•• ••• {phone[-4:]}"
    return f"+{parsed.country_code} •• ••• {str(parsed.national_number)[-4:]}"


def _contacts(user):
    """{method: address} for the account's verified email and phone: the only
    places a code is ever sent."""
    found = {}
    if user.email and user.email_verified_at:
        found[EMAIL] = user.email
    if user.phone and user.phone_verified_at:
        found[PHONE] = user.phone
    return found


def contact_hints(user):
    """{method: hint} for the account's verified email and phone."""
    return {
        method: email_hint(address) if method == EMAIL else phone_hint(address)
        for method, address in _contacts(user).items()
    }


def active_authenticator(user):
    """The account's confirmed authenticator, or None. A pending setup does
    not count."""
    return UserAuthenticator.objects.filter(
        user=user, status=UserAuthenticator.Status.ACTIVE
    ).first()


# --- codes by email and phone ---------------------------------------------------


def send_code(user, method, purpose, ip):
    """Send a new code for `purpose` to the account's own email or phone,
    voiding the one before. Answers {sent_to, expires_in, resend_in}."""
    destination = _contacts(user).get(method)
    if destination is None:
        _field_error(*METHOD_UNAVAILABLE)
    _limit_sends(user, purpose, ip)

    raw = f"{secrets.randbelow(1_000_000):06d}"
    now = timezone.now()
    with transaction.atomic():
        VerificationCode.objects.filter(user=user, purpose=purpose, used_at__isnull=True).update(
            used_at=now
        )
        VerificationCode.objects.create(
            user=user,
            method=method,
            purpose=purpose,
            code_hash=_code_hash(user, purpose, raw),
            expires_at=now + timedelta(seconds=CODE_TTL_SECONDS),
        )
        # After the commit, as request_otp does: a provider call inside the
        # transaction would hold it open for a network round trip.
        sender = get_sender(method)
        subject = SIGN_IN_SUBJECT if purpose == TwoFactorPurpose.SIGN_IN else SECURITY_SUBJECT
        transaction.on_commit(
            lambda: sender.send(
                destination, raw, method, subject=subject, minutes=CODE_TTL_SECONDS // 60
            )
        )

    return {
        "sent_to": contact_hints(user)[method],
        "expires_in": CODE_TTL_SECONDS,
        "resend_in": RESEND_SECONDS,
    }


def check_code(user, method, purpose, code):
    """Spend the account's live `purpose` code sent by `method`, if `code`
    is it. 422 expired when there is none (never sent, voided by a newer one,
    past its 10 minutes, or out of tries), 422 invalid when it is not this."""
    _take_try(user)
    now = timezone.now()
    with transaction.atomic():
        row = (
            VerificationCode.objects.select_for_update()
            .filter(user=user, purpose=purpose, method=method, used_at__isnull=True)
            .first()
        )
        if row is None or row.expires_at <= now or row.attempts >= WRONG_CODES:
            _give_back_try(user)
            _field_error(*EXPIRED_CODE)
        right = constant_time_compare(row.code_hash, _code_hash(user, purpose, code))
        if right:
            row.used_at = now
            row.save(update_fields=["used_at"])
        else:
            VerificationCode.objects.filter(pk=row.pk).update(attempts=F("attempts") + 1)

    # Outside the block, so the attempt above is committed.
    if not right:
        _field_error(*WRONG_CODE)
    _give_back_try(user)


def _code_hash(user, purpose, raw):
    """A keyed hash, not a password hash: the code lives 10 minutes and 5
    tries, and its key (SECRET_KEY) is not in the database."""
    return salted_hmac(
        "apps.accounts.two_factor.code", f"{user.pk}:{purpose}:{raw}", algorithm="sha256"
    ).hexdigest()


# --- verification tokens ----------------------------------------------------------


def verify(user, sid, method, purpose, code):
    """POST /me/security/verify: check a code from any method the account
    has, and answer a verification token for `purpose`."""
    if method == TwoFactorMethod.AUTHENTICATOR:
        check_authenticator_code(user, code)
    else:
        check_code(user, method, purpose, code)
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    VerificationToken.objects.create(
        token_hash=_hash(raw),
        user=user,
        session_id=sid,
        purpose=purpose,
        expires_at=timezone.now() + timedelta(seconds=TOKEN_TTL_SECONDS),
    )
    return {"verification_token": raw, "expires_in": TOKEN_TTL_SECONDS}


def spend_token(user, sid, purpose, raw):
    """Use up a verification token for this user, this session and this
    purpose, or answer 403 verification_required. Call it inside the
    change's own transaction, so a change that fails gives the token back."""
    now = timezone.now()
    spent = bool(raw) and VerificationToken.objects.filter(
        token_hash=_hash(raw),
        user=user,
        session_id=sid,
        purpose=purpose,
        used_at__isnull=True,
        expires_at__gt=now,
    ).update(used_at=now)
    if not spent:
        raise VerificationRequired()


def _hash(raw_token):
    return hashlib.sha256(raw_token.encode()).hexdigest()


# --- the switch -------------------------------------------------------------------


def set_enabled(user, sid, enabled):
    """PATCH /me/security/two-factor, once its token is spent: turn 2-step on
    or off. Returns whether it changed.

    Turning it on also marks this session as having passed 2-step: it has
    just passed Verify it's you, so this phone is not asked at its next
    sign-in. Turning it off leaves the authenticator linked, for Verify it's
    you and for when 2-step goes on again.
    """
    now = timezone.now()
    changed = (
        ConsumerAccount.all_objects.filter(pk=user.pk)
        .exclude(two_factor_enabled=enabled)
        .update(two_factor_enabled=enabled, two_factor_changed_at=now)
    )
    if enabled:
        UserSession.objects.filter(pk=sid, user=user).update(two_factor_passed_at=now)
    user.two_factor_enabled = enabled
    if changed:
        user.two_factor_changed_at = now
    return bool(changed)


def set_sign_in_alerts(user, on):
    """PATCH /me/security: turn the new sign-in alert on or off. No Verify
    it's you: it is a notice, like the notification preferences. Returns
    whether it changed."""
    changed = (
        ConsumerAccount.all_objects.filter(pk=user.pk)
        .exclude(sign_in_alerts=on)
        .update(sign_in_alerts=on)
    )
    user.sign_in_alerts = on
    return bool(changed)


# --- notices --------------------------------------------------------------------


def notify(user, sid, change):
    """Email the account that `change` (two_step_on, two_step_off,
    authenticator_add, authenticator_remove or sign_in_alerts_off) was just
    made, from the device and place of the
    session `sid`: "2-step verification was turned off for your Go Style
    account on iPhone 15 in Dubai." Sent once the change is committed.

    Only to a verified email. A phone-only account gets no notice: WhatsApp
    needs an approved template for one, as for the contact change notice.
    """
    if not (user.email and user.email_verified_at):
        return
    session = UserSession.objects.filter(pk=sid).first() if sid else None
    device = (session and session.device_name) or "an unknown device"
    place = f" in {session.location}" if session and session.location else ""
    subject, what = _NOTICES[change]
    message = (
        f"{what} on {device}{place}.\n\n"
        "If this was not you, reset your password and contact support straight away."
    )
    destination = user.email
    # "Go Style: authenticator app removed" heads the email as "Authenticator app removed".
    headline = subject.removeprefix("Go Style: ")
    headline = headline[:1].upper() + headline[1:]
    transaction.on_commit(
        lambda: send_security_notice(destination, subject, message, headline=headline)
    )


# --- the authenticator app ------------------------------------------------------


def start_setup(user, sid):
    """POST /me/security/authenticator: a new secret, pending for this
    session until its first code confirms it. Replaces a pending one (its QR
    code stops working). 409 already_added while one is confirmed."""
    secret = pyotp.random_base32(32)  # 160 bits
    encrypted = _encrypt(user.pk, secret)  # 503 before anything changes
    with transaction.atomic():
        _lock(user)
        if active_authenticator(user) is not None:
            raise AlreadyAdded()
        UserAuthenticator.objects.update_or_create(
            user=user,
            defaults={
                "secret_encrypted": encrypted,
                "status": UserAuthenticator.Status.PENDING,
                "session_id": sid,
                "last_used_step": None,
                "created_at": timezone.now(),
                "confirmed_at": None,
            },
        )
    return {
        "secret": secret,
        "otpauth_url": otpauth_url(user, secret),
        "expires_in": SETUP_TTL_SECONDS,
    }


def otpauth_url(user, secret):
    """The QR code's link. Labelled with the account's email, or its phone
    when it has none. pyotp leaves out the three settings that are its
    defaults; the brief's link has them, and some apps read them."""
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email or user.phone, issuer_name=ISSUER)
    return f"{uri}&algorithm=SHA1&digits=6&period=30"


def confirm_setup(user, sid, code):
    """POST /me/security/authenticator/confirm: the app's first code turns
    this session's pending setup on. Returns when it was added. 409 no_setup
    without one (none, past its 10 minutes, or another session's); a wrong
    code leaves it pending."""
    now = timezone.now()
    with transaction.atomic():
        _lock(user)
        row = UserAuthenticator.objects.filter(
            user=user,
            status=UserAuthenticator.Status.PENDING,
            session_id=sid,
            created_at__gt=now - timedelta(seconds=SETUP_TTL_SECONDS),
        ).first()
        if row is None:
            raise NoSetup()
        step = _check_totp(user, row, code)
        row.status = UserAuthenticator.Status.ACTIVE
        row.confirmed_at = now
        row.last_used_step = step
        row.session_id = None
        row.save(update_fields=["status", "confirmed_at", "last_used_step", "session_id"])
    return row.confirmed_at


def check_authenticator_code(user, code):
    """Pass the account's confirmed authenticator's code, once. 422 method
    unavailable without one."""
    with transaction.atomic():
        row = (
            UserAuthenticator.objects.select_for_update()
            .filter(user=user, status=UserAuthenticator.Status.ACTIVE)
            .first()
        )
        if row is None:
            _field_error(*METHOD_UNAVAILABLE)
        row.last_used_step = _check_totp(user, row, code)
        row.save(update_fields=["last_used_step"])


def remove(user):
    """DELETE /me/security/authenticator: pending or confirmed, it goes.
    Nothing to remove is not an error. Returns whether a confirmed one went:
    a setup never finished is no news for a notice."""
    with transaction.atomic():
        _lock(user)
        removed = active_authenticator(user) is not None
        UserAuthenticator.objects.filter(user=user).delete()
    return removed


def _check_totp(user, row, code):
    """The 30-second step `code` is for, or 422 invalid. The current step
    and one either side pass, but never a step at or before the last one
    that passed: a code works once, even inside its window."""
    secret = _decrypt(row)  # 503 before a try is taken
    _take_try(user)
    totp = pyotp.TOTP(secret)
    now = totp.timecode(timezone.now())
    found = None
    # Every step compared, in constant time, so the time taken says nothing.
    for step in (now - 1, now, now + 1):
        if hmac.compare_digest(totp.generate_otp(step).encode(), code.encode()):
            if row.last_used_step is None or step > row.last_used_step:
                found = step
    if found is None:
        _field_error(*WRONG_CODE)
    _give_back_try(user)
    return found


def _lock(user):
    """Every change to an account's authenticator holds its account row, so
    two at once cannot both pass the checks before them."""
    ConsumerAccount.all_objects.select_for_update().only("pk").get(pk=user.pk)


def _key():
    raw = settings.TWO_FACTOR_ENCRYPTION_KEY
    try:
        # Standard or URL-safe base64.
        key = base64.b64decode(raw.replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != 32:
        logger.error(
            "TWO_FACTOR_ENCRYPTION_KEY is missing or not 32 bytes of base64: "
            "the authenticator app cannot be used (docs/TWO_FACTOR_SERVER.md)"
        )
        raise AuthenticatorUnavailable()
    return key


def _encrypt(user_id, secret):
    """nonce + AES-GCM of the secret. The account id is the associated
    data, so a secret copied onto another account's row does not open."""
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(_key()).encrypt(nonce, secret.encode(), str(user_id).encode())


def _decrypt(row):
    data = bytes(row.secret_encrypted)
    try:
        secret = AESGCM(_key()).decrypt(
            data[:_NONCE_BYTES], data[_NONCE_BYTES:], str(row.user_id).encode()
        )
    except InvalidTag:
        logger.error(
            "authenticator %s does not open with TWO_FACTOR_ENCRYPTION_KEY "
            "(was the key changed?)",
            row.pk,
        )
        raise AuthenticatorUnavailable()
    return secret.decode()


# --- limits ---------------------------------------------------------------------
# In the cache, as ratelimit.py's are, which stay as they are: these are new
# keys, and the IP is sessions.client_ip, which trusts X-Forwarded-For only
# from nginx.


def _take_try(user):
    """Spend one of the account's wrong-code tries BEFORE a code is checked,
    and 429 when none is left. A right code (or none to check) gives it back.
    Taken first, so codes sent at the same moment cannot all get through."""
    if _count(_TRIES_KEY.format(user.pk), WRONG_CODES_WINDOW) > WRONG_CODES:
        raise TooManyAttempts()


def _give_back_try(user):
    try:
        cache.decr(_TRIES_KEY.format(user.pk))
    except ValueError:  # the window ended meanwhile
        pass


def _limit_sends(user, purpose, ip):
    resend_key = f"2fa:resend:{user.pk}:{purpose}"
    if not cache.add(resend_key, 1, RESEND_SECONDS):
        raise TooManyAttempts(wait=RESEND_SECONDS)
    for key, limit, window in (
        (f"2fa:send:hour:{user.pk}", SENDS_PER_ACCOUNT_HOURLY, HOUR),
        (f"2fa:send:day:{user.pk}", SENDS_PER_ACCOUNT_DAILY, DAY),
        (f"2fa:send:ip:hour:{ip}" if ip else None, SENDS_PER_IP_HOURLY, HOUR),
    ):
        if key and _count(key, window) > limit:
            # Nothing was sent, so the 30 seconds have not started.
            cache.delete(resend_key)
            raise TooManyAttempts()


def _count(key, window):
    """Add one to a fixed-window counter and return it (ratelimit._bump's
    add, then incr)."""
    if cache.add(key, 1, timeout=window):
        return 1
    try:
        return cache.incr(key)
    except ValueError:  # expired between the add and the incr
        cache.set(key, 1, timeout=window)
        return 1


# --- the sign-in challenge ----------------------------------------------------------


def sign_in(user, request):
    """POST /auth/login for an account with 2-step on, once its password is
    right. Answers what login answers: the token pair at once for a device
    that has passed 2-step (its new session stays passed), else a challenge
    listing the ways the account can send or make a code.

    Any client gets the challenge, whatever X-App-Version it sends, or none:
    the code is what keeps the tokens back, not the app's version.
    """
    device_id = sessions.read_device(request).get("device_id")
    passed_at = _passed_at(user, device_id)
    if passed_at is not None:
        return services.tokens_for(user, request, two_factor_passed_at=passed_at)

    raw = CHALLENGE_PREFIX + secrets.token_urlsafe(32)
    LoginChallenge.objects.create(
        token_hash=_hash(raw),
        user=user,
        device_id=device_id,
        expires_at=timezone.now() + timedelta(seconds=CHALLENGE_TTL_SECONDS),
    )
    methods = [{"method": m, "hint": hint} for m, hint in contact_hints(user).items()]
    if active_authenticator(user) is not None:
        methods.append({"method": TwoFactorMethod.AUTHENTICATOR, "hint": None})
    return {
        "two_factor_required": True,
        "challenge_token": raw,
        "expires_in": CHALLENGE_TTL_SECONDS,
        "methods": methods,
    }


def _passed_at(user, device_id):
    """When this device passed 2-step, while it still has a session that
    did and has not ended (as /me/sessions counts them): a device is
    remembered until it signs out or is signed out. Never without an
    X-Device-Id."""
    if not device_id:
        return None
    return next(
        (
            s.two_factor_passed_at
            for s in sessions.live(user)
            if s.device_id == device_id and s.two_factor_passed_at
        ),
        None,
    )


def send_sign_in_code(raw_challenge, method, ip):
    """POST /auth/2fa/send: 3.1's rules, for the challenge's account."""
    user = _live_challenge(raw_challenge).user
    return send_code(user, method, TwoFactorPurpose.SIGN_IN, ip)


def finish_sign_in(raw_challenge, method, code, request):
    """POST /auth/2fa/verify: the challenge's code, from the device that
    signed in, answers the token pair. The session starts here and its
    device is remembered as having passed. A wrong code counts against the
    challenge, which 5 use up."""
    challenge = _live_challenge(raw_challenge)
    if challenge.device_id != sessions.read_device(request).get("device_id"):
        raise ChallengeExpired()
    user = challenge.user
    try:
        if method == TwoFactorMethod.AUTHENTICATOR:
            check_authenticator_code(user, code)
        else:
            check_code(user, method, TwoFactorPurpose.SIGN_IN, code)
    except ValidationError as e:
        if any(d.code == "invalid" for d in e.detail.get("code", [])):
            LoginChallenge.objects.filter(pk=challenge.pk).update(attempts=F("attempts") + 1)
        raise

    now = timezone.now()
    # Once: a second right code at the same moment finds it used.
    if not LoginChallenge.objects.filter(pk=challenge.pk, used_at__isnull=True).update(
        used_at=now
    ):
        raise ChallengeExpired()
    return services.tokens_for(user, request, two_factor_passed_at=now)


def _live_challenge(raw_challenge):
    """The challenge whose token this is, unused, unexpired, with tries
    left, of an account that may still sign in. Else 401 challenge_expired."""
    challenge = (
        LoginChallenge.objects.select_related("user")
        .filter(
            token_hash=_hash(raw_challenge or ""),
            used_at__isnull=True,
            expires_at__gt=timezone.now(),
            attempts__lt=CHALLENGE_WRONG_CODES,
        )
        .first()
    )
    if challenge is None or not challenge.user.is_active or challenge.user.is_deleted:
        raise ChallengeExpired()
    return challenge

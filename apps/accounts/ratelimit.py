"""Redis-backed rate limits for the OTP flow.

These run in the shared cache (Redis in production, local memory in dev/tests)
and are checked before any Postgres work so a flood never reaches the database.
A tripped limit raises DRF's Throttled, which the view renders as 429.
"""

from django.core.cache import cache
from rest_framework.exceptions import Throttled

HOUR = 3600
DAY = 86400

# request_otp limits.
COOLDOWN_SECONDS = 60          # min gap between codes for one (identifier, purpose)
PER_IDENTIFIER_HOURLY = 5
PER_IDENTIFIER_DAILY = 10
PER_IP_HOURLY = 20

# verify_otp limits.
VERIFY_PER_IP_HOURLY = 20


def _too_generic():
    # The wording never depends on the identifier or on account existence, so a
    # rate-limit response cannot be used to probe for accounts.
    return Throttled(detail="Too many requests. Please try again later.")


def _bump(key, limit, window):
    """Increment a fixed-window counter and raise once it exceeds `limit`.

    cache.add seeds the counter with its TTL; cache.incr keeps that TTL on
    every backend we use. The add/incr split (rather than a bare incr) is what
    lets the very first hit establish the expiry.
    """
    if cache.add(key, 1, timeout=window):
        count = 1
    else:
        try:
            count = cache.incr(key)
        except ValueError:
            # Expired between the add and the incr; treat as a fresh window.
            cache.set(key, 1, timeout=window)
            count = 1
    if count > limit:
        raise _too_generic()


def enforce_request_otp(identifier, purpose, ip):
    # Cooldown first: it is the cheapest gate and the most common rejection.
    if not cache.add(f"otp:cooldown:{purpose}:{identifier}", 1, COOLDOWN_SECONDS):
        raise Throttled(
            wait=COOLDOWN_SECONDS,
            detail="Please wait before requesting another code.",
        )
    _bump(f"otp:id:hour:{identifier}", PER_IDENTIFIER_HOURLY, HOUR)
    _bump(f"otp:id:day:{identifier}", PER_IDENTIFIER_DAILY, DAY)
    if ip:
        _bump(f"otp:ip:hour:{ip}", PER_IP_HOURLY, HOUR)


def enforce_verify_otp(ip):
    if ip:
        _bump(f"otpverify:ip:hour:{ip}", VERIFY_PER_IP_HOURLY, HOUR)

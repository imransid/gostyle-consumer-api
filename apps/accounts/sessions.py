"""
Where you're signed in: one session per sign-in on one device
(docs/DEVICE_SESSIONS_API.md), behind DEVICE_SESSIONS_V1.

Everything that opens, checks or ends a UserSession is here:

  * open: whenever the API issues a token pair. Login and register
    (services.tokens_for), and the refresh of a token issued before
    sessions existed (refreshed).
  * check: every authenticated request (authentication.py), every refresh
    (refuse_if_revoked) and booking-api's VerifyConsumer over gRPC.
  * end: logout, Sign Out from another device, a newer sign-in on the same
    device, a password reset (which revokes every refresh token).

The session's id travels in both tokens as the `sid` claim. While the
switch is on, an access token without one (issued before, or while it was
off) is refused like an expired token, so the app refreshes it, and that
refresh opens its session. Every token in use then has one.
"""

import hashlib
import hmac
import ipaddress
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, AuthenticationFailed, NotFound
from rest_framework.settings import api_settings as drf_settings
from rest_framework_simplejwt.exceptions import InvalidToken, TokenBackendError, TokenError
from rest_framework_simplejwt.settings import api_settings as jwt_settings
from rest_framework_simplejwt.state import token_backend
from rest_framework_simplejwt.tokens import RefreshToken, UntypedToken

from . import geoip, push_api
from .models import ConsumerAccount, UserSession

logger = logging.getLogger(__name__)

SID_CLAIM = "sid"

# last_seen_at moves at most this often per session, to spare writes. It is
# also how long a request trusts the cache that its session is live.
TOUCH_EVERY = timedelta(minutes=5)
ENDED_TTL = timedelta(hours=1)
_CACHE_KEY = "consumer-session:{}"
_LIVE, _ENDED = "live", "ended"

# The IP the session's row holds now, so a request writes ip_address only
# when its IP is a new one.
_IP_KEY = "consumer-session-ip:{}"
IP_TTL = timedelta(days=1)
# The IP of the session's last place lookup, so a refresh or a last_seen_at
# update looks the place up again only when the IP has changed since. Lost,
# it costs one lookup (microseconds) that finds the same place.
_GEO_KEY = "consumer-session-geo:{}"
GEO_TTL = timedelta(days=30)

# Signed-out sessions whose devices the push service could not be told to
# remove yet (_remove_push), retried on the next push call.
_PUSH_PENDING_KEY = "consumer-session-push-pending"
PUSH_PENDING_TTL = timedelta(days=30)

# Header -> (field, longest value kept).
_DEVICE_HEADERS = (
    ("HTTP_X_DEVICE_ID", "device_id", 128),
    ("HTTP_X_DEVICE_NAME", "device_name", 100),
    ("HTTP_X_DEVICE_TYPE", "device_type", 10),
    ("HTTP_X_PLATFORM", "platform", 10),
    ("HTTP_X_OS_VERSION", "os_version", 50),
    ("HTTP_X_APP_VERSION", "app_version", 50),
    ("HTTP_X_APP_BUILD", "app_build", 50),
)

# User-Agent token -> name. Order matters: Edge and Opera also say Chrome,
# Chrome also says Safari, Android also says Linux, an iPhone also says
# Mac OS X.
_BROWSERS = (
    ("Edg", "Edge"),
    ("OPR/", "Opera"),
    ("Firefox/", "Firefox"),
    ("FxiOS/", "Firefox"),
    ("CriOS/", "Chrome"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)
_SYSTEMS = (
    ("iPhone", "iOS"),
    ("iPad", "iPadOS"),
    ("Android", "Android"),
    ("CrOS", "ChromeOS"),
    ("Mac OS X", "macOS"),
    ("Windows", "Windows"),
    ("Linux", "Linux"),
)


def enabled():
    return settings.DEVICE_SESSIONS_V1


# --- errors ---------------------------------------------------------------
# Each carries envelope_code: exceptions.py answers it as the top-level code
# with an empty errors list, since none of these belongs to a field.


class SessionRevoked(AuthenticationFailed):
    """401 for a token whose session has ended."""

    default_detail = "You’ve been signed out on this device."
    default_code = envelope_code = "session_revoked"


class NotSignedIn(AuthenticationFailed):
    """401 on the sessions routes for no token, or an invalid one."""

    default_detail = "Please sign in again."
    default_code = envelope_code = "not_authenticated"


class SessionNotFound(NotFound):
    default_detail = "That device isn’t signed in anymore."
    default_code = envelope_code = "not_found"


class CurrentSession(APIException):
    """409: the caller asked to sign out their own session from the list."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "Use Sign Out to leave this device."
    default_code = envelope_code = "current_session"


def refuse_token_without_session():
    """An access token with no sid, while sessions are on: refused with the
    very 401 simplejwt gives an invalid or expired token, so the app does
    what it already does for one (refresh, then retry). The refresh opens
    the session (refreshed)."""
    raise InvalidToken(
        {
            "detail": "Given token not valid for any token type",
            "messages": [
                {
                    "token_class": "AccessToken",
                    "token_type": "access",
                    "message": "Token has no session",
                }
            ],
        }
    )


# --- what a request says ----------------------------------------------------


def read_device(request):
    """What this request says about its device: only the fields it sent.

    Every header is optional. A value that is empty, or not one of the
    allowed ones, is the same as no header: nothing here fails a request.
    Only printable ASCII is kept (the app strips the rest already, because
    Android refuses anything else in a header).
    """
    fields = {}
    for header, field, limit in _DEVICE_HEADERS:
        raw = request.META.get(header, "")
        value = "".join(ch for ch in raw if " " <= ch <= "~").strip()[:limit].strip()
        if value:
            fields[field] = value

    for field, allowed in (
        ("device_type", UserSession.DeviceType.values),
        ("platform", UserSession.Platform.values),
    ):
        if field in fields:
            value = fields.pop(field).lower()
            if value in allowed:
                fields[field] = value
    return fields


def _device_for_new_session(request):
    """read_device, and for a web browser, which sends no X-Device-* headers,
    a name from its User-Agent ("Chrome on macOS").

    Only when a session opens: a phone's token pasted into a browser later
    must not rename the phone.
    """
    device = read_device(request)
    if "platform" not in device and "device_name" not in device:
        name = _browser_name(request.META.get("HTTP_USER_AGENT", ""))
        if name:
            device.update(
                device_name=name,
                device_type=UserSession.DeviceType.BROWSER,
                platform=UserSession.Platform.WEB,
            )
    return device


def _browser_name(user_agent):
    """'Chrome on macOS' for a web browser's User-Agent, else None. The app's
    own requests (CFNetwork, okhttp) are not browsers."""
    if not user_agent.startswith("Mozilla/"):
        return None
    browser = next((name for token, name in _BROWSERS if token in user_agent), None)
    if browser is None:
        return None
    system = next((name for token, name in _SYSTEMS if token in user_agent), None)
    return f"{browser} on {system}" if system else browser


def client_ip(request):
    """The client's real address, or None.

    Behind nginx the socket address is the proxy's. nginx appends the address
    it saw to X-Forwarded-For, so the entry NUM_PROXIES from the right is the
    client; the entries left of it are whatever the client wrote, so never
    the left-most. But only when the request really came through nginx: port
    3850 is published, and a request sent straight to it could write any
    X-Forwarded-For it likes. nginx proves itself with X-Proxy-Secret, equal
    to PROXY_SECRET (docs/DEVICE_SESSIONS_SERVER.md). Otherwise, and without
    NUM_PROXIES (local), it is the socket address.
    """
    meta = request.META
    forwarded = meta.get("HTTP_X_FORWARDED_FOR")
    num_proxies = drf_settings.NUM_PROXIES
    if num_proxies and forwarded and _came_through_proxy(meta):
        addresses = [a.strip() for a in forwarded.split(",")]
        raw = addresses[-min(num_proxies, len(addresses))]
    else:
        raw = meta.get("REMOTE_ADDR")
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


def _came_through_proxy(meta):
    secret = settings.PROXY_SECRET
    presented = meta.get("HTTP_X_PROXY_SECRET", "")
    return bool(secret) and hmac.compare_digest(presented.encode(), secret.encode())


def parse_id(value):
    """A session id as a UUID, or None for anything that is not one."""
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def current_id(request):
    """The id of the session the bearer token belongs to, or None."""
    token = request.auth
    return parse_id(token.get(SID_CLAIM)) if token is not None else None


def _hash(raw_token):
    return hashlib.sha256(raw_token.encode()).hexdigest()


# --- open ---------------------------------------------------------------------


def start(user, request):
    """Open a session for a sign-in and return its token pair."""
    refresh = RefreshToken.for_user(user)
    _open(user, request, refresh)
    return _pair(refresh)


def _pair(refresh):
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def _open(user, request, refresh):
    """Create the session that owns `refresh`, and stamp its id on it.

    One live session per device: a sign-in from a device that has one ends
    that one first. Without a device id every sign-in is its own.
    """
    from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

    ip = client_ip(request)
    session = UserSession(user=user, **_device_for_new_session(request), ip_address=ip)
    session.location = _place(session, ip).get("location")
    refresh[SID_CLAIM] = str(session.id)
    raw = str(refresh)
    session.refresh_token_hash = _hash(raw)

    for attempt in range(2):
        try:
            with transaction.atomic():
                replaced = []
                if session.device_id:
                    replaced = _close(
                        UserSession.objects.filter(user=user, device_id=session.device_id)
                    )
                session.save(force_insert=True)
                # simplejwt saved the token before it had a sid. Saved again
                # with it, so its hash is the session's and end_others can
                # tell it from an older token.
                OutstandingToken.objects.filter(jti=refresh[jwt_settings.JTI_CLAIM]).update(
                    token=raw
                )
            break
        except IntegrityError:
            # Another sign-in from this device opened its session between
            # the close and the save. A second pass closes that one.
            if attempt:
                raise
    _mark_ended(replaced)
    _cache_set(_IP_KEY.format(session.pk), ip or "", IP_TTL)
    return session


# --- check --------------------------------------------------------------------


def check(sid, request):
    """Refuse a request whose session has ended (SessionRevoked), keep
    ip_address the last request's IP, and move last_seen_at at most once
    every TOUCH_EVERY.

    The cache holds the answer for a while: "live" for TOUCH_EVERY, which is
    also what spaces the writes, and "ended" from the moment a session ends.
    Ending one writes "ended" at once, so Sign Out stops the other phone on
    its very next request, not when its access token expires.
    """
    key, ip_key = _CACHE_KEY.format(sid), _IP_KEY.format(sid)
    cached = _cache_get_many([key, ip_key])
    state = cached.get(key)
    if state == _ENDED:
        raise SessionRevoked()
    ip = client_ip(request)

    if state == _LIVE:
        if ip and ip != cached.get(ip_key):
            moved = UserSession.objects.filter(pk=sid, ended_at__isnull=True).update(
                ip_address=ip
            )
            if not moved:  # ended since "live" was cached
                _cache_set(key, _ENDED, ENDED_TTL)
                raise SessionRevoked()
            _cache_set(ip_key, ip, IP_TTL)
        return

    session = _find(sid)
    if session is None or session.ended_at is not None:
        _cache_set(key, _ENDED, ENDED_TTL)
        raise SessionRevoked()
    fields = {}
    if ip and ip != session.ip_address:
        fields["ip_address"] = ip
    if timezone.now() - session.last_seen_at >= TOUCH_EVERY:
        fields.update(last_seen_at=timezone.now(), **_place(session, ip))
    if fields:
        UserSession.objects.filter(pk=session.pk, ended_at__isnull=True).update(**fields)
    _cache_set(ip_key, ip or session.ip_address or "", IP_TTL)
    # add, not set: a Sign Out that wrote "ended" since the read above wins.
    _cache_add(key, _LIVE, TOUCH_EVERY)


def is_live(sid):
    """check() without moving last_seen_at, for VerifyConsumer."""
    state = _cache_get(_CACHE_KEY.format(sid))
    if state is not None:
        return state == _LIVE
    session = _find(sid)
    return session is not None and session.ended_at is None


def _find(sid):
    session_id = parse_id(sid)
    if session_id is None:
        return None
    return UserSession.objects.filter(pk=session_id).first()


def _place(session, ip):
    """{"location": ...} for `ip`, looked up only when the IP has changed
    since the session's last lookup: a phone on the same network causes
    none. A private or reserved address is no lookup and keeps the place it
    had. Called when a session starts, on a refresh and on a last_seen_at
    update, the three times the brief looks it up."""
    if not ip:
        return {}
    geo_key = _GEO_KEY.format(session.pk)
    if ip == _cache_get(geo_key):
        return {}
    place = geoip.location(ip)
    if place is geoip.KEEP:
        return {}
    _cache_set(geo_key, ip, GEO_TTL)
    return {"location": place}


# --- refresh ------------------------------------------------------------------


def refuse_if_revoked(raw_refresh):
    """SessionRevoked for a refresh token whose session has ended.

    The session is found by the token's hash, as a rotation finds it, or by
    its sid once a rotation has moved the hash on. Runs before simplejwt's
    own checks, so a signed-out device hears session_revoked and not "Token
    is blacklisted". A token that is not valid at all is left to simplejwt.
    """
    try:
        token = UntypedToken(raw_refresh)
    except TokenError:
        return
    if token.get(jwt_settings.TOKEN_TYPE_CLAIM) != "refresh":
        return
    session = UserSession.objects.filter(refresh_token_hash=_hash(raw_refresh)).first()
    if session is None:
        sid = token.get(SID_CLAIM)
        if sid is None:
            return  # older than sessions: refreshed() opens one
        session = _find(sid)
        if session is None:
            raise SessionRevoked()
    if session.ended_at is not None:
        raise SessionRevoked()


def refreshed(raw_refresh, data, request):
    """After simplejwt rotated the tokens: the same session goes on, or one
    opens for a token older than sessions. Returns the answer to send.

    The session now holds the new refresh token, and takes this request's
    IP, place and device fields (an updated app shows its new version without
    signing in again), and last_seen_at at most once every TOUCH_EVERY.
    """
    if "refresh" not in data:
        return data
    # Just issued by simplejwt from a token it verified.
    refresh = RefreshToken(data["refresh"], verify=False)
    session = UserSession.objects.filter(
        refresh_token_hash=_hash(raw_refresh), ended_at__isnull=True
    ).first()
    if session is None:
        if refresh.get(SID_CLAIM) is None:
            user = ConsumerAccount.objects.get(pk=refresh[jwt_settings.USER_ID_CLAIM])
            _open(user, request, refresh)
            return _pair(refresh)
        return data  # ended meanwhile: its next request answers session_revoked

    now = timezone.now()
    ip = client_ip(request)
    fields = {
        "refresh_token_hash": _hash(data["refresh"]),
        **read_device(request),
        **_place(session, ip),
    }
    if ip:
        fields["ip_address"] = ip
    if now - session.last_seen_at >= TOUCH_EVERY:
        fields["last_seen_at"] = now

    replaced = []
    with transaction.atomic():
        # A new X-Device-Id (a rebuilt app that sends one now): one live
        # session per device, so another one on it ends, as a newer
        # sign-in would end it.
        device_id = fields.get("device_id")
        if device_id and device_id != session.device_id:
            replaced = _close(
                UserSession.objects.filter(user_id=session.user_id, device_id=device_id).exclude(
                    pk=session.pk
                )
            )
        UserSession.objects.filter(pk=session.pk, ended_at__isnull=True).update(**fields)
    _mark_ended(replaced)
    if ip:
        _cache_set(_IP_KEY.format(session.pk), ip, IP_TTL)
    return data


# --- end ----------------------------------------------------------------------


def live(user):
    """The user's sessions that have not ended, newest visit first.

    Not ended means no ended_at, and its refresh token still good: neither
    expired nor revoked, in simplejwt's outstanding-token list.
    """
    good = _good_refresh_hashes(user)
    sessions = UserSession.objects.filter(user=user, ended_at__isnull=True).order_by(
        "-last_seen_at", "-created_at"
    )
    return [s for s in sessions if s.refresh_token_hash in good]


def end(sessions, *, remove_push=False):
    """End the live ones among `sessions`: ended_at, their refresh tokens
    revoked, and any request with their tokens refused from now on. Returns
    them.

    remove_push also has the push service delete the push tokens they
    registered: Sign Out from another device and Sign out of all other
    devices, the two the brief has do it.
    """
    with transaction.atomic():
        closed = _close(sessions)
    _mark_ended(closed)
    if remove_push:
        _remove_push([s.pk for s in closed])
    return closed


def end_current(request):
    """End the session the bearer token belongs to (logout)."""
    sid = current_id(request)
    if sid is not None:
        end(UserSession.objects.filter(pk=sid, user=request.user))


def end_others(user, current):
    """Sign out every device of the user except the session `current`.
    Returns how many were signed in."""
    good = _good_refresh_hashes(user)
    others = UserSession.objects.filter(user=user)
    if current is not None:
        others = others.exclude(pk=current)
    closed = end(others, remove_push=True)
    if current is not None:
        _revoke_tokens_without_session(user)
    return sum(1 for s in closed if s.refresh_token_hash in good)


def _close(sessions):
    """The database half of ending: mark the live ones among `sessions`
    ended and revoke their refresh tokens. Returns them. Must run inside a
    transaction (select_for_update)."""
    closed = list(sessions.select_for_update().filter(ended_at__isnull=True))
    if closed:
        UserSession.objects.filter(pk__in=[s.pk for s in closed]).update(ended_at=timezone.now())
        hashes = {s.refresh_token_hash for s in closed}
        for user_id in {s.user_id for s in closed}:
            for token in _outstanding(user_id):
                if _hash(token.token) in hashes:
                    _blacklist(token)
    return closed


def _outstanding(user_id):
    """The user's refresh tokens that are neither expired nor revoked."""
    # Imported here: token_blacklist models load only once apps are ready.
    from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

    return OutstandingToken.objects.filter(
        user_id=user_id, expires_at__gt=timezone.now(), blacklistedtoken__isnull=True
    )


def _good_refresh_hashes(user):
    return {_hash(token.token) for token in _outstanding(user.pk)}


def _blacklist(token):
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

    BlacklistedToken.objects.get_or_create(token=token)


def _revoke_tokens_without_session(user):
    """Revoke the user's live refresh tokens that carry no sid.

    They were issued before sessions (or while the switch was off), to
    devices the list cannot show yet, and Sign out of all other devices must
    reach them too: without this each would come back as a new session on
    its next refresh. Their access tokens are already refused (no sid).
    Only called when the caller has a sid, so the caller's own token is
    never one of them.
    """
    for token in _outstanding(user.pk):
        try:
            claims = token_backend.decode(token.token, verify=False)
        except TokenBackendError:
            claims = {}
        if SID_CLAIM not in claims:
            _blacklist(token)


def _mark_ended(closed):
    for session in closed:
        _cache_set(_CACHE_KEY.format(session.pk), _ENDED, ENDED_TTL)


# --- push tokens --------------------------------------------------------------


def retry_pending_push():
    """After the push service has just answered: tell it about the signed-out
    sessions it could not be reached for before."""
    _remove_push([])


def _remove_push(session_ids):
    """Have the push service delete the push tokens these sessions
    registered (POST /devices/unregister-session), and those of sessions
    signed out while it could not be reached, so a signed-out phone's tokens
    go as soon as the service is back.

    A session is done once the service has answered for it. While the
    service is down the rest wait in the cache for the next push call, not
    one PUSH_API_TIMEOUT each. Two sign-outs during an outage can race on
    that list; the loser's session then waits for nobody. Rare enough.
    """
    pending = [str(i) for i in session_ids]
    pending += [i for i in (_cache_get(_PUSH_PENDING_KEY) or []) if i not in pending]
    if not pending:
        return
    left = []
    for done, session_id in enumerate(pending):
        try:
            status_code, body = push_api.unregister_session(session_id=session_id)
        except push_api.PushApiUnavailable as e:
            logger.warning(
                "push service unavailable: %d signed-out session(s) wait: %s",
                len(pending) - done,
                e,
            )
            left += pending[done:]
            break
        if status_code >= 500:
            logger.warning("push service could not unregister a session (%s): %s", status_code, body)
            left.append(session_id)
    if left:
        _cache_set(_PUSH_PENDING_KEY, left, PUSH_PENDING_TTL)
    else:
        _cache_delete(_PUSH_PENDING_KEY)


# --- cache --------------------------------------------------------------------
# The cache only spares the database. When it fails, every check reads the
# row instead, and no request fails because of it.


def _cache_get(key):
    try:
        return cache.get(key)
    except Exception as e:  # noqa: BLE001
        logger.warning("session cache read failed: %s", e)
        return None


def _cache_get_many(keys):
    try:
        return cache.get_many(keys)
    except Exception as e:  # noqa: BLE001
        logger.warning("session cache read failed: %s", e)
        return {}


def _cache_delete(key):
    try:
        cache.delete(key)
    except Exception as e:  # noqa: BLE001
        logger.warning("session cache write failed: %s", e)


def _cache_set(key, value, ttl):
    try:
        cache.set(key, value, timeout=ttl.total_seconds())
    except Exception as e:  # noqa: BLE001
        logger.warning("session cache write failed: %s", e)


def _cache_add(key, value, ttl):
    try:
        cache.add(key, value, timeout=ttl.total_seconds())
    except Exception as e:  # noqa: BLE001
        logger.warning("session cache write failed: %s", e)

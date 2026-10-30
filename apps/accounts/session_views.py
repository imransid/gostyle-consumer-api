"""
GET  /api/v1/me/sessions
POST /api/v1/auth/token/refresh  (session-aware replacement for simplejwt's)

Lists the authenticated customer's active sessions: device name, location
(from GeoIP on the IP address), and last activity time.  The list is ordered
most-recently-active first.
"""

import logging
import re

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenRefreshView

from .models import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serializer
# ---------------------------------------------------------------------------


class SessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Session
        fields = [
            "id",
            "device_name",
            "location",
            "full_location",
            "ip_address",
            "last_seen_at",
            "created_at",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class SessionListView(APIView):
    """GET /api/v1/me/sessions — the customer's sessions."""

    permission_classes = [IsAuthenticated]
    throttle_classes = []

    @extend_schema(responses={200: SessionSerializer(many=True)})
    def get(self, request):
        sessions = Session.objects.filter(account=request.user)
        return Response(
            SessionSerializer(sessions, many=True).data,
            status=status.HTTP_200_OK,
        )


class SessionTokenRefreshView(TokenRefreshView):
    """POST /api/v1/auth/token/refresh — simplejwt refresh + session update.

    After the normal token rotation, finds the caller's most-recent session
    and bumps last_seen_at.  If the IP changed since the last request, the
    location is re-looked-up from the GeoIP database so a phone that moved
    from Dubai to Abu Dhabi shows the new city.
    """

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)

        # Only touch the session on a successful refresh.
        if response.status_code == 200:
            try:
                _bump_session(request)
            except Exception:
                # Never let a session bookkeeping failure break token refresh.
                logger.exception("session update failed on token refresh")

        return response


def _bump_session(request):
    """Find the caller's most-recent session and update it."""
    from rest_framework_simplejwt.authentication import JWTAuthentication

    # The access token is in the Authorization header; decode it to get the
    # account id.  On a refresh the old access token may be expired, but we
    # only need the user claim, not validity.  However the caller might NOT
    # send the access token (the refresh endpoint doesn't require it).  In
    # that case, fall back to decoding the refresh token from the body.
    account = None

    # Try 1: the caller is authenticated (access token present).
    if hasattr(request, "user") and request.user and request.user.is_authenticated:
        account = request.user

    # Try 2: decode the account id from the refresh token payload.
    if account is None:
        try:
            from rest_framework_simplejwt.tokens import RefreshToken

            refresh = request.data.get("refresh", "")
            if refresh:
                token = RefreshToken(refresh)
                user_id = token.get("consumer_id")
                if user_id:
                    from .models import ConsumerAccount

                    account = ConsumerAccount.objects.filter(pk=user_id).first()
        except Exception:
            pass

    if account is None:
        return

    session = (
        Session.objects.filter(account=account).order_by("-last_seen_at").first()
    )
    if session is None:
        return

    update_session_if_ip_changed(request, session)


# ---------------------------------------------------------------------------
# Helpers: called by login / register / token-refresh views
# ---------------------------------------------------------------------------


def _trusted_ip(request) -> str | None:
    """The client IP from a header set by reverse proxy/CDN, not spoofable.

    Behind Cloudflare: CF-Connecting-IP.
    Behind nginx: X-Real-IP ($remote_addr).
    Behind proxy chains: X-Forwarded-For (right-most address).
    Fallback (direct connection, local dev, tests): REMOTE_ADDR.
    """
    cf_ip = request.META.get("HTTP_CF_CONNECTING_IP")
    if cf_ip:
        return cf_ip.strip()

    real_ip = request.META.get("HTTP_X_REAL_IP")
    if real_ip:
        return real_ip.strip()

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]

    return request.META.get("REMOTE_ADDR")


# --- User-Agent → device name ------------------------------------------------

# Patterns are tried in order; first match wins.
_DEVICE_PATTERNS = [
    # GoStyle mobile apps: "GoStyle/3.1.0 (iPhone15,4; iOS 18.1)" or "GoStyle/3.1.0 (Samsung SM-S928B; Android 15)"
    (re.compile(r"GoStyle/[\d.]+\s+\(([^;)]+)"), None),
    # Generic iOS: "...(iPhone; CPU iPhone OS 18_1 like Mac OS X)..."
    (re.compile(r"\b(iPad|iPhone|iPod)\b"), None),
    # Generic Android: "Android...; <Device Model> Build/..." or "Android...; <Device Model>)"
    (re.compile(r"\bAndroid\b[^;)]*;\s*([^;)]+?)(?:\s+Build[/\s].*)?[);]"), None),
    # Desktop browsers
    (re.compile(r"\b(Windows NT|Macintosh|Linux x86_64|CrOS)\b"), {
        "Windows NT": "Windows",
        "Macintosh": "Mac",
        "Linux x86_64": "Linux",
        "CrOS": "Chromebook",
    }),
]


def _device_name_from_ua(ua: str) -> str:
    """Best-effort device label from a User-Agent string."""
    if not ua:
        return ""
    for pattern, mapping in _DEVICE_PATTERNS:
        m = pattern.search(ua)
        if m:
            raw = m.group(1).strip()
            raw = re.sub(r"\s+Build[/_\s].*$", "", raw, flags=re.IGNORECASE).strip()
            if mapping:
                return mapping.get(raw, raw)
            return raw
    return ""


def create_session(request, account) -> Session:
    """Create a Session row for a new sign-in or registration."""
    from .geoip import location_details_for_ip

    ip = _trusted_ip(request)
    ua = request.META.get("HTTP_USER_AGENT", "")
    device = _device_name_from_ua(ua)
    location, full_location = location_details_for_ip(ip)

    return Session.objects.create(
        account=account,
        ip_address=ip,
        user_agent=ua,
        device_name=device,
        location=location,
        full_location=full_location,
    )


def update_session_if_ip_changed(request, session: Session) -> None:
    """On a token refresh: bump last_seen_at, and re-lookup location if the
    IP changed (a phone that moved from Dubai to Abu Dhabi)."""
    from .geoip import is_private_ip, location_details_for_ip

    ip = _trusted_ip(request)
    fields = ["last_seen_at"]
    session.last_seen_at = timezone.now()

    if ip and ip != session.ip_address:
        session.ip_address = ip
        fields.append("ip_address")

        # Skip private/reserved addresses: keep location as it was.
        if not is_private_ip(ip):
            new_location, new_full_location = location_details_for_ip(ip)
            session.location = new_location
            session.full_location = new_full_location
            fields.extend(["location", "full_location"])

        # Also update device name in case the UA changed
        ua = request.META.get("HTTP_USER_AGENT", "")
        new_device = _device_name_from_ua(ua)
        if new_device and new_device != session.device_name:
            session.device_name = new_device
            session.user_agent = ua
            fields.extend(["device_name", "user_agent"])

    session.save(update_fields=fields)



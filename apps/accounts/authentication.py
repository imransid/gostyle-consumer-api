from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme
from rest_framework_simplejwt.authentication import JWTAuthentication

from . import sessions


class SessionJWTAuthentication(JWTAuthentication):
    """JWTAuthentication, and while DEVICE_SESSIONS_V1 is on, a token must
    belong to a session that has not ended.

    401 session_revoked on EVERY authenticated request, not only on refresh:
    otherwise Sign Out would leave the other phone working until its access
    token expires, seven days later. A token with no sid at all (issued
    before sessions, or while the switch was off) gets the 401 of an invalid
    token, so the app refreshes it and the refresh opens its session.
    apps/accounts/sessions.py, docs/DEVICE_SESSIONS_API.md.
    """

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None and sessions.enabled():
            sid = result[1].get(sessions.SID_CLAIM)
            if sid is None:
                sessions.refuse_token_without_session()
            sessions.check(sid, request)
        return result


class SessionJWTScheme(SimpleJWTScheme):
    """Swagger: the same jwtAuth scheme (the Authorize button) as simplejwt's
    own class, which drf-spectacular matches by exact class only."""

    target_class = "apps.accounts.authentication.SessionJWTAuthentication"

"""
Where you're signed in (docs/DEVICE_SESSIONS_API.md):

    GET    /api/v1/me/sessions        every device signed in, this one first
    DELETE /api/v1/me/sessions/{id}   sign one other device out
    DELETE /api/v1/me/sessions        sign out every device except this one

"This device" is always the session the bearer token belongs to (its sid).
Behind DEVICE_SESSIONS_V1: off, all three are 404. The work is in
apps/accounts/sessions.py.
"""

from datetime import timedelta, timezone

from django.http import Http404
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.views import TokenRefreshView

from . import sessions
from .models import UserSession

# The brief's times: UAE time with whole seconds, 2026-10-27T11:58:03+04:00.
# A fixed offset, not a zone: the UAE keeps no daylight saving time.
UAE = timezone(timedelta(hours=4), "UAE")


class _UaeDateTime(serializers.DateTimeField):
    def to_representation(self, value):
        return value.astimezone(UAE).replace(microsecond=0).isoformat()


class SessionSerializer(serializers.ModelSerializer):
    # Plain strings in Swagger: an enum for a field named "platform" would
    # rename the published PlatformEnum of /me/device-tokens.
    device_type = serializers.CharField(
        read_only=True, help_text="phone, tablet, browser, desktop or unknown"
    )
    platform = serializers.CharField(
        read_only=True, allow_null=True, help_text="ios, android or web"
    )
    is_current = serializers.SerializerMethodField()
    created_at = _UaeDateTime(read_only=True)
    last_seen_at = _UaeDateTime(read_only=True)

    class Meta:
        model = UserSession
        fields = [
            "id",
            "device_name",
            "device_type",
            "platform",
            "os_version",
            "app_version",
            "ip_address",
            "location",
            "is_current",
            "created_at",
            "last_seen_at",
        ]

    def get_is_current(self, session) -> bool:
        return session.pk == self.context["current"]


class _SessionsView(APIView):
    """404 while the switch is off, and the 401s in this API's own words:
    no token or an invalid one is not_authenticated "Please sign in again.",
    with no errors. session_revoked stays as it is."""

    def initial(self, request, *args, **kwargs):
        if not sessions.enabled():
            raise Http404("Not found")
        super().initial(request, *args, **kwargs)

    def perform_authentication(self, request):
        try:
            request.user
        except sessions.SessionRevoked:
            raise
        except AuthenticationFailed as e:
            raise sessions.NotSignedIn() from e

    def permission_denied(self, request, message=None, code=None):
        if request.authenticators and not request.successful_authenticator:
            raise sessions.NotSignedIn()
        super().permission_denied(request, message=message, code=code)


_ERRORS = {
    401: OpenApiResponse(
        description="`session_revoked` (this device was signed out) or `not_authenticated`."
    ),
}


class SessionsView(_SessionsView):
    @extend_schema(
        description=(
            "Every device signed in to this account: the current one first "
            "(`is_current`), then the others by `last_seen_at`, most recent "
            "first. All in one page: `next` and `previous` are always null."
        ),
        responses={
            200: inline_serializer(
                "SessionPage",
                {
                    "count": serializers.IntegerField(),
                    "next": serializers.CharField(allow_null=True),
                    "previous": serializers.CharField(allow_null=True),
                    "results": SessionSerializer(many=True),
                },
            ),
            **_ERRORS,
        },
    )
    def get(self, request):
        current = sessions.current_id(request)
        rows = sessions.live(request.user)  # newest visit first
        rows.sort(key=lambda s: s.pk != current)  # stable: the rest keep their order
        results = SessionSerializer(rows, many=True, context={"current": current}).data
        return Response(
            {"count": len(results), "next": None, "previous": None, "results": results}
        )

    @extend_schema(
        operation_id="v1_me_sessions_sign_out_others",
        request=None,
        description=(
            "Signs out every device except this one, push tokens included. "
            "Bookings and wallet credit are not touched. 200 with "
            '`{"revoked": 0}` when there is no other device.'
        ),
        responses={
            200: inline_serializer("SessionsRevoked", {"revoked": serializers.IntegerField()}),
            **_ERRORS,
        },
    )
    def delete(self, request):
        revoked = sessions.end_others(request.user, sessions.current_id(request))
        return Response({"revoked": revoked}, status=status.HTTP_200_OK)


class SessionDetailView(_SessionsView):
    @extend_schema(
        operation_id="v1_me_sessions_sign_out_one",
        request=None,
        description=(
            "Signs one other device out: its refresh token is revoked, its "
            "very next request answers 401 `session_revoked`, and its push "
            "tokens are removed. 204 also when it had already ended."
        ),
        responses={
            204: None,
            **_ERRORS,
            404: OpenApiResponse(description="`not_found`: not a session of this account."),
            409: OpenApiResponse(description="`current_session`: this device's own session."),
        },
    )
    def delete(self, request, session_id):
        target = sessions.parse_id(session_id)
        if target is not None and target == sessions.current_id(request):
            raise sessions.CurrentSession()
        mine = UserSession.objects.filter(pk=target, user=request.user)
        if target is None or not mine.exists():
            raise sessions.SessionNotFound()
        sessions.end(mine, remove_push=True)
        return Response(status=status.HTTP_204_NO_CONTENT)


class SessionTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        if not sessions.enabled():
            return super().validate(attrs)
        sessions.refuse_if_revoked(attrs["refresh"])
        data = super().validate(attrs)
        return sessions.refreshed(attrs["refresh"], data, self.context["request"])


class SessionTokenRefreshView(TokenRefreshView):
    """POST /auth/token/refresh, simplejwt's, keeping the session: a refresh
    token whose session has ended is 401 session_revoked, and the session
    takes the new refresh token and this request's device fields."""

    serializer_class = SessionTokenRefreshSerializer

    # The body and answer are simplejwt's, under the name Swagger had.
    @extend_schema(request=TokenRefreshSerializer, responses={200: TokenRefreshSerializer})
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

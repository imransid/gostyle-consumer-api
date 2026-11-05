"""
Sign in with Google (GOOGLE_SIGN_IN_V1, docs/GOOGLE_SIGN_IN_API.md):

    POST /api/v1/auth/google         sign in or sign up with a Google ID token
    POST /api/v1/auth/password/set   an account made with Google adds a password

Off, both are 404. The rules are in apps/accounts/google.py.
"""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import ErrorDetail, PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import google, services, sessions, two_factor
from .models import TwoFactorPurpose


class GoogleSignInSerializer(serializers.Serializer):
    id_token = serializers.CharField(max_length=8192, help_text="The Google ID token.")


class PasswordSetSerializer(serializers.Serializer):
    # Optional here: none at all is the 403 a wrong one is, not a 422.
    verification_token = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="From POST /me/security/verify with purpose password_set.",
    )
    new_password = serializers.CharField(max_length=128, write_only=True)
    confirm_password = serializers.CharField(max_length=128, write_only=True)

    def validate_new_password(self, value):
        # The rules of sign-up (RegisterSerializer): 8+, uppercase, a number, a symbol.
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value

    def validate(self, attrs):
        # As at sign-up: the two differ is an error on the confirmation.
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "Passwords do not match."})
        return attrs


class _GoogleView(APIView):
    def initial(self, request, *args, **kwargs):
        if not google.enabled():
            raise Http404("Not found")
        super().initial(request, *args, **kwargs)


class GoogleSignInView(_GoogleView):
    """No authentication at all: this is a sign-in, and a stale bearer
    token must not turn it into a 401 first."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "google_sign_in"

    @extend_schema(
        summary="Sign in or sign up with Google",
        description=(
            "GOOGLE_SIGN_IN_V1 (off: 404). The same call from Login and Register. "
            "The Google ID token is checked against Google's public keys: `iss` "
            "Google, `aud` our web or iOS client id, not expired. Then the account: "
            "the one already linked to this Google account; else the one with the "
            "same email when Google says it is verified (linked now, and told by "
            "email); else a new one with no password (`is_new_user: true`). An "
            "account with 2-step verification on answers the challenge "
            "/auth/login answers, and no tokens."
        ),
        request=GoogleSignInSerializer,
        responses={
            200: inline_serializer(
                "GoogleSignInTokens",
                {
                    "access": serializers.CharField(),
                    "refresh": serializers.CharField(),
                    "is_new_user": serializers.BooleanField(),
                },
            ),
            401: OpenApiResponse(
                description="`invalid_token`: bad, expired, or not for our client id."
            ),
            409: OpenApiResponse(
                description="`email_in_use`: Google did not verify the email and an account "
                "uses it. `google_already_linked`: that account has a different Google account."
            ),
            422: OpenApiResponse(description="`id_token` `required`; or the account is disabled."),
            503: OpenApiResponse(
                description="`service_unavailable`: Google's keys cannot be read now."
            ),
        },
    )
    def post(self, request):
        s = GoogleSignInSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        google_user = google.verify(s.validated_data["id_token"])
        outcome = google.sign_in(google_user, request)
        account = outcome.account

        # As /auth/login: 2-step on means the challenge, never the tokens.
        # A new account starts with it off.
        if account.two_factor_enabled and two_factor.enabled():
            answer = two_factor.sign_in(account, request)
            if "access" in answer:  # a device that has passed 2-step
                answer = {**answer, "is_new_user": False}
            return Response(answer, status=status.HTTP_200_OK)

        # A new account's first session is no news for the sign-in alert,
        # as at register.
        tokens = services.tokens_for(account, request, sign_in=not outcome.is_new_user)
        return Response({**tokens, "is_new_user": outcome.is_new_user}, status=status.HTTP_200_OK)


class PasswordSetView(_GoogleView):
    permission_classes = [IsAuthenticated]
    throttle_classes = []

    @extend_schema(
        summary="Set a password (an account made with Google)",
        description=(
            "GOOGLE_SIGN_IN_V1 (off: 404). For an account with `has_password: false`. "
            "First a code: POST /me/security/verify/send then /me/security/verify, "
            "purpose `password_set`. Then this, with the verification token. 204; the "
            "account then signs in with email and password too, and is told by email."
        ),
        request=PasswordSetSerializer,
        responses={
            204: None,
            403: OpenApiResponse(
                description="`verification_token` `invalid`: none, wrong, used, expired, "
                "or for another purpose."
            ),
            409: OpenApiResponse(description="`password_already_set`: use Change password."),
            422: OpenApiResponse(
                description="`new_password`: the sign-up rules. `confirm_password`: the two differ."
            ),
        },
    )
    def post(self, request):
        user = request.user
        if user.has_usable_password():
            raise google.PasswordAlreadySet()
        s = PasswordSetSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        sid = sessions.current_id(request)
        with transaction.atomic():
            try:
                two_factor.spend_token(
                    user, sid, TwoFactorPurpose.PASSWORD_SET, data.get("verification_token")
                )
            except two_factor.VerificationRequired:
                raise PermissionDenied(
                    {
                        "verification_token": [
                            ErrorDetail("Please verify it’s you first.", code="invalid")
                        ]
                    }
                )
            user.set_password(data["new_password"])
            user.password_changed_at = timezone.now()
            user.save(update_fields=["password", "password_changed_at", "updated_at"])
            two_factor.notify(user, sid, TwoFactorPurpose.PASSWORD_SET)
        return Response(status=status.HTTP_204_NO_CONTENT)

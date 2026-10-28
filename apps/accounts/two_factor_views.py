"""
Sign-in & Security: 2-step verification and the authenticator app
(docs/TWO_FACTOR_API.md):

    GET    /api/v1/me/security                         the switch and the methods
    POST   /api/v1/me/security/verify/send             send a Verify it's you code
    POST   /api/v1/me/security/verify                  check it: a verification token
    PATCH  /api/v1/me/security/two-factor              turn 2-step on or off
    POST   /api/v1/me/security/authenticator           start adding an authenticator
    POST   /api/v1/me/security/authenticator/confirm   its first code turns it on
    DELETE /api/v1/me/security/authenticator           remove it

and the sign-in challenge that POST /auth/login answers while 2-step is on:

    POST   /api/v1/auth/2fa/send                       send the sign-in code
    POST   /api/v1/auth/2fa/verify                     check it: the token pair

Behind TWO_FACTOR_V1 and DEVICE_SESSIONS_V1: either off, all are 404. No
call looks at X-App-Version: it only names the session.

A change that needs a verification token spends it in the change's own
transaction, so a change that fails gives the token back. The work is in
apps/accounts/two_factor.py.
"""

import re

from django.db import transaction
from django.http import Http404
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import sessions, two_factor
from .models import TwoFactorPurpose
from .session_views import UAE

_SIX_DIGITS = re.compile(r"[0-9]{6}")

# Verify it's you's purposes. sign_in is the sign-in challenge's own.
_PURPOSES = [p for p in TwoFactorPurpose.values if p != TwoFactorPurpose.SIGN_IN]


def _uae(value):
    """The sessions API's times: UAE time, whole seconds."""
    return value.astimezone(UAE).replace(microsecond=0).isoformat() if value else None


# --- bodies -------------------------------------------------------------------------
# method and purpose are checked in validate_*, not by ChoiceField: an enum
# named Purpose or Method in Swagger would rename the published PurposeEnum
# of /auth/otp/request.


def _one_of(value, allowed):
    if value not in allowed:
        raise serializers.ValidationError(
            f'"{value}" is not a valid choice.', code="invalid_choice"
        )
    return value


class _CodeSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="The six digits.")

    def validate_code(self, value):
        # Not six digits is a wrong code, and costs no try: it is never checked.
        if not _SIX_DIGITS.fullmatch(value):
            _, code, message = two_factor.WRONG_CODE
            raise serializers.ValidationError(message, code=code)
        return value


class VerifySendSerializer(serializers.Serializer):
    method = serializers.CharField(help_text="email or phone")
    purpose = serializers.CharField(
        help_text="two_step_on, two_step_off, authenticator_add or authenticator_remove"
    )

    def validate_method(self, value):
        return _one_of(value, ("email", "phone"))

    def validate_purpose(self, value):
        return _one_of(value, _PURPOSES)


class VerifySerializer(_CodeSerializer, VerifySendSerializer):
    method = serializers.CharField(help_text="email, phone or authenticator")

    def validate_method(self, value):
        return _one_of(value, ("email", "phone", "authenticator"))


class _TokenSerializer(serializers.Serializer):
    # Optional here: none at all (left out, empty or null) is the brief's 403
    # verification_required, not a 422.
    verification_token = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="From POST /me/security/verify.",
    )


class AuthenticatorConfirmSerializer(_CodeSerializer):
    pass


class _ChallengeSerializer(serializers.Serializer):
    challenge_token = serializers.CharField(help_text="From POST /auth/login.")


class SignInSendSerializer(_ChallengeSerializer):
    method = serializers.CharField(help_text="email or phone")

    def validate_method(self, value):
        return _one_of(value, ("email", "phone"))


class SignInVerifySerializer(_CodeSerializer, _ChallengeSerializer):
    method = serializers.CharField(help_text="email, phone or authenticator")

    def validate_method(self, value):
        return _one_of(value, ("email", "phone", "authenticator"))


class TwoFactorSwitchSerializer(_TokenSerializer):
    enabled = serializers.BooleanField()


class AuthenticatorChangeSerializer(_TokenSerializer):
    purpose = serializers.CharField(
        required=False,
        help_text=(
            "Optional, as the app sends it. The route decides which purpose "
            "the token must be for."
        ),
    )


# --- answers, for Swagger ------------------------------------------------------------


def _contact(name):
    return inline_serializer(
        name,
        {
            "available": serializers.BooleanField(),
            "hint": serializers.CharField(allow_null=True, help_text="Masked, shown as is."),
        },
    )


_SECURITY = inline_serializer(
    "Security",
    {
        "two_factor_enabled": serializers.BooleanField(),
        "methods": inline_serializer(
            "SecurityMethods",
            {
                "email": _contact("SecurityEmail"),
                "phone": _contact("SecurityPhone"),
                "authenticator": inline_serializer(
                    "SecurityAuthenticator",
                    {
                        "available": serializers.BooleanField(),
                        "added_at": serializers.CharField(
                            allow_null=True, help_text="2026-10-27T14:05:12+04:00"
                        ),
                    },
                ),
            },
        ),
    },
)
_SENT = inline_serializer(
    "SecurityCodeSent",
    {
        "sent_to": serializers.CharField(),
        "expires_in": serializers.IntegerField(),
        "resend_in": serializers.IntegerField(),
    },
)

_ERRORS = {
    401: OpenApiResponse(description="`not_authenticated` or `session_revoked`."),
    404: OpenApiResponse(description="2-step verification is switched off on the server."),
}
_CODE_ERRORS = {
    422: OpenApiResponse(
        description="`code` `invalid` or `expired`, or `method` `unavailable`."
    ),
    429: OpenApiResponse(description="`too_many_attempts`: 5 wrong codes in 15 minutes."),
}
_TOKEN_ERRORS = {
    403: OpenApiResponse(
        description="`verification_required`: no verification token, or a used, "
        "expired or wrong-purpose one."
    ),
}


# --- routes --------------------------------------------------------------------------


class _SecurityView(APIView):
    throttle_classes = []  # the limits are in two_factor

    def initial(self, request, *args, **kwargs):
        if not two_factor.enabled():
            raise Http404("Not found")
        super().initial(request, *args, **kwargs)

    def body(self, serializer_class):
        s = serializer_class(data=self.request.data)
        s.is_valid(raise_exception=True)
        return s.validated_data

    @property
    def sid(self):
        return sessions.current_id(self.request)


class SecurityView(_SecurityView):
    @extend_schema(
        description=(
            "The 2-step switch, and the ways this account can verify: its "
            "verified email and phone (masked), and a confirmed authenticator "
            "app."
        ),
        responses={200: _SECURITY, **_ERRORS},
    )
    def get(self, request):
        user = request.user
        hints = two_factor.contact_hints(user)
        authenticator = two_factor.active_authenticator(user)
        return Response(
            {
                "two_factor_enabled": user.two_factor_enabled,
                "methods": {
                    method: {"available": method in hints, "hint": hints.get(method)}
                    for method in ("email", "phone")
                }
                | {
                    "authenticator": {
                        "available": authenticator is not None,
                        "added_at": _uae(authenticator and authenticator.confirmed_at),
                    }
                },
            }
        )


class VerifySendView(_SecurityView):
    @extend_schema(
        request=VerifySendSerializer,
        description=(
            "Sends a six-digit code, valid 10 minutes, to this account's own "
            "verified email, or its phone by WhatsApp. A new send voids the "
            "last. Again within 30 seconds is 429."
        ),
        responses={
            200: _SENT,
            **_ERRORS,
            422: OpenApiResponse(description="`method` `unavailable`."),
            429: OpenApiResponse(description="`too_many_attempts`: sent too soon or too often."),
        },
    )
    def post(self, request):
        data = self.body(VerifySendSerializer)
        answer = two_factor.send_code(
            request.user, data["method"], data["purpose"], sessions.client_ip(request)
        )
        return Response(answer)


class VerifyView(_SecurityView):
    @extend_schema(
        request=VerifySerializer,
        description=(
            "Checks a code from 3.1, or the authenticator app's current one. "
            "Answers a verification token for `purpose`: this session only, "
            "5 minutes, used once."
        ),
        responses={
            200: inline_serializer(
                "VerificationToken",
                {
                    "verification_token": serializers.CharField(),
                    "expires_in": serializers.IntegerField(),
                },
            ),
            **_ERRORS,
            **_CODE_ERRORS,
        },
    )
    def post(self, request):
        data = self.body(VerifySerializer)
        # No transaction around it: a wrong code's count must stay counted.
        answer = two_factor.verify(
            request.user, self.sid, data["method"], data["purpose"], data["code"]
        )
        return Response(answer)


class TwoFactorSwitchView(_SecurityView):
    @extend_schema(
        request=TwoFactorSwitchSerializer,
        description=(
            "Turns 2-step verification on (a token for `two_step_on`) or off "
            "(`two_step_off`). The value it already has is a 200 too, and the "
            "token is still used. Turning it off leaves the authenticator "
            "linked. The account's email hears of every change."
        ),
        responses={
            200: inline_serializer(
                "TwoFactorSwitch", {"two_factor_enabled": serializers.BooleanField()}
            ),
            **_ERRORS,
            **_TOKEN_ERRORS,
        },
    )
    def patch(self, request):
        data = self.body(TwoFactorSwitchSerializer)
        enabled = data["enabled"]
        purpose = TwoFactorPurpose.TWO_STEP_ON if enabled else TwoFactorPurpose.TWO_STEP_OFF
        with transaction.atomic():
            two_factor.spend_token(request.user, self.sid, purpose, data.get("verification_token"))
            if two_factor.set_enabled(request.user, self.sid, enabled):
                two_factor.notify(request.user, self.sid, purpose)
        return Response({"two_factor_enabled": enabled})


class AuthenticatorView(_SecurityView):
    @extend_schema(
        operation_id="v1_me_security_authenticator_start",
        request=AuthenticatorChangeSerializer,
        description=(
            "Starts adding an authenticator app, with a token for "
            "`authenticator_add`. Pending until its first code (confirm), for "
            "10 minutes, and only for this session. Again replaces a pending "
            "one. The secret is never answered again."
        ),
        responses={
            201: inline_serializer(
                "AuthenticatorSetup",
                {
                    "secret": serializers.CharField(),
                    "otpauth_url": serializers.CharField(),
                    "expires_in": serializers.IntegerField(),
                },
            ),
            **_ERRORS,
            **_TOKEN_ERRORS,
            409: OpenApiResponse(description="`already_added`: remove the current one first."),
            503: OpenApiResponse(description="`authenticator_unavailable`: the server's key."),
        },
    )
    def post(self, request):
        data = self.body(AuthenticatorChangeSerializer)
        with transaction.atomic():
            two_factor.spend_token(
                request.user,
                self.sid,
                TwoFactorPurpose.AUTHENTICATOR_ADD,
                data.get("verification_token"),
            )
            answer = two_factor.start_setup(request.user, self.sid)
        return Response(answer, status=status.HTTP_201_CREATED)

    @extend_schema(
        operation_id="v1_me_security_authenticator_remove",
        request=AuthenticatorChangeSerializer,
        description=(
            "Removes the authenticator app, with a token for "
            "`authenticator_remove` from any method, the app itself included. "
            "2-step verification stays as it is. 204 also when there is none."
        ),
        responses={204: None, **_ERRORS, **_TOKEN_ERRORS},
    )
    def delete(self, request):
        data = self.body(AuthenticatorChangeSerializer)
        with transaction.atomic():
            two_factor.spend_token(
                request.user,
                self.sid,
                TwoFactorPurpose.AUTHENTICATOR_REMOVE,
                data.get("verification_token"),
            )
            if two_factor.remove(request.user):
                two_factor.notify(request.user, self.sid, TwoFactorPurpose.AUTHENTICATOR_REMOVE)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AuthenticatorConfirmView(_SecurityView):
    @extend_schema(
        request=AuthenticatorConfirmSerializer,
        description=(
            "The app's first code turns this session's pending authenticator "
            "on. A wrong code leaves it pending."
        ),
        responses={
            200: inline_serializer(
                "AuthenticatorAdded",
                {"added_at": serializers.CharField(help_text="2026-10-27T14:05:12+04:00")},
            ),
            **_ERRORS,
            **_CODE_ERRORS,
            409: OpenApiResponse(description="`no_setup`: start setting it up again."),
            503: OpenApiResponse(description="`authenticator_unavailable`: the server's key."),
        },
    )
    def post(self, request):
        data = self.body(AuthenticatorConfirmSerializer)
        added_at = two_factor.confirm_setup(request.user, self.sid, data["code"])
        two_factor.notify(request.user, self.sid, TwoFactorPurpose.AUTHENTICATOR_ADD)
        return Response({"added_at": _uae(added_at)})


# --- the sign-in challenge ------------------------------------------------------------


class _SignInView(APIView):
    """Anonymous: the challenge token is the proof. No authentication at
    all, so a stale bearer token cannot turn the call into a 401 first."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []  # the limits are in two_factor

    def initial(self, request, *args, **kwargs):
        if not two_factor.enabled():
            raise Http404("Not found")
        super().initial(request, *args, **kwargs)

    def body(self, serializer_class):
        s = serializer_class(data=self.request.data)
        s.is_valid(raise_exception=True)
        return s.validated_data


_CHALLENGE_EXPIRED = {
    401: OpenApiResponse(
        description="`challenge_expired`: the sign-in's 10 minutes are up, or its "
        "5 wrong codes, or it is used. Sign in again."
    ),
    404: _ERRORS[404],
}


class SignInSendView(_SignInView):
    @extend_schema(
        request=SignInSendSerializer,
        description=(
            "Sends the sign-in code for a challenge from POST /auth/login, as "
            "POST /me/security/verify/send does: the account's own email, or "
            "its phone by WhatsApp. For an authenticator there is nothing to "
            "send."
        ),
        responses={
            200: _SENT,
            **_CHALLENGE_EXPIRED,
            422: OpenApiResponse(description="`method` `unavailable`."),
            429: OpenApiResponse(description="`too_many_attempts`: sent too soon or too often."),
        },
    )
    def post(self, request):
        data = self.body(SignInSendSerializer)
        answer = two_factor.send_sign_in_code(
            data["challenge_token"], data["method"], sessions.client_ip(request)
        )
        return Response(answer)


class SignInVerifyView(_SignInView):
    @extend_schema(
        request=SignInVerifySerializer,
        description=(
            "Checks the sign-in code, from the same X-Device-Id as the login. "
            "Answers the token pair login answers; the session starts here, "
            "and this device is not asked again while it stays signed in."
        ),
        responses={
            200: inline_serializer(
                "SignInTokens",
                {"access": serializers.CharField(), "refresh": serializers.CharField()},
            ),
            **_CHALLENGE_EXPIRED,
            **_CODE_ERRORS,
        },
    )
    def post(self, request):
        data = self.body(SignInVerifySerializer)
        # No transaction around it: a wrong code's count must stay counted.
        tokens = two_factor.finish_sign_in(
            data["challenge_token"], data["method"], data["code"], request
        )
        return Response(tokens)

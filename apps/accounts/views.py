from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.generics import RetrieveUpdateAPIView
# from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny, IsAuthenticated

from . import ratelimit, services
from .serializers import (
    DetailSerializer,
    LoginSerializer,
    LogoutSerializer,
    OtpRequestSerializer,
    OtpRequestedSerializer,
    OtpVerifiedSerializer,
    OtpVerifySerializer,
    PasswordChangeSerializer,
    PasswordForgotSerializer,
    PasswordResetSerializer,
    PasswordResetTokenSerializer,
    PasswordVerifySerializer,
    ProfileSerializer,
    RegisterResponseSerializer,
    RegisterSerializer,
    TokenPairSerializer,
)
# Constant OTP-request response. It carries nothing derived from the request or
# from account existence, so the body is byte-identical for every identifier.
_OTP_REQUESTED = {
    "detail": "If the details are valid, a verification code has been sent.",
    "retry_after": ratelimit.COOLDOWN_SECONDS,
}


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class OtpRequestView(APIView):
    """Send a verification code for the authenticated user's own contact.
    Also serves the /auth/otp/resend alias.
    """

    permission_classes = [AllowAny]
    throttle_classes = []  # rate-limited in the service via Redis

    @extend_schema(request=OtpRequestSerializer, responses={200: OtpRequestedSerializer})
    def post(self, request):
        s = OtpRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        services.request_otp_for_user(
            user=request.user,
            destination_type=data["destination_type"],
            purpose=data["purpose"],
            ip=_client_ip(request),
        )
        return Response(_OTP_REQUESTED, status=status.HTTP_200_OK)


class OtpVerifyView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = []  # rate-limited in the service via Redis

    @extend_schema(request=OtpVerifySerializer, responses={200: OtpVerifiedSerializer})
    def post(self, request):
        s = OtpVerifySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        services.verify_otp_for_user(
            user=request.user,
            destination_type=data["destination_type"],
            purpose=data["purpose"],
            code=data["code"],
            ip=_client_ip(request),
        )
        return Response(
            {"detail": "Account verified successfully.", "account_exists": True},
            status=status.HTTP_200_OK,
        )

class RegisterView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = []

    @extend_schema(request=RegisterSerializer, responses={201: RegisterResponseSerializer})
    def post(self, request):
        s = RegisterSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        result = services.register(
            destination=data["destination"],
            destination_type=data["destination_type"],
            full_name=data["full_name"],
            password=data["password"],
        )
        return Response(result, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    @extend_schema(request=LoginSerializer, responses={200: TokenPairSerializer})
    def post(self, request):
        s = LoginSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data
        destination_type, destination = data["destination_type"], data["destination"]

        account = services.find_account(destination, destination_type)
        if account is None or not account.check_password(data["password"]):
            raise ValidationError({"detail": "Invalid credentials."})
        if not account.is_active:
            raise ValidationError({"detail": "Account disabled."})
        if not services.contact_verified(account, destination_type):
            raise ValidationError({"detail": "Please verify your account before logging in."})

        return Response(services.tokens_for(account), status=status.HTTP_200_OK)


class MeView(RetrieveUpdateAPIView):
    """GET / PATCH the authenticated member's profile (name for greetings)."""

    serializer_class = ProfileSerializer

    def get_object(self):
        return self.request.user


class LogoutView(APIView):
    @extend_schema(request=LogoutSerializer, responses={205: None})
    def post(self, request):
        s = LogoutSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            RefreshToken(s.validated_data["refresh"]).blacklist()
        except TokenError:
            raise ValidationError({"detail": "Invalid or expired token."})
        return Response(status=status.HTTP_205_RESET_CONTENT)


class PasswordChangeView(APIView):
    """
    POST /api/v1/auth/password/change

    For a caller who knows their current password. The old password is the
    proof, so no OTP is involved.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = []

    @extend_schema(request=PasswordChangeSerializer, responses={200: None})
    def post(self, request):
        s = PasswordChangeSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        services.change_password(
            user=request.user,
            old_password=s.validated_data["old_password"],
            new_password=s.validated_data["new_password"],
        )
        return Response({"detail": "Password changed."}, status=status.HTTP_200_OK)


class PasswordForgotView(APIView):
    """
    POST /api/v1/auth/password/forgot

    Step 1 of 3. AllowAny by necessity: someone who has forgotten their
    password cannot hold a token.

    This is the one endpoint that accepts a client-supplied destination and
    sends a message to it, which is the shape closed off in 227d437. It is
    acceptable here only because enforce_request_otp applies a cooldown plus
    per-destination hourly and daily caps plus a per-IP cap, and because the
    response is identical whether the account exists or not.
    """

    permission_classes = [AllowAny]
    throttle_classes = []  # rate-limited in the service via Redis

    @extend_schema(request=PasswordForgotSerializer, responses={200: None})
    def post(self, request):
        s = PasswordForgotSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        services.request_password_reset(
            destination=s.validated_data["destination"],
            destination_type=s.validated_data["destination_type"],
            ip=_client_ip(request),
        )
        # Same body whether or not an account exists. Do not "improve" this.
        return Response(
            {"detail": "If that account exists, a code has been sent."},
            status=status.HTTP_200_OK,
        )


class PasswordVerifyView(APIView):
    """
    POST /api/v1/auth/password/verify

    Step 2 of 3. Trades a correct code for a single-use reset token.
    """

    permission_classes = [AllowAny]
    throttle_classes = []

    @extend_schema(
        request=PasswordVerifySerializer,
        responses={200: PasswordResetTokenSerializer},
    )
    def post(self, request):
        s = PasswordVerifySerializer(data=request.data)
        s.is_valid(raise_exception=True)

        result = services.verify_password_reset(
            destination=s.validated_data["destination"],
            destination_type=s.validated_data["destination_type"],
            code=s.validated_data["code"],
            ip=_client_ip(request),
        )
        return Response(result, status=status.HTTP_200_OK)


class PasswordResetView(APIView):
    """
    POST /api/v1/auth/password/reset

    Step 3 of 3. The token carries the identity; no destination is accepted
    from the client, so a valid token cannot be pointed at another account.
    """

    permission_classes = [AllowAny]
    throttle_classes = []

    @extend_schema(request=PasswordResetSerializer, responses={200: None})
    def post(self, request):
        s = PasswordResetSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        services.reset_password(
            reset_token=s.validated_data["reset_token"],
            new_password=s.validated_data["new_password"],
        )
        return Response(
            {"detail": "Password reset. Please sign in."},
            status=status.HTTP_200_OK,
        )
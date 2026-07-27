from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from drf_spectacular.utils import extend_schema

from . import ratelimit, services
from .serializers import (
    LoginSerializer,
    LogoutSerializer,
    OtpRequestedSerializer,
    OtpRequestSerializer,
    OtpVerifiedSerializer,
    OtpVerifySerializer,
    ProfileSerializer,
    RegisterSerializer,
    TokenPairSerializer,
    RegisterResponseSerializer
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
    """Send a verification code. Also serves the /auth/otp/resend alias."""

    permission_classes = [AllowAny]
    throttle_classes = []  # rate-limited in the service via Redis

    @extend_schema(request=OtpRequestSerializer, responses={200: OtpRequestedSerializer})
    def post(self, request):
        s = OtpRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        services.request_otp(
            destination=data["destination"],
            destination_type=data["destination_type"],
            purpose=data["purpose"],
            ip=_client_ip(request),
        )
        return Response(_OTP_REQUESTED, status=status.HTTP_200_OK)


class OtpVerifyView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = []  # rate-limited in the service via Redis

    @extend_schema(request=OtpVerifySerializer, responses={200: OtpVerifiedSerializer})
    def post(self, request):
        s = OtpVerifySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        account_exists = services.verify_otp(
            destination=data["destination"],
            destination_type=data["destination_type"],
            purpose=data["purpose"],
            code=data["code"],
            ip=_client_ip(request),
        )
        return Response(
            {"verified": True, "account_exists": account_exists},
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

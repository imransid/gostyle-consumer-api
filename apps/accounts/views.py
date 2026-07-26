from django.utils import timezone
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

from . import services
from .identifiers import EMAIL, channel_for_kind, mask_destination
from .models import ConsumerAccount, OtpCode
from .serializers import (
    DetailSerializer,
    ForgotPasswordSerializer,
    LoginSerializer,
    LogoutSerializer,
    OtpSentSerializer,
    ProfileSerializer,
    RegisterSerializer,
    ResendSerializer,
    ResetPasswordSerializer,
    TokenPairSerializer,
    VerifySerializer,
)
from .throttling import OtpDestinationThrottle


def _tokens_for(user):
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def _otp_sent_meta(kind, destination, extra=None):
    """Common payload for OTP-send responses so the client can render the
    masked destination and the resend countdown (the '0:47' timer)."""
    data = {
        "detail": "Verification code sent.",
        "destination": mask_destination(kind, destination),
        "retry_after": services.RESEND_COOLDOWN_SECONDS,
        "expires_in": services.OTP_TTL_SECONDS,
    }
    if extra:
        data.update(extra)
    return data


def _mark_verified(user, kind):
    field = "email_verified_at" if kind == EMAIL else "phone_verified_at"
    if getattr(user, field) is None:
        setattr(user, field, timezone.now())
        user.save(update_fields=[field])


class RegisterView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [OtpDestinationThrottle]

    @extend_schema(request=RegisterSerializer, responses={201: OtpSentSerializer})
    def post(self, request):
        s = RegisterSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data
        kind, destination = data["kind"], data["destination"]

        account = services.find_account(kind, destination)
        if account and services.contact_verified(account, kind):
            raise ValidationError(
                {"detail": "An account with this contact already exists. Please log in."}
            )

        # New sign-up, or resumed from an abandoned (unverified) one.
        account = account or ConsumerAccount()
        setattr(account, "email" if kind == EMAIL else "phone", destination)
        account.full_name = data["full_name"]
        account.accepted_terms_at = timezone.now()
        account.set_password(data["password"])
        account.is_active = True
        account.save()

        services.request_otp(destination, channel=channel_for_kind(kind))

        return Response(
            _otp_sent_meta(kind, destination, {"channel": channel_for_kind(kind)}),
            status=status.HTTP_201_CREATED,
        )


class OtpResendView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [OtpDestinationThrottle]

    @extend_schema(request=ResendSerializer, responses={200: OtpSentSerializer})
    def post(self, request):
        s = ResendSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        kind, destination = s.validated_data["kind"], s.validated_data["destination"]

        account = services.find_account(kind, destination)
        if account is None or services.contact_verified(account, kind):
            raise ValidationError({"detail": "Nothing to verify for this contact."})

        services.request_otp(destination, channel=channel_for_kind(kind))
        return Response(_otp_sent_meta(kind, destination))


class OtpVerifyView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp_verify"

    @extend_schema(request=VerifySerializer, responses={200: TokenPairSerializer})
    def post(self, request):
        s = VerifySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        kind, destination = s.validated_data["kind"], s.validated_data["destination"]

        services.verify_otp(destination, s.validated_data["code"])

        account = services.find_account(kind, destination)
        if account is None:
            raise ValidationError({"detail": "No pending registration. Please register first."})
        if not account.is_active:
            raise ValidationError({"detail": "Account disabled."})

        _mark_verified(account, kind)
        return Response(_tokens_for(account), status=status.HTTP_200_OK)


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    @extend_schema(request=LoginSerializer, responses={200: TokenPairSerializer})
    def post(self, request):
        s = LoginSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        kind, destination = s.validated_data["kind"], s.validated_data["destination"]

        invalid = ValidationError({"detail": "Invalid credentials."})
        account = services.find_account(kind, destination)
        if account is None or not account.check_password(s.validated_data["password"]):
            raise invalid
        if not account.is_active:
            raise ValidationError({"detail": "Account disabled."})
        if not services.contact_verified(account, kind):
            raise ValidationError({"detail": "Please verify your account before logging in."})

        return Response(_tokens_for(account), status=status.HTTP_200_OK)


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [OtpDestinationThrottle]

    @extend_schema(request=ForgotPasswordSerializer, responses={200: OtpSentSerializer})
    def post(self, request):
        s = ForgotPasswordSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        kind, destination = s.validated_data["kind"], s.validated_data["destination"]

        account = services.find_account(kind, destination)
        if account is not None:
            try:
                services.request_otp(
                    destination,
                    channel=channel_for_kind(kind),
                    purpose=OtpCode.Purpose.RESET,
                )
            except ValidationError:
                # Swallow the resend-cooldown error so the response stays
                # identical whether or not the account exists (no enumeration).
                pass

        # Always the same response, regardless of account existence.
        return Response(
            {
                "detail": "If an account exists for this contact, a reset code has been sent.",
                "destination": mask_destination(kind, destination),
                "retry_after": services.RESEND_COOLDOWN_SECONDS,
            }
        )


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp_verify"

    @extend_schema(request=ResetPasswordSerializer, responses={200: DetailSerializer})
    def post(self, request):
        s = ResetPasswordSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        kind, destination = s.validated_data["kind"], s.validated_data["destination"]

        services.verify_otp(destination, s.validated_data["code"], purpose=OtpCode.Purpose.RESET)

        account = services.find_account(kind, destination)
        if account is None:
            raise ValidationError({"detail": "No account for this contact."})

        services.reset_password(account, s.validated_data["new_password"], kind)
        return Response({"detail": "Password updated. Please log in."}, status=status.HTTP_200_OK)


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

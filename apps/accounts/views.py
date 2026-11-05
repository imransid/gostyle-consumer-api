import logging
import urllib.parse

from django.conf import settings
from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.generics import RetrieveUpdateAPIView
# from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.permissions import AllowAny, IsAuthenticated
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework.exceptions import ErrorDetail, NotFound, PermissionDenied, ValidationError
from rest_framework.exceptions import PermissionDenied
from .models import DeletionReason, Purpose
from .serializers import (
    ContactChangeConfirmSerializer,
    ContactChangeProofSerializer,
    ContactChangeRequestSerializer,
)


from .models import Favourite

from . import google, ratelimit, services, sessions, two_factor
from .notifications import (
    send_deletion_cancelled_notice,
    send_deletion_scheduled_notice,
    verified_email,
)
from .permissions import IsVerified
from .serializers import (
    DeleteAccountSerializer,
    DeleteAccountWithGoogleSerializer,
    DeletionScheduledSerializer,
    DeletionSummarySerializer,
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
    UserLookupResponseSerializer,
    UserLookupResultSerializer,
    UserLookupSerializer,
)
logger = logging.getLogger(__name__)

# Constant OTP-request response. It carries nothing derived from the request or
# from account existence, so the body is byte-identical for every identifier.
_OTP_REQUESTED = {
    "detail": "If the details are valid, a verification code has been sent.",
    "retry_after": ratelimit.COOLDOWN_SECONDS,
}

_CHANGE_CONTACT_SENT = {
    "success": True,
    "expires_in": services.OTP_TTL_SECONDS,
    "resend_after": ratelimit.COOLDOWN_SECONDS,
}


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class OtpRequestView(APIView):
    """Send a verification code for the authenticated user's own contact.
    Also serves the /auth/otp/resend alias.

    purpose=change_contact forks twice: with no change_token it is step 1 of a
    contact change and may only reach a contact the account holds; with one it
    is step 3 and may reach the new contact.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = []  # rate-limited in the service via Redis

    @extend_schema(request=OtpRequestSerializer, responses={200: OtpRequestedSerializer})
    def post(self, request):
        if request.data.get("purpose") == Purpose.CHANGE_CONTACT:
            return self._change_contact(request)

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

    def _change_contact(self, request):
        if request.data.get("change_token"):
            s = ContactChangeRequestSerializer(data=request.data)
            s.is_valid(raise_exception=True)
            data = s.validated_data

            services.request_contact_change_code(
                user=request.user,
                change_token=data["change_token"],
                destination=data["destination"],
                destination_type=data["destination_type"],
                ip=_client_ip(request),
            )
            return Response(_CHANGE_CONTACT_SENT, status=status.HTTP_200_OK)

        s = OtpRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        # Step 1 may only reach a contact already on the account. The service
        # reads the destination off the user anyway, so this check exists to
        # answer 403 rather than silently sending somewhere else.
        own = getattr(request.user, services._account_field(data["destination_type"]))
        if not own or own != data["destination"]:
            raise PermissionDenied(
                {
                    "destination": [
                        ErrorDetail(
                            "That contact is not on this account.",
                            code="destination_not_own",
                        )
                    ]
                }
            )

        services.request_otp_for_user(
            user=request.user,
            destination_type=data["destination_type"],
            purpose=Purpose.CHANGE_CONTACT,
            ip=_client_ip(request),
        )
        return Response(_CHANGE_CONTACT_SENT, status=status.HTTP_200_OK)

class OtpReSendView(APIView):

    permission_classes = [AllowAny]
    throttle_classes = [] 

    @extend_schema(request=OtpRequestSerializer, responses={200: OtpRequestedSerializer})
    def post(self, request):
        s = OtpRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        services.resend_otp_for_user(
            destination=data["destination"],
            destination_type=data["destination_type"],
            purpose=data["purpose"],
            ip=_client_ip(request),
        )
        return Response(_OTP_REQUESTED, status=status.HTTP_200_OK)

class OtpVerifyView(APIView):
    """Check a code against the caller's own contact.

    purpose=change_contact forks the same way the request view does: with no
    change_token it is step 2 and answers a token; with one it is step 4 and
    commits the new contact.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = []  # rate-limited in the service via Redis

    @extend_schema(request=OtpVerifySerializer, responses={200: OtpVerifiedSerializer})
    def post(self, request):
        if request.data.get("purpose") == Purpose.CHANGE_CONTACT:
            return self._change_contact(request)

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

    def _change_contact(self, request):
        if request.data.get("change_token"):
            s = ContactChangeConfirmSerializer(data=request.data)
            s.is_valid(raise_exception=True)
            data = s.validated_data

            user = services.confirm_contact_change(
                user=request.user,
                change_token=data["change_token"],
                destination=data["destination"],
                destination_type=data["destination_type"],
                code=data["code"],
                ip=_client_ip(request),
            )
            return Response(
                {"success": True, "user": ProfileSerializer(user).data},
                status=status.HTTP_200_OK,
            )

        s = ContactChangeProofSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        result = services.verify_contact_change_proof(
            user=request.user,
            destination_type=data["destination_type"],
            code=data["code"],
            ip=_client_ip(request),
        )
        return Response({"success": True, **result}, status=status.HTTP_200_OK)


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
            gender=data["gender"],
            request=request,
        )
        return Response(result, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    @extend_schema(
        request=LoginSerializer,
        description=(
            "With 2-step verification on for the account (and on the server), "
            "a device that has not passed it gets a challenge instead of the "
            "tokens: `{two_factor_required, challenge_token, expires_in, "
            "methods}`. The code then goes to /auth/2fa/send and /auth/2fa/verify "
            "(docs/TWO_FACTOR_API.md section 6). With Google sign-in on, an "
            "account made with Google that has no password yet answers 401 "
            "`password_not_set` (docs/GOOGLE_SIGN_IN_API.md section 4)."
        ),
        responses={
            200: TokenPairSerializer,
            401: OpenApiResponse(
                description="`password_not_set`: this account signs in with Google."
            ),
        },
    )
    def post(self, request):
        s = LoginSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data
        destination_type, destination = data["destination_type"], data["destination"]

        account = services.find_account(destination, destination_type)
        if (
            account is not None
            and google.enabled()
            and not account.has_usable_password()
            and google.linked(account) is not None
        ):
            # Made with Google and no password set yet: say so, so the app
            # can offer Continue with Google (docs/GOOGLE_SIGN_IN_API.md 4).
            raise google.PasswordNotSet()
        if account is None or not account.check_password(data["password"]):
            raise ValidationError({"detail": "Invalid credentials."})
        if not account.is_active:
            raise ValidationError({"detail": "Account disabled."})
        if not services.contact_verified(account, destination_type):
            raise ValidationError({"detail": "Please verify your account before logging in."})

        # Only once the password is right, so a stranger learns nothing of it.
        if account.two_factor_enabled and two_factor.enabled():
            return Response(two_factor.sign_in(account, request), status=status.HTTP_200_OK)

        return Response(services.tokens_for(account, request), status=status.HTTP_200_OK)


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
            refused = False
        except TokenError:
            refused = True
        # This device's session ends even when its refresh token was refused:
        # the device is leaving either way. After the blacklist above, which
        # would refuse a token that ending the session had blacklisted first.
        if sessions.enabled():
            sessions.end_current(request)
        if refused:
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


class UserLookupView(APIView):
    """
    GET /api/v1/user/lookup?contact=<email or E.164 phone>

    Find a member by phone or email: 200 with {found: true, user: {id, name,
    image}}, or 200 with {found: false, user: null}.

    A GET, so the contact is in the URL. nginx and gunicorn both drop the
    query from their access logs for this path (nginx/log-format-json.conf,
    config/gunicorn_logging.py).

    IsVerified, not IsAuthenticated. The daily cap is per account, and a cap
    per account is worth nothing while a fresh, unverified account is free.

    An unverified account and no account at all answer with the same 404,
    byte for byte. If they differed, this would be a free check of who has
    signed up. The service folds both into None; keep it that way.
    """

    permission_classes = [IsVerified]
    throttle_classes = []  # rate-limited in the service via Redis

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "contact",
                str,
                required=True,
                description=(
                    "One email, or one phone in E.164. URL-encoded: a `+` is "
                    "`%2B`. Exact match only. 422 `invalid_contact` when it "
                    "is neither."
                ),
            ),
        ],
        responses={
            200: UserLookupResponseSerializer,
            429: OpenApiResponse(
                description=(
                    f"More than {ratelimit.LOOKUP_PER_ACCOUNT_DAILY} lookups "
                    "by this account today. Hits and misses both count."
                ),
            ),
        },
    )
    def get(self, request):
        s = UserLookupSerializer(data=request.query_params)
        s.is_valid(raise_exception=True)

        match = services.lookup_account(
            caller=request.user,
            destination=s.validated_data["destination"],
            destination_type=s.validated_data["destination_type"],
        )
        if match is None:
            return Response({"found": False, "user": None}, status=status.HTTP_200_OK)
        return Response(
            {"found": True, "user": UserLookupResultSerializer(match).data},
            status=status.HTTP_200_OK,
        )


class DeleteAccountView(APIView):
    """
    POST /api/v1/auth/me/delete

    Schedules the account for purging ConsumerAccount.PURGE_AFTER_DAYS from
    now. Nothing is removed yet: the member can still sign in and cancel
    until then. The purge itself is the purge_due_accounts command.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = []

    @extend_schema(
        request=DeleteAccountWithGoogleSerializer,
        description=(
            "The proof is the password. With Google sign-in on, an account "
            "linked to Google may send `id_token` instead: a Google ID token "
            "taken in the last 10 minutes, of the linked Google account "
            "(docs/GOOGLE_SIGN_IN_API.md section 6). Exactly one of the two."
        ),
        responses={
            200: DeletionScheduledSerializer,
            401: OpenApiResponse(
                description="`id_token` `invalid_token`: bad, expired, or older than 10 minutes."
            ),
            403: OpenApiResponse(
                description="`password` `wrong_password`, or `id_token` `wrong_google_account`."
            ),
            422: OpenApiResponse(description="`password` `required`: neither sent, or both."),
        },
    )
    def post(self, request):
        with_google = google.enabled()
        serializer_class = (
            DeleteAccountWithGoogleSerializer if with_google else DeleteAccountSerializer
        )
        s = serializer_class(data=request.data)
        s.is_valid(raise_exception=True)

        if with_google and s.validated_data.get("id_token"):
            # Confirm with Google: a fresh token of the linked Google account.
            google_user = google.verify_fresh(s.validated_data["id_token"])
            link = google.linked(request.user)
            if link is None or link.subject != google_user.subject:
                google.wrong_google_account()
        elif not request.user.check_password(s.validated_data["password"]):
            raise PermissionDenied(
                {
                    "password": [
                        ErrorDetail("Wrong password.", code="wrong_password")
                    ]
                }
            )

        # request_deletion sets purge_at on this same instance before it
        # saves, so request.user already holds the value that was written.
        request.user.request_deletion(
            s.validated_data.get("reason", DeletionReason.OTHER),
            s.validated_data.get("note", ""),
        )
        account = request.user
        transaction.on_commit(lambda: send_deletion_scheduled_notice(account))
        return Response(
            DeletionScheduledSerializer(
                {
                    "purge_at": account.purge_at,
                    # The address send_deletion_scheduled_notice uses, by the
                    # same rule, so the screen never names one that got nothing.
                    "confirmation_sent_to": verified_email(account),
                }
            ).data,
            status=status.HTTP_200_OK,
        )


class DeletionSummaryView(APIView):
    """
    GET /api/v1/auth/me/delete/summary

    What the "Delete your account?" screen lists: only what this system
    really has. Upcoming bookings come from booking-api's own Upcoming
    number, asked with the caller's token; booking-api down is null, never
    an error, so the screen still opens.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="What a deletion removes (the Delete Account screen)",
        description=(
            "The numbers for the first Delete Account screen. `purge_after_days` "
            "is the countdown (30). `purge_at` is set when a deletion is already "
            "scheduled. `upcoming_bookings` is the Upcoming tab's number, null "
            "when booking-api could not be asked. `favourites` is saved salons "
            "plus stylists. Nothing else is sent: there is no wallet, saved card, "
            "discount card or gallery in the system yet, so the app hides those rows."
        ),
        responses={200: DeletionSummarySerializer},
    )
    def get(self, request):
        account = request.user
        return Response(
            DeletionSummarySerializer(
                {
                    "purge_after_days": account.PURGE_AFTER_DAYS,
                    "purge_at": account.purge_at,
                    "upcoming_bookings": _upcoming_bookings(request),
                    "favourites": account.favourites.count()
                    + account.favourite_stylists.count(),
                }
            ).data
        )


def _upcoming_bookings(request):
    """booking-api's counts.upcoming for the caller, or None when it could
    not be asked or answered something else."""
    # Imported here: apps.salons imports this app.
    from apps.salons.booking_api import BookingApiUnavailable, list_bookings

    query = urllib.parse.urlencode({"filter": "upcoming", "page": 1, "pageSize": 1})
    try:
        code, body = list_bookings(
            query,
            authorization=request.META.get("HTTP_AUTHORIZATION", ""),
            timeout=settings.SINGLE_LOOKUP_TIMEOUT,
        )
    except BookingApiUnavailable:
        code, body = None, None
    counts = body.get("counts") if code == 200 and isinstance(body, dict) else None
    upcoming = counts.get("upcoming") if isinstance(counts, dict) else None
    if isinstance(upcoming, bool) or not isinstance(upcoming, int):
        logger.warning(
            "deletion summary: no Upcoming number from booking-api (%s)", code
        )
        return None
    return upcoming


class CancelDeletionView(APIView):
    """
    POST /api/v1/auth/me/delete/cancel

    Clears a scheduled deletion. Safe to call when none is scheduled.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = []

    @extend_schema(request=None, responses={204: None})
    def post(self, request):
        # Told only when there was a deletion to cancel: this is safe to
        # call when none is scheduled, and then nothing changed.
        was_scheduled = request.user.purge_at is not None
        request.user.cancel_deletion()
        if was_scheduled:
            account = request.user
            transaction.on_commit(lambda: send_deletion_cancelled_notice(account))
        return Response(status=status.HTTP_204_NO_CONTENT)

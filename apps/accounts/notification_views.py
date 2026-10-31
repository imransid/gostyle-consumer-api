from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import NotificationPreference
from .notification_serializers import NotificationPreferenceSerializer

_ERROR_ENVELOPE = {
    "type": "object",
    "properties": {
        "detail": {"type": "string"},
        "code": {"type": "string"},
        "errors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "nullable": True},
                    "message": {"type": "string"},
                },
            },
        },
    },
}


def _error_response(errors):
    """Serializer errors in the contract's 400 envelope."""
    items, unknown = [], False
    for field, messages in errors.items():
        for message in messages:
            unknown = unknown or getattr(message, "code", None) == "unknown_field"
            items.append(
                {
                    "field": None if field == "non_field_errors" else field,
                    # Added with the channel switches, so the app can tell
                    # email_not_verified from sms_unavailable without
                    # reading the sentence.
                    "code": getattr(message, "code", None) or "invalid",
                    "message": str(message),
                }
            )
    return Response(
        {
            "detail": (
                "Unknown notification preference."
                if unknown
                else "Invalid notification preference."
            ),
            "code": "validation_error",
            "errors": items,
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


@extend_schema_view(
    get=extend_schema(
        summary="Get notification preferences",
        description=(
            "All the caller's preferences. The customer comes from the bearer "
            "token. First use creates the record with the defaults. `email`, "
            "`sms` and the read-only `channels_available` only while "
            "NOTIFICATION_CHANNELS_V1 is on; `reminder_lead_minutes` (30, 60, "
            "120 or 1440) only while REMINDER_TIMING_V1 is on."
        ),
        responses={200: NotificationPreferenceSerializer},
    ),
    patch=extend_schema(
        summary="Update notification preferences",
        description=(
            "Partial update: only the fields sent change, and the complete "
            "preferences come back. Switches must be real booleans, "
            "`reminder_lead_minutes` a number. Unknown keys are a 400. "
            "Turning `push` off leaves the others alone. Turning `email` on "
            "needs a verified email (`email_not_verified`); `sms` on needs a "
            "verified phone, and answers `sms_unavailable` until there is an "
            "SMS provider."
        ),
        request=NotificationPreferenceSerializer,
        examples=[
            OpenApiExample("Turn off push", value={"push": False}, request_only=True),
            OpenApiExample(
                "Turn on special offers", value={"special_offers": True}, request_only=True
            ),
        ],
        responses={
            200: NotificationPreferenceSerializer,
            400: OpenApiResponse(
                response=_ERROR_ENVELOPE,
                description="Not a boolean, or not a known preference.",
                examples=[
                    OpenApiExample(
                        "Not a boolean",
                        value={
                            "detail": "Invalid notification preference.",
                            "code": "validation_error",
                            "errors": [
                                {"field": "special_offers", "message": "Must be a boolean."}
                            ],
                        },
                    ),
                    OpenApiExample(
                        "Unknown field",
                        value={
                            "detail": "Unknown notification preference.",
                            "code": "validation_error",
                            "errors": [
                                {
                                    "field": "email",
                                    "message": "This notification preference is not supported.",
                                }
                            ],
                        },
                    ),
                ],
            ),
        },
    ),
)
class NotificationPreferencesView(APIView):
    """GET / PATCH /api/v1/notifications/preferences"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        prefs = NotificationPreference.for_account(request.user)
        return Response(NotificationPreferenceSerializer(prefs).data)

    def patch(self, request):
        prefs = NotificationPreference.for_account(request.user)
        serializer = NotificationPreferenceSerializer(
            prefs, data=request.data, partial=True
        )
        if not serializer.is_valid():
            return _error_response(serializer.errors)
        serializer.save()
        return Response(serializer.data)
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
            "All seven preferences of the caller. The customer comes from the "
            "bearer token. First use creates the record with the defaults."
        ),
        responses={200: NotificationPreferenceSerializer},
    ),
    patch=extend_schema(
        summary="Update notification preferences",
        description=(
            "Partial update: only the fields sent change, and the complete "
            "preferences come back. Values must be real booleans. Unknown "
            "keys are a 400. Turning `push` off leaves the other six alone."
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
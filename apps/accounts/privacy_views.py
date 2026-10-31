"""
GET / PATCH /api/v1/me/privacy (docs/PRIVACY_SETTINGS_API.md), behind
PRIVACY_SETTINGS_V1. Off, both are the 404 this path was before.

"Location access" on the same screen is the phone's own permission: nothing
of it is kept here.
"""

import logging

from django.conf import settings
from django.db import DatabaseError, transaction
from django.http import Http404
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.salons.customer_sync import refresh_salon_name

from .notification_serializers import StrictBooleanField

logger = logging.getLogger(__name__)

PRIVACY_FIELDS = ("show_name_to_salons", "show_photo_in_moments")


class PrivacySerializer(serializers.Serializer):
    show_name_to_salons = StrictBooleanField(required=False)
    show_photo_in_moments = StrictBooleanField(required=False)

    def to_internal_value(self, data):
        # A typo is refused, never ignored, as on /notifications/preferences
        # (but 422, this API's code for a bad field).
        if isinstance(data, dict):
            unknown = [key for key in data if key not in PRIVACY_FIELDS]
            if unknown:
                raise serializers.ValidationError(
                    {
                        key: [
                            serializers.ErrorDetail(
                                "This privacy setting is not supported.", code="unknown_field"
                            )
                        ]
                        for key in unknown
                    }
                )
        return super().to_internal_value(data)


class SalonRecordUnavailable(APIException):
    status_code = 503
    default_detail = "Your privacy settings could not be saved right now. Please try again."
    default_code = "service_unavailable"


def _answer(account):
    return {name: getattr(account, name) for name in PRIVACY_FIELDS}


class PrivacyView(APIView):
    """GET / PATCH /api/v1/me/privacy"""

    @extend_schema(
        summary="My privacy settings",
        description=(
            "Behind PRIVACY_SETTINGS_V1 (off: 404). `show_name_to_salons` "
            "(default true): off, salons see the first name and the last "
            "name's initial (\"Sara A.\"). `show_photo_in_moments` (default "
            "false): kept for Moments, which does not exist yet; it changes "
            "nothing today."
        ),
        responses={200: PrivacySerializer},
    )
    def get(self, request):
        if not settings.PRIVACY_SETTINGS_V1:
            raise Http404("Not found")
        return Response(_answer(request.user))

    @extend_schema(
        summary="Change my privacy settings",
        description=(
            "Behind PRIVACY_SETTINGS_V1 (off: 404). Partial: only what is sent "
            "changes, all of it or none. Values must be real booleans; unknown "
            "keys are a 422. Turning `show_name_to_salons` off also shortens "
            "the name on the salon's record of you that this app made, at "
            "once; a salon's own record of you (from a visit before the app) "
            "is the salon's, and stays as it is."
        ),
        request=PrivacySerializer,
        examples=[
            OpenApiExample(
                "Hide my full name", value={"show_name_to_salons": False}, request_only=True
            )
        ],
        responses={200: PrivacySerializer},
    )
    def patch(self, request):
        if not settings.PRIVACY_SETTINGS_V1:
            raise Http404("Not found")
        body = PrivacySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        account = request.user
        changed = {
            name: value
            for name, value in body.validated_data.items()
            if getattr(account, name) != value
        }
        if changed:
            for name, value in changed.items():
                setattr(account, name, value)
            try:
                # Both or neither: the setting and the salon's copy of the
                # name, so the screen never says "hidden" while it is not.
                with transaction.atomic():
                    account.save(update_fields=[*changed, "updated_at"])
                    if "show_name_to_salons" in changed:
                        refresh_salon_name(account)
            except DatabaseError as e:
                account.refresh_from_db(fields=list(changed))
                logger.error("privacy settings not saved for %s: %s", account.pk, e)
                raise SalonRecordUnavailable() from e
            for name, value in changed.items():
                logger.info("privacy setting changed: account=%s %s=%s", account.pk, name, value)
        return Response(_answer(account))

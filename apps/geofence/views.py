"""POST /api/v1/me/location and GET /api/v1/me/geofences (docs/SERVICE_AREA_ALERTS_FE.md)."""

import logging
from datetime import datetime

from django.conf import settings
from django.http import Http404
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework.exceptions import ErrorDetail, ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.salons.views import _OUR_ENVELOPE, BookingApiDown

from . import service, sources, zones

logger = logging.getLogger(__name__)

# The phone's own uncertainty is never this large for a fix worth measuring.
MAX_ACCURACY_M = 10_000

MESSAGES = {
    "required": "This field is required.",
    "invalid_latitude": "Send a latitude between -90 and 90.",
    "invalid_longitude": "Send a longitude between -180 and 180.",
    "invalid_accuracy": f"Send accuracy_m in metres, from 0 to {MAX_ACCURACY_M}.",
    "invalid_recorded_at": "Send recorded_at as an ISO 8601 time with its offset, not in the future.",
}

READING_EXAMPLE = {
    "latitude": 25.2048,
    "longitude": 55.2708,
    "accuracy_m": 12,
    "recorded_at": "2026-11-12T11:41:07Z",
}

ANSWER_EXAMPLE = {
    "ignored": None,
    "visits": [
        {
            "booking_id": "0b8f6c1e-3a52-4d7e-9c41-5f2a7e9d8b10",
            "salon_id": "7c2d9a4e-1b3f-4e6a-8d5c-9f0e1a2b3c4d",
            "distance_m": 180,
            "skipped": None,
            "zones": [
                {"zone": "arrival", "radius_m": 50, "inside": False, "notified": False},
                {"zone": "auto_trigger", "radius_m": 200, "inside": True, "notified": True},
                {"zone": "primary", "radius_m": 800, "inside": True, "notified": True},
            ],
            "sent": ["visit.almost_there"],
        }
    ],
}

REGIONS_EXAMPLE = {
    "regions": [
        {
            "id": "0b8f6c1e-3a52-4d7e-9c41-5f2a7e9d8b10:arrival",
            "booking_id": "0b8f6c1e-3a52-4d7e-9c41-5f2a7e9d8b10",
            "salon_id": "7c2d9a4e-1b3f-4e6a-8d5c-9f0e1a2b3c4d",
            "zone": "arrival",
            "latitude": 25.2048,
            "longitude": 55.2708,
            "radius_m": 50,
            "starts_at": "2026-11-12T11:45:00Z",
            "ends_at": "2026-11-12T12:45:00Z",
        }
    ]
}


def _switch():
    if not settings.SERVICE_AREA_ALERTS_V1:
        raise Http404("Not found")


def _invalid(field, code):
    return {field: [ErrorDetail(MESSAGES[code], code=code)]}


def _coordinate(data, field, low, high, code, errors):
    if field not in data or data[field] is None or data[field] == "":
        errors.update({field: [ErrorDetail(MESSAGES["required"], code="required")]})
        return None
    value = zones.number(data[field])
    if value is None or not low <= value <= high:
        errors.update(_invalid(field, code))
        return None
    return value


def parse_reading(data, now):
    """The reading in a POST /me/location body, or a 422 naming each bad field."""
    if not isinstance(data, dict):
        raise ValidationError({"non_field_errors": [ErrorDetail("Send a JSON object.", code="invalid")]})
    errors = {}
    latitude = _coordinate(data, "latitude", -90, 90, "invalid_latitude", errors)
    longitude = _coordinate(data, "longitude", -180, 180, "invalid_longitude", errors)

    accuracy_m = None
    if data.get("accuracy_m") not in (None, ""):
        accuracy_m = zones.number(data["accuracy_m"])
        if accuracy_m is None or not 0 <= accuracy_m <= MAX_ACCURACY_M:
            errors.update(_invalid("accuracy_m", "invalid_accuracy"))

    recorded_at = now
    if data.get("recorded_at") not in (None, ""):
        try:
            recorded_at = datetime.fromisoformat(str(data["recorded_at"]))
        except ValueError:
            recorded_at = None
        if recorded_at is None or recorded_at.tzinfo is None or recorded_at > now + service.MAX_CLOCK_AHEAD:
            errors.update(_invalid("recorded_at", "invalid_recorded_at"))

    if errors:
        raise ValidationError(errors)
    return service.Reading(latitude, longitude, accuracy_m, recorded_at)


@extend_schema(
    summary="Service area alerts: send a location reading",
    description=(
        "The phone's position, measured against the rings the salon set in "
        "Build -> Service Area (arrival, auto-trigger, primary) for each of "
        "the customer's confirmed visits that starts within 3 hours and has "
        "not ended. The first reading inside a ring sends that ring's alert "
        "(push type visit.arrived, visit.almost_there or visit.nearby) once "
        "per salon per day; a reading inside an inner ring passes the outer "
        "ones in silence. The reading is not stored. A reading older than 5 "
        "minutes is ignored (`ignored`: stale). Behind SERVICE_AREA_ALERTS_V1 "
        "(off: 404). See docs/SERVICE_AREA_ALERTS_FE.md."
    ),
    request={
        "application/json": {
            "type": "object",
            "properties": {
                "latitude": {"type": "number"},
                "longitude": {"type": "number"},
                "accuracy_m": {"type": "number", "nullable": True},
                "recorded_at": {"type": "string", "format": "date-time", "nullable": True},
            },
            "required": ["latitude", "longitude"],
        }
    },
    examples=[OpenApiExample("A reading", request_only=True, value=READING_EXAMPLE)],
    responses={
        200: OpenApiResponse(
            response={"type": "object"},
            description="Measured. `visits` is empty when there is no visit to watch.",
            examples=[OpenApiExample("Inside the auto-trigger ring", value=ANSWER_EXAMPLE)],
        ),
        401: OpenApiResponse(response=_OUR_ENVELOPE, description="Not signed in, or the token has expired."),
        422: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description=(
                "`required`, `invalid_latitude`, `invalid_longitude`, `invalid_accuracy`, "
                "`invalid_recorded_at`, each on its field."
            ),
        ),
        429: OpenApiResponse(response=_OUR_ENVELOPE, description="More than LOCATION_PING_RATE (20 a minute)."),
        503: OpenApiResponse(response=_OUR_ENVELOPE, description="The visits could not be read. Try the next reading."),
    },
)
class LocationView(APIView):
    """POST /api/v1/me/location"""

    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "location"

    def post(self, request):
        _switch()
        now = timezone.now()
        reading = parse_reading(request.data, now)
        if service.is_stale(reading, now):
            logger.info(
                "service area: account %s sent a reading from %s, older than %s: ignored",
                request.user.pk,
                reading.recorded_at.isoformat(),
                service.MAX_READING_AGE,
            )
            return Response({"ignored": "stale", "visits": []})
        try:
            visits = service.evaluate(request.user, reading, now)
        except sources.Unavailable as exc:
            logger.error("service area: account %s: reading not measured: %s", request.user.pk, exc)
            raise BookingApiDown() from exc
        return Response({"ignored": None, "visits": visits})


@extend_schema(
    summary="Service area alerts: the rings to watch",
    description=(
        "One region per ring per salon for the customer's confirmed visits in "
        "the next 24 hours, for the phone's own geofencing (iOS region "
        "monitoring, Android GeofencingClient). On entering one, the app "
        "sends POST /me/location; the server decides whether to alert. Salons "
        "with no Service Area, or no map pin, are left out. Behind "
        "SERVICE_AREA_ALERTS_V1 (off: 404)."
    ),
    responses={
        200: OpenApiResponse(
            response={"type": "object"},
            description="Innermost ring first within each visit.",
            examples=[OpenApiExample("One visit, its arrival ring", value=REGIONS_EXAMPLE)],
        ),
        401: OpenApiResponse(response=_OUR_ENVELOPE, description="Not signed in, or the token has expired."),
        503: OpenApiResponse(response=_OUR_ENVELOPE, description="The visits could not be read."),
    },
)
class GeofencesView(APIView):
    """GET /api/v1/me/geofences"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        _switch()
        try:
            regions = service.regions(request.user)
        except sources.Unavailable as exc:
            logger.error("service area: account %s: regions not read: %s", request.user.pk, exc)
            raise BookingApiDown() from exc
        return Response({"regions": regions})

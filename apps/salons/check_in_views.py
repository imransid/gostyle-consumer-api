"""
Self check-in: a customer says "I am here" for their own booking, and reads
the desk's answer (SELF_CHECK_IN_V1):

    POST /api/v1/booking/<id>/check-in   raise a request
    GET  /api/v1/booking/<id>/check-in   the latest one, or null

booking-api owns all of it (gostyle-booking-api, docs/SELF_CHECK_IN_HANDOVER.md):
who may raise one, when check-in opens, one request at a time, no second try
after the desk said no, and the desk's approve or reject. This module checks
the flag, forwards the caller's own token, and passes booking-api's answer
back as it came, as the single cancel and reschedule do.

WHICH ID. The id the app already holds for the visit: a SINGLE row's id, or
a ROUTINE session's. A party (a GROUP row) is not one booking, and v1 has no
party-wide request: its id is 404 at booking-api, as any id that is not the
caller's own booking.

NO LOOKUP FIRST. Unlike the cancel, nothing here depends on the booking's
type: booking-api answers for the booking itself, so asking the Upcoming
shelf first would be a round trip that decides nothing.

NO BODY AND NO X-Tenant-Id are forwarded. booking-api's route takes no body,
and it takes the tenant from the booking, so a header the app sends can
never be the tenant a request is filed under.
"""
from django.conf import settings
from django.http import Http404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .booking_api import BookingApiUnavailable, raise_check_in, read_check_in
from .views import _OUR_ENVELOPE, BookingApiDown

_REQUEST = {
    "type": "object",
    "properties": {
        "requestId": {"type": "string"},
        "bookingId": {"type": "string"},
        "state": {"type": "string", "enum": ["WAITING", "APPROVED", "REJECTED", "EXPIRED", "CLOSED"]},
        "raisedAt": {"type": "string"},
        "decidedAt": {"type": "string", "nullable": True},
    },
}
_ANSWER = {"type": "object", "properties": {"request": {**_REQUEST, "nullable": True}}}


class SelfCheckInView(APIView):
    """POST and GET /api/v1/booking/<id>/check-in"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="I am here: ask the desk to check me in",
        description=(
            "Behind SELF_CHECK_IN_V1 (off: 404, booking-api is not called). "
            "No body. Raises a check-in request on the caller's own booking; "
            "the desk approves or rejects it. Opens 30 minutes before the "
            "start and closes at the end time. While it waits, the booking is "
            "never marked a no-show automatically. A second tap answers 200 "
            "with the request already waiting.\n\n"
            "booking-api's answer comes back as it came: 201 or 200 with "
            "`{request}`, or its refusal with a `code`: "
            "`BOOKING_CHECKIN_WINDOW` (409, too early: `details.windowOpensAt`; "
            "or closed: `details.windowClosed`), `BOOKING_STATE_INVALID` (409, "
            "the booking is not confirmed: `details.status`), "
            "`BOOKING_CHECKIN_REJECTED` (409, the desk said no: send the "
            "customer to the desk), `BOOKING_NOT_FOUND` (404)."
        ),
        request=None,
        responses={
            201: OpenApiResponse(response=_ANSWER, description="Raised: WAITING."),
            200: OpenApiResponse(response=_ANSWER, description="One was already waiting: the same request."),
            404: OpenApiResponse(description="Not the caller's booking, a party's id, or the flag is off."),
            409: OpenApiResponse(description="booking-api: too early, closed, not confirmed, or rejected before."),
            503: OpenApiResponse(response=_OUR_ENVELOPE, description="booking-api unreachable."),
        },
    )
    def post(self, request, booking_id):
        if not settings.SELF_CHECK_IN_V1:
            raise Http404("Not found")
        try:
            code, answer = raise_check_in(
                booking_id, authorization=request.META.get("HTTP_AUTHORIZATION", ""),
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        return Response(answer, status=code)

    @extend_schema(
        summary="Has the desk answered my check-in?",
        description=(
            "Behind SELF_CHECK_IN_V1 (off: 404). The latest request on the "
            "caller's own booking: WAITING, APPROVED (checked in), REJECTED "
            "(speak to the desk), EXPIRED (nobody answered before the end "
            "time) or CLOSED (the booking moved on another way). "
            "`{\"request\": null}` when none was raised. The desk's reason "
            "is never shown here."
        ),
        responses={
            200: OpenApiResponse(response=_ANSWER, description="`{request}`, or `{request: null}`."),
            404: OpenApiResponse(description="Not the caller's booking, a party's id, or the flag is off."),
            503: OpenApiResponse(response=_OUR_ENVELOPE, description="booking-api unreachable."),
        },
    )
    def get(self, request, booking_id):
        if not settings.SELF_CHECK_IN_V1:
            raise Http404("Not found")
        try:
            code, answer = read_check_in(
                booking_id, authorization=request.META.get("HTTP_AUTHORIZATION", ""),
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        return Response(answer, status=code)

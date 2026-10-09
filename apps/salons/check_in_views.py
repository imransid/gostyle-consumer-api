"""
Self check-in: a customer says "I am here" for their own booking, and reads
the desk's answer (SELF_CHECK_IN_V1):

    POST /api/v1/booking/<id>/check-in            raise a request
    POST /api/v1/booking/<id>/check-in/withdraw   take it back (Cancel Request)
    GET  /api/v1/booking/<id>/check-in            the latest one, or null

WITHDRAW, NOT CANCEL, in the path: POST /api/v1/booking/<id>/cancel cancels
the whole visit, and a route one segment away from it must not share its
word. The app's button still says Cancel Request.

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

AT A CHAIR (gostyle-booking-api, docs/chair-check-in.md). The app scans the
QR card on the chair and sends `chair_token`, exactly as scanned. It goes on
to booking-api as `chairToken`, with the app's own User-Agent as
`userAgent`. booking-api asks platform which chair it is and decides; this
module never looks at the chair. A blank or non-string `chair_token` is a
broken scan, not a chair to look up: refused here (the project's field-level
422), so platform never records a scan of a token that could not resolve.

NO CHAIR is Wait for Staff: no `chair_token`, and the call to booking-api is
byte for byte the one it was before chairs (no body). The rest of the app's
body is never forwarded, and neither is X-Tenant-Id: booking-api takes the
tenant from the booking, so a header the app sends can never be the tenant
a request is filed under.

TWO 503s, AND THEY MEAN DIFFERENT THINGS. booking-api's own
(`DEPENDENCY_UNAVAILABLE`, `details.fallback: WAIT_FOR_STAFF`) means the
chair could not be checked: scanning is off, the desk still works. It comes
back unchanged. Ours (`booking_api_unavailable`) means booking-api itself
could not be reached, and Wait for Staff would fail the same way. Never
turn one into the other. booking-api also says `DEPENDENCY_UNAVAILABLE`
with NO fallback when it cannot check the customer's own token, and Wait
for Staff fails there too: `details.fallback` is the switch, not the code.
"""
from collections.abc import Mapping

from django.conf import settings
from django.http import Http404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.exceptions import ErrorDetail, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .booking_api import (
    BookingApiUnavailable,
    raise_check_in,
    read_check_in,
    withdraw_check_in,
)
from .views import _OUR_ENVELOPE, BookingApiDown

_REQUEST = {
    "type": "object",
    "properties": {
        "requestId": {"type": "string"},
        "bookingId": {"type": "string"},
        "state": {
            "type": "string",
            "enum": ["WAITING", "APPROVED", "REJECTED", "EXPIRED", "CLOSED", "WITHDRAWN"],
        },
        "raisedAt": {"type": "string"},
        "decidedAt": {"type": "string", "nullable": True},
        "chair": {
            "type": "object",
            "nullable": True,
            "description": "The chair scanned with this request, as it was then. Null: no chair.",
            "properties": {
                "number": {"type": "string"},
                "zoneName": {"type": "string", "nullable": True},
            },
        },
    },
}
_ANSWER = {"type": "object", "properties": {"request": {**_REQUEST, "nullable": True}}}
_RAISE = {
    "type": "object",
    "properties": {
        "chair_token": {
            "type": "string",
            "description": (
                "AT A CHAIR: the text of the chair's QR card, exactly as "
                "scanned. Leave it out for Wait for Staff."
            ),
            "example": "q7Xk2mP9rT4vW8yZ1aB3cD",
        },
    },
}


def _chair_token(request):
    """
    The scanned token, or None for no chair (Wait for Staff). Absent and
    null are both no chair. Anything else must be a string with something in
    it, and goes on exactly as scanned, never trimmed: the token is
    platform's, and a changed one is a different card.
    """
    data = request.data
    token = data.get("chair_token") if isinstance(data, Mapping) else None
    if token is None:
        return None
    if not isinstance(token, str):
        raise ValidationError({"chair_token": [
            ErrorDetail("The chair scan must be text.", code="invalid"),
        ]})
    if not token.strip():
        raise ValidationError({"chair_token": [
            ErrorDetail("The chair scan was empty. Please scan the card again.", code="blank"),
        ]})
    return token


class SelfCheckInView(APIView):
    """POST and GET /api/v1/booking/<id>/check-in"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="I am here: ask the desk to check me in",
        description=(
            "Behind SELF_CHECK_IN_V1 (off: 404, booking-api is not called). "
            "Raises a check-in request on the caller's own booking; the desk "
            "approves or rejects it. Opens 30 minutes before the start and "
            "closes at the end time. While it waits, the booking is never "
            "marked a no-show automatically. A second tap answers 200 with "
            "the request already waiting.\n\n"
            "AT A CHAIR: send `chair_token`, the chair card's text exactly as "
            "scanned, and the request carries the chair (`request.chair`). "
            "WAIT FOR STAFF: no body (or no `chair_token`); the request "
            "carries no chair.\n\n"
            "booking-api's answer comes back as it came: 201 or 200 with "
            "`{request}`, or its refusal with a `code`: "
            "`BOOKING_CHECKIN_WINDOW` (409, too early: `details.windowOpensAt`; "
            "or closed: `details.windowClosed`), `BOOKING_STATE_INVALID` (409, "
            "the booking is not confirmed: `details.status`), "
            "`BOOKING_CHECKIN_REJECTED` (409, the desk said no: send the "
            "customer to the desk), `BOOKING_CHAIR_REFUSED` (409, not with "
            "that chair: show `message`; `details.reason` is CARD_OUT_OF_DATE, "
            "OTHER_SALON, CHAIR_NOT_AVAILABLE or UNKNOWN_CARD), "
            "`BOOKING_NOT_FOUND` (404).\n\n"
            "TWO 503s. `DEPENDENCY_UNAVAILABLE` with `details.fallback: "
            "WAIT_FOR_STAFF` is booking-api's: the chair could not be checked, "
            "the desk still works, so offer Wait for Staff. "
            "`booking_api_unavailable` is ours: booking-api itself is down, "
            "and Wait for Staff will fail too. Offer Wait for Staff only on "
            "`details.fallback` = WAIT_FOR_STAFF, never on the status or the "
            "code alone: booking-api's other `DEPENDENCY_UNAVAILABLE` (no "
            "fallback) means it could not check the customer's token."
        ),
        request={"application/json": _RAISE},
        responses={
            201: OpenApiResponse(response=_ANSWER, description="Raised: WAITING."),
            200: OpenApiResponse(response=_ANSWER, description="One was already waiting: the same request."),
            422: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description=(
                    "`chair_token` blank (`blank`) or not text (`invalid`): a broken scan. "
                    "booking-api is not called."
                ),
            ),
            404: OpenApiResponse(description="Not the caller's booking, a party's id, or the flag is off."),
            409: OpenApiResponse(
                description="booking-api: too early, closed, not confirmed, rejected before, or not that chair.",
            ),
            503: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description=(
                    "Ours (`booking_api_unavailable`): booking-api unreachable, nothing works. "
                    "Or booking-api's (`DEPENDENCY_UNAVAILABLE`, `details.fallback: "
                    "WAIT_FOR_STAFF`): the chair could not be checked, the desk still works."
                ),
            ),
        },
    )
    def post(self, request, booking_id):
        if not settings.SELF_CHECK_IN_V1:
            raise Http404("Not found")
        # With no chair, the call is exactly the one before chairs: no
        # chair_token and no user_agent are passed at all.
        chair = {}
        chair_token = _chair_token(request)
        if chair_token is not None:
            chair = {
                "chair_token": chair_token,
                "user_agent": request.META.get("HTTP_USER_AGENT", ""),
            }
        try:
            code, answer = raise_check_in(
                booking_id, authorization=request.META.get("HTTP_AUTHORIZATION", ""), **chair,
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
            "time), CLOSED (the booking moved on another way) or WITHDRAWN "
            "(the customer took it back). "
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


_STATE_NOW = {
    "type": "object",
    "properties": {
        "statusCode": {"type": "integer", "example": 409},
        "code": {"type": "string", "example": "BOOKING_STATE_INVALID"},
        "message": {"type": "string", "example": "This request has already ended."},
        "details": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "nullable": True,
                    "enum": ["APPROVED", "REJECTED", "EXPIRED", "CLOSED", None],
                    "description": "The request's state as it now is; null: none was ever raised.",
                },
            },
        },
        "error": {"type": "string", "example": "Conflict"},
    },
}


class SelfCheckInWithdrawView(APIView):
    """POST /api/v1/booking/<id>/check-in/withdraw"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Cancel Request: take back my waiting check-in request",
        description=(
            "Behind SELF_CHECK_IN_V1 (off: 404, booking-api is not called). "
            "Withdraws the request waiting for the desk on the caller's own "
            "booking. The booking is untouched, and is still never marked a "
            "no-show automatically. Unlike after a rejection, the customer "
            "may say \"I am here\" again straight away, at another chair or "
            "with Wait for Staff. No body: only the token goes on.\n\n"
            "booking-api's answer comes back as it came: 200 with "
            "`{request}`, WITHDRAWN (a second tap answers the same one); or "
            "409 `BOOKING_STATE_INVALID` when nothing is waiting, whose "
            "`details.request` is the request's state AS IT NOW IS, so the "
            "app moves on: APPROVED, REJECTED, EXPIRED or CLOSED (\"This "
            "request has already ended.\"; EXPIRED or CLOSED may have been "
            "written by this very call, when the desk had already checked "
            "them in or the end time had passed), or null when none was "
            "ever raised (\"There is no check-in request to cancel.\")."
        ),
        request=None,
        responses={
            200: OpenApiResponse(response=_ANSWER, description="Withdrawn, or already withdrawn: the request."),
            409: OpenApiResponse(
                response=_STATE_NOW,
                description="booking-api: nothing waiting; `details.request` says what it is now.",
            ),
            404: OpenApiResponse(description="Not the caller's booking, a party's id, or the flag is off."),
            503: OpenApiResponse(response=_OUR_ENVELOPE, description="booking-api unreachable."),
        },
    )
    def post(self, request, booking_id):
        if not settings.SELF_CHECK_IN_V1:
            raise Http404("Not found")
        try:
            code, answer = withdraw_check_in(
                booking_id, authorization=request.META.get("HTTP_AUTHORIZATION", ""),
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        return Response(answer, status=code)

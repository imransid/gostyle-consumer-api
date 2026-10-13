"""
Group check-in: "we are here" for an app party (GROUP_CHECK_IN_V1).

    POST /api/v1/booking/group/<group_id>/check-in   the booker's one tap
    GET  /api/v1/booking/group/<group_id>/check-in   each lane's latest answer

A PASS-THROUGH, like check_in_views.py. booking-api raises the single
check-in request on each lane of the party and answers lane by lane; this
service forwards the call and that answer as they are (camelCase), with the
token only, never X-Tenant-Id. booking-api decides who may (the booker only:
404 for anyone else) and every rule of every lane.

NO CHAIR for a party in v1: a `chair_token` is refused with 422 before
booking-api is called, never dropped. The customer meant "I am in this
chair", and a party's request carries none. Each member is Wait for Staff.

OFF unless GROUP_CHECK_IN_V1 and SELF_CHECK_IN_V1 are both on: 404, the path
as it was before, and booking-api is not called.
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
    raise_group_check_in,
    read_group_check_in,
)
from .views import _OUR_ENVELOPE, BookingApiDown

NO_CHAIR_FOR_A_PARTY = (
    "A party is checked in without a chair. Tap We are here, and the desk "
    "checks each member in."
)


def _on():
    """Both switches: a party's check-in IS self check-in."""
    return settings.SELF_CHECK_IN_V1 and settings.GROUP_CHECK_IN_V1


def _booking_ids(request):
    """
    The optional `booking_ids` (D2), read before booking-api is called. None
    when not sent: every lane. A `chair_token` with any value but null is
    refused, as is anything that is not a list of strings: 422, never a
    guess.
    """
    body = request.data
    if not isinstance(body, Mapping):
        raise ValidationError(
            {"booking_ids": [ErrorDetail("Send a JSON object.", code="invalid")]}
        )
    if body.get("chair_token") is not None:
        raise ValidationError(
            {"chair_token": [ErrorDetail(NO_CHAIR_FOR_A_PARTY, code="invalid")]}
        )
    ids = body.get("booking_ids")
    if ids is None:
        return None
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        raise ValidationError(
            {"booking_ids": [ErrorDetail("A list of booking ids.", code="invalid")]}
        )
    return ids


class GroupCheckInView(APIView):
    """POST and GET /api/v1/booking/group/<group_id>/check-in"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="We are here: check in my whole party",
        description=(
            "Behind GROUP_CHECK_IN_V1 (and SELF_CHECK_IN_V1). The booker of an app party "
            "only: the desk gets the ordinary check-in request for each member, and "
            "approves each one. `booking_ids` (optional) limits it to those members. "
            "A member refused (too early, say) does not stop the others: its lane "
            "carries the single check-in's own error. Sending it again is safe. "
            "booking-api's answer is passed through as it is (camelCase)."
        ),
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "booking_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional. Only these members' bookings. Left out: everyone.",
                    }
                },
            }
        },
        responses={
            201: OpenApiResponse(description="At least one member raised: { groupId, lanes }."),
            200: OpenApiResponse(description="Nothing new, a member already waiting: { groupId, lanes }."),
            404: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description="No such party, not the booker, or a switch is off.",
            ),
            409: OpenApiResponse(
                description=(
                    "Nothing to do for anyone: every member's shared refusal (as the single "
                    "check-in says it), or BOOKING_GROUP_CHECKIN_NONE with `details.lanes`."
                ),
            ),
            422: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description="A chair_token (a party has none), or booking_ids not a list of ids.",
            ),
            503: OpenApiResponse(response=_OUR_ENVELOPE, description="booking-api unreachable."),
        },
    )
    def post(self, request, group_id):
        if not _on():
            raise Http404("Not found")
        booking_ids = _booking_ids(request)
        try:
            code, answer = raise_group_check_in(
                group_id,
                authorization=request.META.get("HTTP_AUTHORIZATION", ""),
                booking_ids=booking_ids,
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        return Response(answer, status=code)

    @extend_schema(
        summary="My party's check-in, member by member",
        description=(
            "Behind GROUP_CHECK_IN_V1 (and SELF_CHECK_IN_V1). The booker only: each member's "
            "booking with its latest check-in request, or null. Passed through as it is."
        ),
        responses={
            200: OpenApiResponse(description="{ groupId, lanes }."),
            404: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description="No such party, not the booker, or a switch is off.",
            ),
            503: OpenApiResponse(response=_OUR_ENVELOPE, description="booking-api unreachable."),
        },
    )
    def get(self, request, group_id):
        if not _on():
            raise Http404("Not found")
        try:
            code, answer = read_group_check_in(
                group_id, authorization=request.META.get("HTTP_AUTHORIZATION", ""),
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        return Response(answer, status=code)

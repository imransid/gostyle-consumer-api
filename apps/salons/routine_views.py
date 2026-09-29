"""
The app team's routine contract (docs/routine-booking-fe-contract.md, and
our draft of the rest, docs/routine-booking-fe-contract-draft.md):

    POST  /api/v1/booking/routine-preview                 the dates a plan would take
    POST  /api/v1/booking/routine                         book the whole plan
    PATCH /api/v1/booking/<id>/sessions/<session_id>      move one session

Next to the /booking/series routes, which do not change: both sets reach the
same booking-api routes, and this one speaks the contract's words
(routine_contract.py, pure).

Behind ROUTINE_CONTRACT_V1: off, every route here is 404 and booking-api is
never called. Built step by step (docs/ROUTINE_FE_CONTRACT_AUDIT.md, 4.2): the
preview in C3; the create (C4) and the move (C5) answer 501 until theirs,
so a switch turned on too early is not mistaken for a missing route.

THE OLD ROUTE'S CHECKS, NOT NEW ONES. Each route here does what the old
routine create does, in the same order and with the same helpers
(group_views: JSON only, the salon and its route, the services sold here, a
stylist on the roster who does all of them, booking-api's clock), and holds
every session to the salon's own hours with the old route's facts
(series_views._Hours). Only the words and the shapes are the contract's.
"""

import logging
import uuid

from django.conf import settings
from django.http import Http404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import group_translate as gt
from . import group_views as gv
from . import routine_contract as rc
from . import timezones
from .booking_api import BookingApiUnavailable, create_series_booking
from .group_views import (
    _check_stylists,
    _clock,
    _json_only,
    _now,
    _party_timing,
    _refuse,
    _roster,
    _salon_and_route,
    _service_rows,
    _validated,
)
from .routine_serializers import RoutinePreviewRequestSerializer
from .series_views import _Hours
from .views import _OUR_ENVELOPE, BookingApiDown

logger = logging.getLogger(__name__)


class RouteNotBuilt(APIException):
    """C1 only: the flag is on, and the route is built in a later step."""

    status_code = status.HTTP_501_NOT_IMPLEMENTED
    default_detail = "This route is not built yet."
    default_code = "not_built"


def _gate():
    """404 with the flag off, before anything else is looked at."""
    if not settings.ROUTINE_CONTRACT_V1:
        raise Http404("Not found")


_FLAG = (
    "Behind ROUTINE_CONTRACT_V1 (off: 404, booking-api is not called). "
    "Not built yet: with the flag on it answers 501 until its step lands "
    "(docs/ROUTINE_FE_CONTRACT_AUDIT.md, 4.2)."
)
_RESPONSES = {
    404: OpenApiResponse(response=_OUR_ENVELOPE, description="The flag is off."),
    501: OpenApiResponse(response=_OUR_ENVELOPE, description="Not built yet."),
}


def _salon_hours(salon, tz, clock, *, longest, lead):
    """
    hours(start): every reason the salon's own hours refuse a start
    (routine_contract.hours_codes), from the old route's facts: the salon's
    opening hours on THAT day (series_views._Hours, read once a day),
    booking-api's trading day, and now plus the longest notice of the
    services. The old route answers the first reason only; the contract
    needs them all, to pick one in its own order.
    """
    facts = _Hours(salon, tz, clock, longest=longest, lead=lead, now=_now(tz))
    engine = gt.engine_zone(clock)

    def hours(start):
        engine_day = start.astimezone(engine).date()
        return rc.hours_codes(
            start,
            facts._span(start.astimezone(tz).date()),
            gt.engine_span(engine_day, clock),
            facts.earliest,
            facts.longest,
        )

    return hours


def _candidates(salon, service_ids, roster):
    """
    Any Available Expert: who on the roster does all these services, as the
    Expert step measures it. None is no_stylist_available (the translator).
    """
    coverage = gv.stylist_service_coverage(
        salon, [uuid.UUID(s) for s in service_ids], [uuid.UUID(s) for s in roster],
    )
    return rc.stylists_doing_all(coverage, service_ids)


def _refused(exc):
    """A translator refusal as our 422."""
    return _refuse(exc.field, exc.message, exc.code)


class RoutinePreviewView(APIView):
    """POST /api/v1/booking/routine-preview"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Preview a routine (app contract)",
        description=(
            "Behind ROUTINE_CONTRACT_V1 (off: 404, booking-api is not called). "
            "The dates the plan would take from `start_time` (any offset) on its "
            "`cadence`, each `available` or not with one `reason` (salon_closed, "
            "outside_hours, too_soon, stylist_unavailable, in that order), the "
            "`stylist` who would take it, and up to 3 `alternatives` as ISO times "
            "on the salon's clock. No stylist_id: the server picks one for the "
            "whole plan. Nothing is held and nothing is booked; send no "
            "Idempotency-Key. Every time on the salon's own clock."
        ),
        request=RoutinePreviewRequestSerializer,
        responses={
            200: OpenApiResponse(description="The plan (contract section 2)."),
            404: OpenApiResponse(response=_OUR_ENVELOPE, description="No such salon, or the flag is off."),
            415: OpenApiResponse(response=_OUR_ENVELOPE, description="Not application/json."),
            422: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description="invalid_time, invalid_cadence, foreign_id, stylist_mismatch, "
                            "no_stylist_available, no_services; or booking-api's "
                            "(invalid_session_count, date_out_of_range, ...), in our envelope.",
            ),
            503: OpenApiResponse(response=_OUR_ENVELOPE, description="booking-api unreachable."),
        },
    )
    def post(self, request):
        _gate()
        _json_only(request)
        data = _validated(RoutinePreviewRequestSerializer, request)
        salon, route = _salon_and_route(data["salon_id"])
        service_ids = [str(s) for s in data["service_ids"]]
        visit = [{"service_ids": service_ids}]
        rows = _service_rows(salon, visit, "service_ids")
        roster = _roster(salon)
        stylist = str(data["stylist_id"]) if data.get("stylist_id") else None
        if stylist is not None:
            _check_stylists(salon, [{"stylist_id": stylist, "service_ids": service_ids}], roster)
            candidates = []
        else:
            candidates = _candidates(salon, service_ids, roster)

        tz = timezones.resolve(salon.branch_timezone, salon.id)
        authorization = request.META.get("HTTP_AUTHORIZATION", "")
        clock = _clock(authorization, route)
        try:
            body = rc.preview_body(
                {
                    "service_ids": service_ids,
                    "stylist_id": stylist,
                    "start_time": data["start_time"],
                    "cadence": data["cadence"],
                    "sessions": data["sessions"],
                },
                branch_id=route["branch_id"], tz=tz, clock=clock, candidates=candidates,
            )
        except rc.Refusal as exc:
            raise _refused(exc) from exc

        try:
            # A dry run: nothing is held or booked, and never with a key
            # (a key would replay this preview to the next one).
            code, plan = create_series_booking(
                body,
                authorization=authorization,
                idempotency_key=None,
                tenant_id=route["tenant_id"],
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        if code != status.HTTP_200_OK or not isinstance(plan, dict):
            if code == 400:
                logger.warning("booking-api refused a routine preview body: %r", plan)
            answer_status, answer = rc.translate_refusal(code, plan, route="preview")
            return Response(answer, status=answer_status)

        longest, lead = _party_timing(rows, visit) if rows else (0, 0)
        hours = _salon_hours(salon, tz, clock, longest=longest, lead=lead)
        return Response(
            rc.present_preview(
                plan,
                tz=tz,
                engine_tz=gt.engine_zone(clock),
                hours=hours,
                names={sid: card["name"] for sid, card in roster.items()},
            )
        )


class RoutineCreateView(APIView):
    """POST /api/v1/booking/routine (built in C4)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Book a routine (app contract)",
        description="Every session booked in one call, or none. " + _FLAG,
        request=None,
        responses=_RESPONSES,
    )
    def post(self, request):
        _gate()
        raise RouteNotBuilt()


class RoutineSessionMoveView(APIView):
    """PATCH /api/v1/booking/<id>/sessions/<session_id> (built in C5)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Move one session of a routine (app contract)",
        description="One session to a new start; the rest of the routine stays. " + _FLAG,
        request=None,
        responses=_RESPONSES,
    )
    def patch(self, request, booking_id, session_id):
        _gate()
        raise RouteNotBuilt()

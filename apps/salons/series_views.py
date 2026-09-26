"""
Routine (series) bookings for the app.

    POST /api/v1/booking/series        a preview (dry_run) or the routine
    GET  /api/v1/booking/series/<id>   the routine hub

Each route forwards 1 to 1 to booking-api's mobile route
(/v1/mobile-booking/series), which plans, prices and books every visit
(docs/SERIES_BOOKING_AUDIT.md, E.3). This service does what it does for a
group booking: resolve the salon, check what only it can check (the salon,
its services, its stylist, and the salon's own hours and the services'
notice for every session), put every time on booking-api's clock, forward
with the caller's own token, and put the answer back on the salon's clock.
The translation is series_translate.py, which is pure.

THE HOURS NEED booking-api's PLAN. A routine's days are booking-api's to
plan, so a create first asks for the plan (a dry run, which saves nothing),
holds every session and pick to the salon's hours, and only then books. A
dry run is answered from that same plan, with the hours applied.

Behind SERIES_BOOKING_V1: off, both routes are 404 and nothing else changes.
BookingDetailView and GroupBookingCancelView are not touched: a routine has
its own routes, so a single booking and a party answer exactly as before.
"""

import logging
from datetime import datetime, time, timedelta

from django.conf import settings
from django.http import Http404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import group_translate as gt
from . import series_translate as st
from . import timezones
from .booking_api import (
    BookingApiUnavailable,
    create_series_booking,
    read_series_booking,
)
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
from .selectors import salon_cards_for_refs
from .series_serializers import SeriesBookingRequestSerializer
from .views import _OUR_ENVELOPE, BookingApiDown, _idempotency_key, _open_span, _salon_card

logger = logging.getLogger(__name__)

_ENVELOPE_NOTE = (
    "booking-api's refusals come back as it sent them, in the app's envelope "
    "`{detail, code, errors: [{field, code, message, expected?}]}`: 422 for a "
    "field (`invalid_session_count`, `payment_plan_not_available`, "
    "`amount_mismatch` with `expected`, ...), 409 `session_not_free`."
)


# The group rule's codes, in a routine's words (group's messages speak of a
# party).
_HOURS_MESSAGES = {
    "salon_closed": "The salon is closed on {day}.",
    "too_soon": "{day} at {time} has passed, or is too soon to book.",
    "outside_hours": "A visit on {day} at {time} would not start and finish "
                     "within the salon's hours.",
}


class _Hours:
    """
    The salon's hours and the services' notice, held to exactly the rule a
    group start is (group_translate.start_refusal): the salon's own hours on
    THAT day (they differ by weekday, and a day can be shut by hand),
    booking-api's trading day, and now plus the longest notice any chosen
    service asks for. Each day's hours are read once per request.
    """

    def __init__(self, salon, tz, clock, *, longest, lead, now):
        self.salon, self.tz, self.clock = salon, tz, clock
        self.longest = longest
        self.earliest = now + timedelta(minutes=lead)
        self._spans = {}

    def _span(self, day):
        if day not in self._spans:
            day_start = datetime.combine(day, time.min, tzinfo=self.tz)
            self._spans[day] = _open_span(self.salon, self.tz, day, day_start)
        return self._spans[day]

    def refusal(self, start):
        """(code, message) for a start the salon cannot take, or None."""
        local = start.astimezone(self.tz)
        engine_day = start.astimezone(gt.engine_zone(self.clock)).date()
        why = gt.start_refusal(
            start,
            self._span(local.date()),
            gt.engine_span(engine_day, self.clock),
            earliest=self.earliest,
            longest_minutes=self.longest,
        )
        if why is None:
            return None
        code = why[0]
        message = _HOURS_MESSAGES.get(code, why[1]).format(
            day=local.date().isoformat(), time=local.strftime("%H:%M")
        )
        return code, message


def _present_hub(answer, engine_tz):
    """
    The routine as the app reads it: the salon card, and every time on the
    salon's own clock. The card carries the salon's zone, so no second
    lookup, as for a party.
    """
    ref = answer.get("salon_id")
    card = salon_cards_for_refs([ref]).get(ref) if isinstance(ref, str) else None
    tz = timezones.resolve(card.get("timezone"), card["id"]) if card else None
    out = st.present_hub(
        answer, salon_id=card["id"] if card else None, tz=tz, engine_tz=engine_tz
    )
    out["salon"] = _salon_card(card, full=True)
    return out


@extend_schema(
    summary="Preview or book a routine",
    description=(
        "Behind SERIES_BOOKING_V1. `dry_run: true` saves nothing: without a "
        "`time` it answers the days and `available_times` (free on every "
        "day); with one, each session free or not with up to 3 "
        "`alternatives`, the money for the three plans and the rules. "
        "Every session is held to the salon's own hours on its day and the "
        "services' notice: in a dry run a session outside them is not free "
        "(with `refusal`), alternatives and free times outside them are left "
        "out; a create is refused before anything is booked. "
        "Without `dry_run` every visit is booked, all or nothing (201, the "
        "hub). Every time is on the salon's own clock, both ways. Send an "
        "`Idempotency-Key` with the real create only, never with a dry run. "
        + _ENVELOPE_NOTE
    ),
    request=SeriesBookingRequestSerializer,
    responses={
        200: OpenApiResponse(description="dry_run: the preview."),
        201: OpenApiResponse(description="Booked: the routine hub."),
        404: OpenApiResponse(response=_OUR_ENVELOPE, description="No such salon, or the flag is off."),
        409: OpenApiResponse(description="`session_not_free`: nothing was booked."),
        415: OpenApiResponse(response=_OUR_ENVELOPE, description="Not application/json."),
        422: OpenApiResponse(description="This project's envelope for `foreign_id`, "
                                         "`stylist_mismatch`, and a session outside the "
                                         "salon's hours as a group start is refused: "
                                         "`salon_closed`, `too_soon`, `outside_hours` on "
                                         "`sessions[i]` or `picks[j]`; or booking-api's."),
        503: OpenApiResponse(response=_OUR_ENVELOPE, description="booking-api unreachable."),
    },
)
class SeriesBookingCreateView(APIView):
    """POST /api/v1/booking/series"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not settings.SERIES_BOOKING_V1:
            raise Http404("Not found")
        _json_only(request)
        # FIRST, before request.data: the key may be derived from the bytes
        # the app sent, and DRF's parsing consumes the stream.
        idempotency_key = _idempotency_key(request)

        data = _validated(SeriesBookingRequestSerializer, request)
        salon, route = _salon_and_route(data["salon_id"])
        service_ids = [str(s["id"]) for s in data["services"]]
        visit = [{"service_ids": service_ids}]
        rows = _service_rows(salon, visit, "services")
        if data.get("stylist_id"):
            _check_stylists(
                salon,
                [{"stylist_id": str(data["stylist_id"]), "service_ids": service_ids}],
                _roster(salon),
            )

        tz = timezones.resolve(salon.branch_timezone, salon.id)
        authorization = request.META.get("HTTP_AUTHORIZATION", "")
        clock = _clock(authorization, route)
        try:
            body = st.engine_body(
                data, branch_id=route["branch_id"], tz=tz, clock=clock, sent=request.data
            )
        except st.TimeNotConstant as exc:
            raise _refuse("time", str(exc), "invalid_time") from exc

        dry_run = bool(data.get("dry_run"))
        engine_tz = gt.engine_zone(clock)
        longest, lead = _party_timing(rows, visit) if rows else (0, 0)
        hours = _Hours(salon, tz, clock, longest=longest, lead=lead, now=_now(tz))

        # ---- booking-api's own plan first, saving nothing -----------------
        # The days of a routine are booking-api's to plan (DAILY skips ITS
        # closed days), so the only way to hold every session, and every
        # pick, to the salon's hours is to ask for the plan, check it, and
        # only then book. A dry run IS that plan, and is answered from it.
        try:
            code, plan = create_series_booking(
                {**body, "dry_run": True},
                authorization=authorization,
                idempotency_key=None,
                tenant_id=route["tenant_id"],
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        if code != status.HTTP_200_OK or not isinstance(plan, dict):
            if code == 400:
                logger.warning("booking-api refused a routine body: %r", plan)
            return Response(plan, status=code)

        preview = st.present_preview(plan, tz=tz, engine_tz=engine_tz)
        if dry_run:
            return Response(st.within_hours(preview, hours.refusal, tz), status=code)

        outside = st.first_outside_hours(preview, hours.refusal, data.get("picks"))
        if outside is not None:
            field, why, message = outside
            raise _refuse(field, message, why)

        # ---- the routine, every session inside the salon's hours ----------
        try:
            code, answer = create_series_booking(
                body,
                authorization=authorization,
                # The real create only: see create_series_booking.
                idempotency_key=idempotency_key,
                tenant_id=route["tenant_id"],
                # Up to 6 visits booked one by one: the long wait is the
                # create's alone. The plan above keeps the ordinary timeout.
                timeout=settings.SERIES_BOOKING_CREATE_TIMEOUT,
            )
        except BookingApiUnavailable as exc:
            # Safe to send again with the same key once the first has
            # finished: booking-api replays the routine it booked.
            raise BookingApiDown() from exc

        if code not in (status.HTTP_200_OK, status.HTTP_201_CREATED) or not isinstance(answer, dict):
            if code == 400:
                logger.warning("booking-api refused a routine body: %r", answer)
            return Response(answer, status=code)

        return Response(_present_hub(answer, engine_tz), status=code)


@extend_schema(
    summary="The routine hub",
    description=(
        "Behind SERIES_BOOKING_V1. Every session with its state, done / "
        "remaining / skipped, the next session, the money, what the customer "
        "may do now, and the salon card. Every time on the salon's own "
        "clock. The customer who made it; anyone else is 404, never 403."
    ),
    request=None,
    responses={
        200: OpenApiResponse(description="The routine."),
        404: OpenApiResponse(description="No such routine, not yours, or the flag is off."),
        503: OpenApiResponse(response=_OUR_ENVELOPE, description="booking-api unreachable."),
    },
)
class SeriesBookingDetailView(APIView):
    """GET /api/v1/booking/series/<id>"""

    permission_classes = [IsAuthenticated]

    def get(self, request, series_id):
        if not settings.SERIES_BOOKING_V1:
            raise Http404("Not found")
        try:
            code, body = read_series_booking(
                series_id, authorization=request.META.get("HTTP_AUTHORIZATION", "")
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        if code != status.HTTP_200_OK or not isinstance(body, dict):
            return Response(body, status=code)
        return Response(_present_hub(body, st.engine_zone_of(body)))

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
from datetime import date, datetime, time, timedelta

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
    cancel_series_booking,
    create_series_booking,
    manage_series_booking,
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
from .series_serializers import _HHMM, SeriesBookingRequestSerializer
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

    @extend_schema(
        summary="Change a routine",
        description=(
            "Behind SERIES_BOOKING_V1. `action: SKIP` with `session_ids`: each "
            "session is cancelled as the customer's own choice and shown as "
            "SKIPPED; the routine goes on. Only sessions still to come and "
            "outside the 24 hour lock. `dry_run: true` checks and changes "
            "nothing. `action: RESCHEDULE` with `session_id`, `date` and `time` (the "
            "salon's clock) moves one visit. `action: EXTEND` with `sessions` (or `dates` for a "
            "CUSTOM routine) adds visits; its dry run answers the new visits. "
            "`action: PAUSE` with `until` (optional `reason`, `note`) moves the "
            "visits to the resume date onwards; `action: RESUME` (optional "
            "`frequency`, `time`, `stylist_id`) moves them back from tomorrow. "
            "Their dry runs answer the moved visits. Answers the whole routine, every time "
            "on the salon's own clock. Send an `Idempotency-Key` with a real "
            "change only. " + _ENVELOPE_NOTE
        ),
        responses={
            200: OpenApiResponse(description="The routine, after the change."),
            404: OpenApiResponse(description="No such routine, not yours, or the flag is off."),
            422: OpenApiResponse(
                description="booking-api's envelope: session_locked, session_not_changeable, "
                            "routine_not_active, invalid_sessions, invalid_action."
            ),
            503: OpenApiResponse(response=_OUR_ENVELOPE, description="booking-api unreachable."),
        },
    )
    def patch(self, request, series_id):
        """
        PATCH /api/v1/booking/series/<id> (step 6): SKIP for now. The body
        goes to booking-api as the app sent it (SKIP carries no time to
        convert); the answer is the hub on the salon's clock, as for GET.
        RESCHEDULE and EXTEND will carry a time, and convert it, when built.

        The app's own Idempotency-Key goes with a real change only, never a
        dry run, and none is made up: the same SKIP sent twice without one
        is simply refused the second time (already skipped).
        """
        if not settings.SERIES_BOOKING_V1:
            raise Http404("Not found")
        _json_only(request)
        body = dict(request.data) if isinstance(request.data, dict) else {}
        authorization = request.META.get("HTTP_AUTHORIZATION", "")
        if body.get("action") in ("EXTEND", "PAUSE", "RESUME"):
            return self._extend(request, series_id, body, authorization)
        tenant_id = None
        if body.get("action") == "RESCHEDULE":
            early, tenant_id = self._reschedule_on_engine_clock(series_id, body, authorization)
            if early is not None:
                return early
        key = None if body.get("dry_run") is True else request.META.get("HTTP_IDEMPOTENCY_KEY")
        try:
            code, answer = manage_series_booking(
                series_id,
                body,
                authorization=authorization,
                idempotency_key=key,
                tenant_id=tenant_id,
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        if code != status.HTTP_200_OK or not isinstance(answer, dict):
            return Response(answer, status=code)
        return Response(_present_hub(answer, st.engine_zone_of(answer)))

    @staticmethod
    def _reschedule_on_engine_clock(series_id, body, authorization):
        """
        RESCHEDULE carries a day and a time on the SALON's clock. It is held
        to the salon's own hours on that day and the services' notice, as a
        create's sessions are (_Hours), then put on booking-api's clock in
        `body`. A move outside the hours is refused here, before booking-api
        is asked. A shape booking-api refuses anyway (not YYYY-MM-DD, not
        HH:MM) is left for it to answer in its own words.

        Returns (response, tenant_id): a Response to send as it is (the
        routine could not be read) or None to go on, and the salon's tenant.
        booking-api needs the tenant to find the salon's services and staff
        for the hold on the new time, exactly as the create sends it.
        """
        day, hhmm = body.get("date"), body.get("time")
        if not isinstance(day, str) or not isinstance(hhmm, str) or not _HHMM.match(hhmm):
            return None, None
        try:
            the_day = date.fromisoformat(day)
        except ValueError:
            return None, None

        try:
            code, hub = read_series_booking(series_id, authorization=authorization)
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        if code != status.HTTP_200_OK or not isinstance(hub, dict):
            return Response(hub, status=code), None

        ref = hub.get("salon_id")
        card = salon_cards_for_refs([ref]).get(ref) if isinstance(ref, str) else None
        if card is None:
            return None, None
        salon, route = _salon_and_route(card["id"])
        tz = timezones.resolve(salon.branch_timezone, salon.id)
        clock = _clock(authorization, route)

        service_ids = [
            str(s["id"]) for s in hub.get("services") or [] if isinstance(s, dict) and s.get("id")
        ]
        visit = [{"service_ids": service_ids}]
        rows = _service_rows(salon, visit, "services") if service_ids else {}
        longest, lead = _party_timing(rows, visit) if rows else (0, 0)
        hours = _Hours(salon, tz, clock, longest=longest, lead=lead, now=_now(tz))

        h, m = (int(x) for x in hhmm.split(":"))
        start = datetime.combine(the_day, time(h, m), tzinfo=tz)
        why = hours.refusal(start)
        if why is not None:
            raise _refuse("time", why[1], why[0])

        on_engine = start.astimezone(gt.engine_zone(clock))
        body["date"] = on_engine.date().isoformat()
        body["time"] = on_engine.strftime("%H:%M")
        return None, route.get("tenant_id")

    def _extend(self, request, series_id, body, authorization):
        """
        EXTEND (step 6), PAUSE and RESUME (step 7), done as the create is: the picks and CUSTOM days go
        onto booking-api's clock, every new session is held to the salon's
        own hours and the services' notice, and the answer comes back on the
        salon's clock. The salon's tenant goes with every call, as for the
        create: booking-api needs it to find the salon's services.

        A dry run answers the new sessions (a preview). A real extend first
        asks booking-api for that same plan (a dry run, which saves nothing),
        refuses a session outside the hours before anything is booked, then
        books with the app's own Idempotency-Key.
        """
        try:
            code, hub = read_series_booking(series_id, authorization=authorization)
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        if code != status.HTTP_200_OK or not isinstance(hub, dict):
            return Response(hub, status=code)

        ref = hub.get("salon_id")
        card = salon_cards_for_refs([ref]).get(ref) if isinstance(ref, str) else None
        if card is None:
            raise Http404("Not found")
        salon, route = _salon_and_route(card["id"])
        tz = timezones.resolve(salon.branch_timezone, salon.id)
        clock = _clock(authorization, route)
        engine_tz = gt.engine_zone(clock)
        tenant_id = route.get("tenant_id")

        service_ids = [
            str(s["id"]) for s in hub.get("services") or [] if isinstance(s, dict) and s.get("id")
        ]
        visit = [{"service_ids": service_ids}]
        rows = _service_rows(salon, visit, "services") if service_ids else {}
        longest, lead = _party_timing(rows, visit) if rows else (0, 0)
        hours = _Hours(salon, tz, clock, longest=longest, lead=lead, now=_now(tz))

        picks = [p for p in body.get("picks") or [] if isinstance(p, dict)]
        sent = dict(body)
        sent["picks"] = [self._pick_on_engine_clock(p, tz, engine_tz) for p in picks]
        if isinstance(body.get("dates"), list):
            salon_time = st.present_hub(hub, salon_id=None, tz=tz, engine_tz=engine_tz).get("time")
            sent["dates"] = [
                self._day_on_engine_clock(d, salon_time, tz, engine_tz) for d in body["dates"]
            ]
        if isinstance(body.get("time"), str):
            # RESUME's "Customize first" time, from the salon's clock.
            sent["time"] = self._time_on_engine_clock(body["time"], tz, engine_tz)

        def ask(payload, key=None):
            try:
                return manage_series_booking(
                    series_id, payload, authorization=authorization,
                    idempotency_key=key, tenant_id=tenant_id,
                )
            except BookingApiUnavailable as exc:
                raise BookingApiDown() from exc

        code, plan = ask({**sent, "dry_run": True})
        if code != status.HTTP_200_OK or not isinstance(plan, dict):
            return Response(plan, status=code)
        preview = st.present_preview(plan, tz=tz, engine_tz=engine_tz)
        if body.get("dry_run") is True:
            return Response(st.within_hours(preview, hours.refusal, tz), status=code)

        indexed = [p for p in picks if isinstance(p.get("index"), int)]
        outside = st.first_outside_hours(preview, hours.refusal, indexed)
        if outside is not None:
            field, why, message = outside
            raise _refuse(field, message, why)

        code, answer = ask({**sent, "dry_run": False}, key=request.META.get("HTTP_IDEMPOTENCY_KEY"))
        if code != status.HTTP_200_OK or not isinstance(answer, dict):
            return Response(answer, status=code)
        return Response(_present_hub(answer, st.engine_zone_of(answer)))

    @staticmethod
    def _pick_on_engine_clock(pick, tz, engine_tz):
        """
        One pick (the salon's day and HH:MM) onto booking-api's clock. A shape
        booking-api refuses anyway is passed as it is, for it to answer.
        """
        day, hhmm = pick.get("date"), pick.get("time")
        if not isinstance(day, str) or not isinstance(hhmm, str) or not _HHMM.match(hhmm):
            return pick
        try:
            the_day = date.fromisoformat(day)
        except ValueError:
            return pick
        h, m = (int(x) for x in hhmm.split(":"))
        on_engine = datetime.combine(the_day, time(h, m), tzinfo=tz).astimezone(engine_tz)
        return {**pick, "date": on_engine.date().isoformat(), "time": on_engine.strftime("%H:%M")}

    @staticmethod
    def _day_on_engine_clock(day, salon_time, tz, engine_tz):
        """A CUSTOM day (the salon's) onto booking-api's clock, at the routine's own time."""
        if not isinstance(day, str) or not isinstance(salon_time, str) or not _HHMM.match(salon_time):
            return day
        try:
            the_day = date.fromisoformat(day)
        except ValueError:
            return day
        h, m = (int(x) for x in salon_time.split(":"))
        on_engine = datetime.combine(the_day, time(h, m), tzinfo=tz).astimezone(engine_tz)
        return on_engine.date().isoformat()

    @staticmethod
    def _time_on_engine_clock(hhmm, tz, engine_tz):
        """
        RESUME's new time (the salon's HH:MM) onto booking-api's clock, at
        today's offset between the two clocks. A shape booking-api refuses
        anyway is passed as it is, for it to answer.
        """
        if not isinstance(hhmm, str) or not _HHMM.match(hhmm):
            return hhmm
        h, m = (int(x) for x in hhmm.split(":"))
        on_engine = datetime.combine(_now(tz).date(), time(h, m), tzinfo=tz).astimezone(engine_tz)
        return on_engine.strftime("%H:%M")


class SeriesBookingCancelView(APIView):
    """
    POST /api/v1/booking/series/<id>/cancel (step 7): the customer ends their
    routine. booking-api does the cancelling (each visit through its own
    lifecycle, then the routine ends); this forwards the body as the app sent
    it, and answers on the salon's clock.

    The dry run's summary lists each visit by its id. Its date and time come
    from the routine in the same answer, once _present_hub has put it on the
    salon's clock, so times are converted in one place only.

    A cancel is final, so a `dry_run` that is not exactly true or false is
    refused before booking-api is asked anything: "true" as text must never
    cancel a routine for real. The app's Idempotency-Key goes with a real
    cancel only, and none is made up: a second cancel is simply refused
    (cannot_cancel).
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Cancel a routine",
        description=(
            "Behind SERIES_BOOKING_V1. Every visit still to come is cancelled "
            "as the customer's own cancel, under the single booking's refund "
            "rules (a visit less than 24 hours away is a late cancel), and the "
            "routine ends. Optional `reason`: NOT_SATISFIED, TOO_EXPENSIVE, "
            "MOVING or OTHER, else `invalid_cancel_reason`. `dry_run: true` "
            "changes nothing and answers the refund summary (`visits_cancelled`, "
            "`late_visits`, `paid`, `refund`, `kept`, and a line per visit with "
            "its `date` and `start_time` on the salon's clock) plus the "
            "`routine` as it is. Allowed while the hub shows `can.cancel`, else "
            "`cannot_cancel`. Send an `Idempotency-Key` with a real cancel only. "
            + _ENVELOPE_NOTE
        ),
        responses={
            200: OpenApiResponse(
                description="The routine, ended. With dry_run: the refund summary and the routine."
            ),
            404: OpenApiResponse(description="No such routine, not yours, or the flag is off."),
            422: OpenApiResponse(
                description="booking-api's envelope: cannot_cancel, invalid_cancel_reason; "
                            "or invalid_dry_run."
            ),
            503: OpenApiResponse(response=_OUR_ENVELOPE, description="booking-api unreachable."),
        },
    )
    def post(self, request, series_id):
        if not settings.SERIES_BOOKING_V1:
            raise Http404("Not found")
        _json_only(request)
        body = request.data if isinstance(request.data, dict) else {}
        if "dry_run" in body and not isinstance(body["dry_run"], bool):
            raise _refuse("dry_run", "dry_run is true or false.", "invalid_dry_run")
        dry_run = body.get("dry_run") is True
        try:
            code, answer = cancel_series_booking(
                series_id,
                body,
                authorization=request.META.get("HTTP_AUTHORIZATION", ""),
                idempotency_key=None if dry_run else request.META.get("HTTP_IDEMPOTENCY_KEY"),
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        if code != status.HTTP_200_OK or not isinstance(answer, dict):
            return Response(answer, status=code)
        if not dry_run:
            return Response(_present_hub(answer, st.engine_zone_of(answer)))
        hub, summary = answer.get("routine"), answer.get("summary")
        if not isinstance(hub, dict) or not isinstance(summary, dict):
            return Response(answer, status=code)
        routine = _present_hub(hub, st.engine_zone_of(hub))
        return Response({
            "dry_run": True,
            "summary": st.present_cancel(summary, routine),
            "routine": routine,
        })

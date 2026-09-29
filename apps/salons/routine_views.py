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
preview in C3, the create in C4, the move in C5.

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
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import group_translate as gt
from . import group_views as gv
from . import routine_contract as rc
from . import series_translate as st
from . import timezones
from .booking_api import (
    BookingApiUnavailable,
    create_series_booking,
    manage_series_booking,
    read_routine_booking,
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
from .routine_serializers import (
    RoutineCreateRequestSerializer,
    RoutineMoveRequestSerializer,
    RoutinePreviewRequestSerializer,
)
from .selectors import salon_cards_for_refs
from .series_views import _HOURS_MESSAGES, _Hours
from .views import _OUR_ENVELOPE, BookingApiDown, _idempotency_key, _salon_card

logger = logging.getLogger(__name__)


def _gate():
    """404 with the flag off, before anything else is looked at."""
    if not settings.ROUTINE_CONTRACT_V1:
        raise Http404("Not found")


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
    """
    A translator refusal as our 422, in exactly the envelope our exception
    handler writes, but ANSWERED, not raised: the handler keeps only field,
    code and message, and amount_mismatch must keep its `expected` figure.
    """
    error = exc.as_error()
    detail = error["message"] if error["field"] is None else rc.VALIDATION
    return Response(
        {"detail": detail, "code": "validation_error", "errors": [error]},
        status=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


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
        stylist = str(data["stylist_id"]) if data.get("stylist_id") else None
        roster, candidates = _stylist_and_candidates(salon, service_ids, stylist)

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
            return _refused(exc)

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


def _present(routine, *, salon, tz, card=None):
    """
    A routine read with view=booking, as the app reads it (draft section 5):
    on the salon's clock, the avatars from our stylist list, and the full
    salon card, found as GET /booking/{id} finds it.
    """
    ref = routine.get("salon_id")
    if card is None and isinstance(ref, str):
        card = salon_cards_for_refs([ref]).get(ref)
    avatars = {str(s.id): s.avatar_url for s in gv.salon_stylists(salon)}
    out = rc.present_routine(
        routine, salon_id=card["id"] if card else str(salon.id), tz=tz, avatars=avatars,
    )
    out["salon"] = _salon_card(card, full=True)
    return out


def _after_the_change(routine_id, *, done, salon, tz, authorization, tenant_id):
    """
    The routine booking-api has just changed (`done`: "booked", "moved"),
    read back with view=booking and presented.

    DONE IS DONE. From here on nothing may answer an error: an app told
    "failed" would do it again. Whatever goes wrong in the read-back, the
    answer is still the success, with at least the routine's id and
    booking_type, and a warning in the log.
    """
    minimal = {"id": routine_id, "booking_type": "ROUTINE"}
    try:
        code, routine = read_routine_booking(
            routine_id, authorization=authorization, tenant_id=tenant_id,
        )
        if code != status.HTTP_200_OK or not isinstance(routine, dict):
            logger.warning(
                "routine %s is %s, but reading it back answered %s", routine_id, done, code,
            )
            return minimal
        return _present(routine, salon=salon, tz=tz)
    except Exception:
        # Deliberately broad: the change is made either way.
        logger.warning(
            "routine %s is %s, but reading it back failed", routine_id, done, exc_info=True,
        )
        return minimal


def _hours_refusal(hours, start, tz, field):
    """
    A start the salon's own hours refuse, as our 422 on `field`: the reason
    final_reason picks, in the old route's words. None when the hours allow it.
    """
    reason = rc.final_reason(hours(start), None)
    if reason is None:
        return None
    local = start.astimezone(tz)
    return _refuse(
        field,
        _HOURS_MESSAGES[reason].format(day=local.date().isoformat(), time=local.strftime("%H:%M")),
        reason,
    )


def _stylist_and_candidates(salon, service_ids, stylist_id):
    """
    The old route's stylist check, or (no stylist_id) Any Available Expert's
    candidates: the roster, a stylist on it who does all the services.
    """
    roster = _roster(salon)
    if stylist_id is not None:
        _check_stylists(salon, [{"stylist_id": stylist_id, "service_ids": service_ids}], roster)
        return roster, []
    return roster, _candidates(salon, service_ids, roster)


class RoutineCreateView(APIView):
    """POST /api/v1/booking/routine"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Book a routine (app contract)",
        description=(
            "Behind ROUTINE_CONTRACT_V1 (off: 404, booking-api is not called). "
            "Every session within 90 days booked in one call, or none; the ones "
            "further away saved and booked when they come within 90 days. "
            "`start_time` is the preview's own (the cadence's anchor); each of "
            "`sessions` is its cadence slot or a time the alternatives rule "
            "allows. Pay at the salon only for now: payment_status DRAFT, "
            "advance_paid_amount 0, due_amount the total, no products, no "
            "promo_code. Send an Idempotency-Key (one is derived from the "
            "request when none is sent) and wait up to 60 seconds. 201: the "
            "routine as one booking, every time on the salon's own clock."
        ),
        request=RoutineCreateRequestSerializer,
        responses={
            201: OpenApiResponse(description="The routine, as GET /booking/{id} reads it."),
            404: OpenApiResponse(response=_OUR_ENVELOPE, description="No such salon, or the flag is off."),
            409: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description="slot_taken on sessions[i]: nothing was booked; idempotency_key_reused.",
            ),
            415: OpenApiResponse(response=_OUR_ENVELOPE, description="Not application/json."),
            422: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description="The section 4 codes, invalid_sessions, date_mismatch, invalid_window, "
                            "salon_closed / outside_hours / too_soon on sessions[i], "
                            "session_not_offered, amount_mismatch (with expected), ...",
            ),
            503: OpenApiResponse(response=_OUR_ENVELOPE, description="booking-api unreachable."),
        },
    )
    def post(self, request):
        _gate()
        _json_only(request)
        # FIRST, before request.data, as the old route: the key may be derived
        # from the bytes the app sent, and DRF's parsing consumes the stream.
        idempotency_key = _idempotency_key(request)

        data = _validated(RoutineCreateRequestSerializer, request)
        salon, route = _salon_and_route(data["salon_id"])
        service_ids = [str(s["id"]) for s in data["services"]]
        visit = [{"service_ids": service_ids}]
        rows = _service_rows(salon, visit, "services")
        stylist = str(data["stylist_id"]) if data.get("stylist_id") else None
        _, candidates = _stylist_and_candidates(salon, service_ids, stylist)

        sent = request.data if isinstance(request.data, dict) else {}
        contract = {
            **sent,
            "salon_id": str(data["salon_id"]),
            "services": [{"id": sid} for sid in service_ids],
            "stylist_id": stylist,
            "start_time": data["start_time"],
            "cadence": data["cadence"],
            "sessions": data["sessions"],
        }
        try:
            # Section 4, before anything is asked of booking-api.
            rc.check_payment(contract)
        except rc.Refusal as exc:
            return _refused(exc)

        tz = timezones.resolve(salon.branch_timezone, salon.id)
        authorization = request.META.get("HTTP_AUTHORIZATION", "")
        clock = _clock(authorization, route)

        # ---- booking-api's own plan first, saving nothing -------------------
        try:
            plan_body = rc.preview_body(
                rc.plan_request(contract),
                branch_id=route["branch_id"], tz=tz, clock=clock, candidates=candidates,
            )
        except rc.Refusal as exc:
            return _refused(exc)
        try:
            code, plan = create_series_booking(
                plan_body, authorization=authorization, idempotency_key=None,
                tenant_id=route["tenant_id"],
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        if code != status.HTTP_200_OK or not isinstance(plan, dict):
            if code == 400:
                logger.warning("booking-api refused a routine plan body: %r", plan)
            answer_status, answer = rc.translate_refusal(code, plan, route="create")
            return Response(answer, status=answer_status)

        salon_plan = st.present_preview(plan, tz=tz, engine_tz=gt.engine_zone(clock))
        try:
            body = rc.create_body(
                contract, branch_id=route["branch_id"], tz=tz, clock=clock,
                candidates=candidates,
                slots=rc.plan_slots(salon_plan), minutes=rc.plan_minutes(salon_plan),
            )
        except rc.Refusal as exc:
            return _refused(exc)

        # ---- every session inside the salon's hours, before booking --------
        longest, lead = _party_timing(rows, visit) if rows else (0, 0)
        hours = _salon_hours(salon, tz, clock, longest=longest, lead=lead)
        for i, session in enumerate(contract["sessions"]):
            start = rc.parse_instant(session["start_time"], f"sessions[{i}].start_time")
            refusal = _hours_refusal(hours, start, tz, f"sessions[{i}]")
            if refusal is not None:
                raise refusal

        # ---- the routine ------------------------------------------------------
        try:
            code, answer = create_series_booking(
                body,
                authorization=authorization,
                # The real create only, as the old route: the app's key, or
                # the one derived from what it sent.
                idempotency_key=idempotency_key,
                tenant_id=route["tenant_id"],
                # Up to 6 visits booked one by one: the long wait is the
                # create's alone.
                timeout=settings.SERIES_BOOKING_CREATE_TIMEOUT,
            )
        except BookingApiUnavailable as exc:
            # Safe to send again with the same key once the first has
            # finished: booking-api replays the routine it booked.
            raise BookingApiDown() from exc
        if code not in (status.HTTP_200_OK, status.HTTP_201_CREATED) or not isinstance(answer, dict):
            if code == 400:
                logger.warning("booking-api refused a routine body: %r", answer)
            answer_status, refusal = rc.translate_refusal(
                code, answer, route="create", picks=body["picks"],
            )
            return Response(refusal, status=answer_status)

        return Response(
            _after_the_change(
                answer.get("id"), done="booked", salon=salon, tz=tz,
                authorization=authorization, tenant_id=route["tenant_id"],
            ),
            status=status.HTTP_201_CREATED,
        )


class RoutineSessionMoveView(APIView):
    """PATCH /api/v1/booking/<id>/sessions/<session_id>: <id> is the routine."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Move one session of a routine (app contract)",
        description=(
            "Behind ROUTINE_CONTRACT_V1 (off: 404, booking-api is not called). "
            "`start_time` (any offset) is the session's new start; the routine "
            "keeps its cadence and no other session changes. `stylist_id` "
            "moves the session to that stylist; left out, it keeps its own. "
            "`dry_run: true` checks everything and moves nothing: a 200 means "
            "the move is allowed and the time is free right now. Send an "
            "Idempotency-Key with a real move only. 200: the whole routine, "
            "every time on the salon's own clock."
        ),
        request=RoutineMoveRequestSerializer,
        responses={
            200: OpenApiResponse(description="The routine (contract section 5)."),
            404: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description="No such routine, not yours, no such session in it, or the flag is off.",
            ),
            409: OpenApiResponse(response=_OUR_ENVELOPE, description="slot_taken on start_time."),
            415: OpenApiResponse(response=_OUR_ENVELOPE, description="Not application/json."),
            422: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description="invalid_time, invalid_dry_run, foreign_id, stylist_mismatch, "
                            "salon_closed / outside_hours / too_soon on start_time; or "
                            "booking-api's session_locked, session_not_changeable, "
                            "reschedule_out_of_range, session_day_taken, routine_not_active.",
            ),
            503: OpenApiResponse(response=_OUR_ENVELOPE, description="booking-api unreachable."),
        },
    )
    def patch(self, request, booking_id, session_id):
        _gate()
        _json_only(request)
        sent = request.data if isinstance(request.data, dict) else {}
        # A dry run must never become a real move: "true" as text is refused,
        # as the old cancel refuses it.
        if "dry_run" in sent and not isinstance(sent["dry_run"], bool):
            raise _refuse("dry_run", "dry_run is true or false.", "invalid_dry_run")
        data = _validated(RoutineMoveRequestSerializer, request)
        try:
            rc.parse_instant(data["start_time"], "start_time")
        except rc.Refusal as exc:
            return _refused(exc)

        # ---- the routine first, as the old RESCHEDULE reads it -----------
        authorization = request.META.get("HTTP_AUTHORIZATION", "")
        routine_id = str(booking_id)
        try:
            code, routine = read_routine_booking(routine_id, authorization=authorization)
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        if code != status.HTTP_200_OK or not isinstance(routine, dict):
            # Not the caller's, or not there: booking-api's 404, as today.
            answer_status, answer = rc.translate_refusal(code, routine, route="move")
            return Response(answer, status=answer_status)

        ref = routine.get("salon_id")
        card = salon_cards_for_refs([ref]).get(ref) if isinstance(ref, str) else None
        if card is None:
            raise Http404("Not found")
        salon, route = _salon_and_route(card["id"])
        tz = timezones.resolve(salon.branch_timezone, salon.id)
        clock = _clock(authorization, route)

        service_ids = [
            str(s["id"]) for s in routine.get("services") or [] if isinstance(s, dict) and s.get("id")
        ]
        stylist = str(data["stylist_id"]) if data.get("stylist_id") else None
        if stylist is not None:
            _check_stylists(
                salon, [{"stylist_id": stylist, "service_ids": service_ids}], _roster(salon),
            )

        payload = {
            "start_time": data["start_time"],
            "stylist_id": stylist,
            "dry_run": sent.get("dry_run") is True,
        }
        move = rc.move_request(payload, session_id, tz)

        # ---- the new time inside the salon's hours, before booking-api -----
        visit = [{"service_ids": service_ids}]
        rows = _service_rows(salon, visit, "services") if service_ids else {}
        longest, lead = _party_timing(rows, visit) if rows else (0, 0)
        hours = _salon_hours(salon, tz, clock, longest=longest, lead=lead)
        _, _, start = rc.salon_day_and_time(data["start_time"], tz, "start_time")
        refusal = _hours_refusal(hours, start, tz, "start_time")
        if refusal is not None:
            raise refusal

        # ---- on booking-api's clock, as the old RESCHEDULE sends it --------
        on_engine = start.astimezone(gt.engine_zone(clock))
        move["date"] = on_engine.date().isoformat()
        move["time"] = on_engine.strftime("%H:%M")
        dry_run = move["dry_run"]
        try:
            code, answer = manage_series_booking(
                routine_id,
                move,
                authorization=authorization,
                # As the old PATCH: the app's own key, with a real move only,
                # never with a dry run, and never made up.
                idempotency_key=None if dry_run else request.META.get("HTTP_IDEMPOTENCY_KEY"),
                tenant_id=route["tenant_id"],
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        if code != status.HTTP_200_OK or not isinstance(answer, dict):
            answer_status, refusal_body = rc.translate_refusal(code, answer, route="move")
            return Response(refusal_body, status=answer_status)

        if dry_run:
            # Nothing moved: the routine as it is now.
            return Response(_present(routine, salon=salon, tz=tz, card=card))
        return Response(
            _after_the_change(
                routine_id, done="moved", salon=salon, tz=tz,
                authorization=authorization, tenant_id=route["tenant_id"],
            )
        )

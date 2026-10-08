"""
A customer's own SINGLE booking, after it is booked (SINGLE_BOOKING_ACTIONS_V1):

    POST /api/v1/booking/<id>/cancel       for a single booking's id
    POST /api/v1/booking/<id>/reschedule   a single booking only

The cancel route belongs to GroupBookingCancelView (group_views.py). With the
flag on it asks this module first, and only a SINGLE booking is answered
here. Every other id (a party, a routine, a booking that is not the caller's,
no booking at all) goes on to the group cancel exactly as it did before the
flag.

The reschedule route is this module's own (SingleBookingRescheduleView). It
moves a SINGLE booking and nothing else: every other id, and every id with
the flag off, is the 404 it was before the route existed.

WHAT THE ID IS, FROM THE SERVER, NEVER BY TRYING. Cancelling as a party and
falling back on a 404 would turn "not yours" and "no such booking" into a
second cancel attempt. The type comes from the caller's own Upcoming shelf at
booking-api, the same rows the app drew the Cancel button from:

  * a single booking's read (GET /v1/mobile-booking/<id>) cannot be used. It
    only names `booking_type` behind booking-api's MOBILE_ROUTINE_CONTRACT,
    and never says a booking is one lane of a party, so a routine session or
    a party lane would look single and be cancelled on its own;
  * the list names all three. A party is ONE `GROUP` row under the party's
    id, and a routine session is a `ROUTINE` row;
  * a booking that can still be cancelled is live and still to come, so it
    is on Upcoming. Anything not there is not a single cancel.

THE LOOKUP NEVER BREAKS A PARTY'S CANCEL. Before the flag, a group cancel did
not depend on the list at all. So a list that fails, refuses, is
unreachable or is slow is "not a single booking", and the group cancel
answers as before. It waits SINGLE_LOOKUP_TIMEOUT (3 seconds), not
BOOKING_API_TIMEOUT, so with booking-api down a cancel does not wait two
full timeouts before its 503. Only the first page is read: a customer with
more live bookings than one page holds is not a real case, and round trips
before every cancel are.

booking-api owns the booking and decides the refund. This module finds the
type, builds the body, and passes booking-api's answer back as it came.

THE MOVE IS THE ROUTINE MOVE'S CHECKS, THEN A HOLD, THEN THE MOVE. The new
time is held to the salon's own hours with the routine move's helpers
(routine_views._salon_hours and _hours_refusal) before booking-api is asked
for anything. Then POST /v1/holds takes the new slot, and POST
/v1/bookings/<id>/reschedule moves the booking onto it. A move that fails
after the hold gives the slot back at once, so a failed move never keeps a
slot from other customers for the hold's 15 minutes.
"""
import logging
import urllib.parse
import uuid
from datetime import datetime, time

from django.conf import settings
from django.http import Http404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.exceptions import UnsupportedMediaType
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import group_translate as gt
from . import timezones
from .booking_api import (
    BookingApiUnavailable,
    cancel_booking,
    list_bookings,
    place_hold,
    release_hold,
    reschedule_booking,
)
from .group_views import (
    _check_stylists,
    _clock,
    _json_only,
    _party_timing,
    _refuse,
    _roster,
    _salon_and_route,
    _service_rows,
    _validated,
)
from .routine_views import _hours_refusal, _salon_hours
from .selectors import salon_cards_for_refs
from .series_views import _HOURS_MESSAGES
from .single_serializers import SingleRescheduleRequestSerializer
from .views import _OUR_ENVELOPE, BookingApiDown

logger = logging.getLogger(__name__)

# The reasons the app may send: the same four as a routine's cancel
# (docs/MOBILE_ROUTINE_BOOKING_FE.md). No free text.
CANCEL_REASONS = ("NOT_SATISFIED", "TOO_EXPENSIVE", "MOVING", "OTHER")

# What booking-api's history says. booking-api refuses a blank reason (422
# "Choose a reason"), so it is always sent, with the app's reason after it
# when there is one. The same form as booking-api's routine cancel.
CANCEL_TEXT = "Cancelled by the customer in the app"

# booking-api's own cap on a page. Only the first page is read: an id past
# it is not treated as single.
LOOKUP_PAGE_SIZE = 50

# What booking-api's history says for a move. booking-api requires a reason
# on every reschedule; the app has no picker for one, so it is always this.
RESCHEDULE_TEXT = "Rescheduled by the customer in the app."

# The channel the app's own booking was made on (booking-api's mobile create
# holds with it), so a move is held to the same rules.
HOLD_CHANNEL = "online"


def single_cancel_response(request, booking_id):
    """
    The answer for a SINGLE booking's id, or None for any other id, which the
    group cancel then answers exactly as before.
    """
    authorization = request.META.get("HTTP_AUTHORIZATION", "")
    row = upcoming_row(
        booking_id, authorization, tenant_id=request.META.get("HTTP_X_TENANT_ID"),
    )
    if row is None or row.get("booking_type") != "SINGLE":
        return None

    # Only now is the body read: a party's cancel has no body and never did.
    reason = cancel_reason(_body(request))
    try:
        code, answer = cancel_booking(
            booking_id,
            # BUILT HERE, never the app's body: booking-api answers 400 for
            # any key it does not know.
            {"reason": cancel_text(reason)},
            authorization=authorization,
            # The app's own key, if it sent one, and never made up: a second
            # cancel is refused by the booking's state anyway.
            idempotency_key=request.META.get("HTTP_IDEMPOTENCY_KEY"),
        )
    except BookingApiUnavailable as exc:
        raise BookingApiDown() from exc
    # booking-api's answer as it came: its status (201, 404, 409, ...) and
    # its body, with the refund in it.
    return Response(answer, status=code)


def upcoming_row(booking_id, authorization, *, tenant_id=None):
    """
    The caller's own row with this id on the first page of their Upcoming
    shelf, or None: not there, or the list failed, refused, could not be
    reached or took longer than SINGLE_LOOKUP_TIMEOUT. Never an error of its
    own, so a party's cancel never depends on it, and a move of anything but
    a single booking answers as it did before the route.
    """
    query = urllib.parse.urlencode({"filter": "upcoming", "page": 1, "pageSize": LOOKUP_PAGE_SIZE})
    try:
        code, body = list_bookings(
            query, authorization=authorization, tenant_id=tenant_id,
            timeout=settings.SINGLE_LOOKUP_TIMEOUT,
        )
    except BookingApiUnavailable:
        logger.warning("single booking: the Upcoming lookup could not reach booking-api")
        return None
    if code != 200 or not isinstance(body, dict):
        logger.warning("single booking: the Upcoming lookup answered %s", code)
        return None
    wanted = str(booking_id).lower()
    for row in body.get("results") or []:
        if isinstance(row, dict) and str(row.get("id", "")).lower() == wanted:
            return row
    return None


def cancel_reason(body):
    """
    The app's reason: one of CANCEL_REASONS, or None when none was given.
    Left out, null or blank is none (a picker left empty). Anything else is
    422 invalid_cancel_reason, so a booking is never cancelled under a reason
    the customer did not pick.
    """
    raw = body.get("reason")
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    if isinstance(raw, str) and raw.strip() in CANCEL_REASONS:
        return raw.strip()
    raise _refuse(
        "reason",
        "reason must be NOT_SATISFIED, TOO_EXPENSIVE, MOVING or OTHER.",
        "invalid_cancel_reason",
    )


def cancel_text(reason):
    """The sentence booking-api stores. Never blank."""
    if reason is None:
        return f"{CANCEL_TEXT}."
    return f"{CANCEL_TEXT}. Reason: {reason}."


def _body(request):
    """
    The app's body as a dict. Empty is fine, with any content type, as a
    party's cancel is. A body that is there must be JSON.
    """
    if not request.body.strip():
        return {}
    media_type = (request.content_type or "").split(";")[0].strip().lower()
    if media_type != "application/json":
        raise UnsupportedMediaType(media_type or "none")
    return request.data if isinstance(request.data, dict) else {}


# ------------------------------------------------------------ the move


@extend_schema(
    summary="Move a single booking to a new time",
    description=(
        "Behind SINGLE_BOOKING_ACTIONS_V1 (off: 404, booking-api is not "
        "called). A SINGLE booking only: the type is looked up on the first "
        "page of the caller's own Upcoming shelf, and any other id (a party, "
        "a routine, a booking that is not the caller's, or a lookup that "
        "fails) is 404 and nothing moves.\n\n"
        "`date` and `time` are the new start on the salon's own clock. "
        "`stylist_id` moves the booking to that stylist; left out or null, it "
        "keeps its own. A booking with more than one stylist cannot be moved "
        "here (422 `multiple_stylists`). The new time is held to the salon's "
        "hours before booking-api is asked, then held, then the booking is "
        "moved onto it. A move that fails after the hold gives the slot back "
        "at once.\n\n"
        "booking-api decides the deposit, and its answer comes back as it "
        "came: 201 with `code`, `from`, `to`, `moveCount`, `lateMove`, "
        "`deposit`, `depositOutcome` and `explanation`, or its refusal. Its "
        "400 on the hold is answered in this service's 422 envelope."
    ),
    request=SingleRescheduleRequestSerializer,
    responses={
        201: OpenApiResponse(description="Moved: booking-api's answer, with the deposit outcome."),
        404: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description="Not a single booking of the caller's on Upcoming, no such salon, or the flag is off.",
        ),
        409: OpenApiResponse(description="booking-api: the new time is not free, or the booking cannot be moved."),
        410: OpenApiResponse(description="booking-api: the hold went before the move. Nothing moved."),
        415: OpenApiResponse(response=_OUR_ENVELOPE, description="Not application/json."),
        422: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description="invalid date or time, `multiple_stylists`, `stylist_required`, "
                        "`foreign_id` (a stylist or service not at this salon), "
                        "`stylist_mismatch`, `salon_closed` / `outside_hours` / `too_soon` "
                        "on `time`, or booking-api's 400 on the hold (`outside_hours`, "
                        "`reschedule_refused`).",
        ),
        503: OpenApiResponse(response=_OUR_ENVELOPE, description="booking-api unreachable."),
    },
)
class SingleBookingRescheduleView(APIView):
    """POST /api/v1/booking/<id>/reschedule"""

    permission_classes = [IsAuthenticated]

    def post(self, request, booking_id):
        if not settings.SINGLE_BOOKING_ACTIONS_V1:
            raise Http404("Not found")
        authorization = request.META.get("HTTP_AUTHORIZATION", "")
        row = upcoming_row(
            booking_id, authorization, tenant_id=request.META.get("HTTP_X_TENANT_ID"),
        )
        if row is None or row.get("booking_type") != "SINGLE":
            # The answer this path gave before the route existed.
            raise Http404("Not found")

        _json_only(request)
        own = _own_stylist(row)
        data = _validated(SingleRescheduleRequestSerializer, request)

        # ---- the salon, the services and the stylist, as the routine move --
        ref = row.get("salon_id")
        card = salon_cards_for_refs([ref]).get(ref) if isinstance(ref, str) else None
        if card is None:
            raise Http404("Not found")
        salon, route = _salon_and_route(card["id"])
        tz = timezones.resolve(salon.branch_timezone, salon.id)

        sent_ids, service_ids = _booked_services(row)
        visit = [{"service_ids": service_ids}]
        rows = _service_rows(salon, visit, "services")

        if data.get("stylist_id"):
            stylist = str(data["stylist_id"])
            _check_stylists(
                salon, [{"stylist_id": stylist, "service_ids": service_ids}], _roster(salon),
            )
        elif own is not None:
            stylist = own
        else:
            # Never "anyone free": booking-api would pick a stylist for them.
            raise _refuse("stylist_id", "Choose a stylist for this booking.", "stylist_required")

        # ---- the new time inside the salon's hours, before booking-api -----
        minutes = gt.hhmm_minutes(data["time"])
        start = datetime.combine(data["date"], time(minutes // 60, minutes % 60), tzinfo=tz)
        longest, lead = _party_timing(rows, visit)
        clock = _clock(authorization, route)
        hours = _salon_hours(salon, tz, clock, longest=longest, lead=lead)
        refusal = _hours_refusal(hours, start, tz, "time")
        if refusal is not None:
            raise refusal

        # ---- hold the new slot, on booking-api's clock ---------------------
        day, minute = gt.to_engine(start, clock)
        hold = {
            "branch": str(route["branch_id"]),
            "day": day,
            "services": sent_ids,
            "startMin": minute,
            "staffId": stylist,
            "channel": HOLD_CHANNEL,
        }
        tenant_id = route["tenant_id"]
        try:
            code, held = place_hold(hold, authorization=authorization, tenant_id=tenant_id)
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        if code == status.HTTP_400_BAD_REQUEST:
            raise _hold_refused(held, start, tz)
        if code not in (status.HTTP_200_OK, status.HTTP_201_CREATED):
            return Response(held, status=code)
        hold_id = held.get("holdId") if isinstance(held, dict) else None
        if not isinstance(hold_id, str) or not hold_id:
            logger.warning("single reschedule: booking-api held a slot but named no hold: %r", held)
            raise BookingApiDown()

        # ---- move the booking onto it --------------------------------------
        # No Idempotency-Key: every try holds afresh, so a key sent again
        # would carry a new holdId and booking-api would refuse it as reused.
        # A second tap is safe without one: its hold finds the slot taken.
        try:
            code, moved = reschedule_booking(
                booking_id,
                # BUILT HERE, never the app's body.
                {"holdId": hold_id, "day": day, "reason": RESCHEDULE_TEXT},
                authorization=authorization, tenant_id=tenant_id,
            )
        except BookingApiUnavailable as exc:
            # Safe whether or not the move landed: a move that did consumed
            # the hold, and releasing it then frees nothing.
            _release(hold_id, authorization=authorization, tenant_id=tenant_id)
            raise BookingApiDown() from exc
        if code not in (status.HTTP_200_OK, status.HTTP_201_CREATED):
            _release(hold_id, authorization=authorization, tenant_id=tenant_id)
        # booking-api's answer as it came: the move with its deposit outcome,
        # or its refusal.
        return Response(moved, status=code)


def stylist_ids(row):
    """Every stylist a booking row names, once each, in the row's order."""
    ids = []
    for s in row.get("stylists") or []:
        sid = s.get("id") if isinstance(s, dict) else None
        if isinstance(sid, str) and sid and sid not in ids:
            ids.append(sid)
    return ids


def too_many_stylists(row):
    """
    True when a single booking cannot be moved here: it has more than one
    stylist. A hold takes one stylist, and booking-api's move puts the whole
    visit on the first service's line. The ONE rule for both answers: this
    route refuses it (multiple_stylists), and the list's can_reschedule is
    false for it (views.BookingListView).
    """
    return len(stylist_ids(row)) > 1


def _own_stylist(row):
    """The booking's one stylist, or None when the row names none."""
    if too_many_stylists(row):
        raise _refuse(
            "non_field_errors",
            "This booking has more than one stylist, so it cannot be moved in "
            "the app. Please contact the salon.",
            "multiple_stylists",
        )
    ids = stylist_ids(row)
    return ids[0] if ids else None


def _booked_services(row):
    """
    The booking's services: as booking-api names them (for the hold), and as
    this service's ids (for the salon's own checks). `foreign_id` when there
    are none, or one is not an id this salon could offer.
    """
    sent = [
        str(s["id"]) for s in row.get("services") or [] if isinstance(s, dict) and s.get("id")
    ]
    ours = []
    for sid in sent:
        try:
            ours.append(str(uuid.UUID(sid)))
        except ValueError:
            ours = []
            break
    if not ours:
        raise _refuse("services", "One or more services are not offered by this salon.", "foreign_id")
    return sent, ours


def _hold_refused(body, start, tz):
    """
    booking-api's 400 on the hold, as our 422, so this route answers one
    error shape. The body is built here, so a 400 is a start booking-api will
    not take (its startMin bound) or a bug: both logged.
    """
    logger.warning("single reschedule: booking-api refused the hold body: %r", body)
    messages = body.get("message") if isinstance(body, dict) else None
    if isinstance(messages, str):
        messages = [messages]
    if any("startMin" in str(m) for m in messages or []):
        local = start.astimezone(tz)
        return _refuse(
            "time",
            _HOURS_MESSAGES["outside_hours"].format(
                day=local.date().isoformat(), time=local.strftime("%H:%M"),
            ),
            "outside_hours",
        )
    return _refuse(
        "non_field_errors",
        "This booking could not be moved to that time. Please pick another time.",
        "reschedule_refused",
    )


def _release(hold_id, *, authorization, tenant_id):
    """
    Give the held slot back now. Never raises: the move's own answer is what
    the app needs, and a hold left behind lapses in 15 minutes anyway.
    """
    try:
        code, _ = release_hold(hold_id, authorization=authorization, tenant_id=tenant_id)
    except BookingApiUnavailable:
        logger.warning("single reschedule: could not release hold %s, it lapses on its own", hold_id)
        return
    if code != status.HTTP_200_OK:
        logger.warning("single reschedule: releasing hold %s answered %s", hold_id, code)

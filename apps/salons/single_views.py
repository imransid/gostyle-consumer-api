"""
A customer's own SINGLE booking, after it is booked (SINGLE_BOOKING_ACTIONS_V1):

    POST /api/v1/booking/<id>/cancel   for a single booking's id

The route belongs to GroupBookingCancelView (group_views.py). With the flag on
it asks this module first, and only a SINGLE booking is answered here. Every
other id (a party, a routine, a booking that is not the caller's, no booking
at all) goes on to the group cancel exactly as it did before the flag.

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

booking-api owns the booking and decides the refund. This module finds the
type, builds the body, and passes booking-api's answer back as it came.
"""
import urllib.parse

from rest_framework.exceptions import UnsupportedMediaType
from rest_framework.response import Response

from .booking_api import BookingApiUnavailable, cancel_booking, list_bookings
from .group_views import _refuse
from .views import BookingApiDown

# The reasons the app may send: the same four as a routine's cancel
# (docs/MOBILE_ROUTINE_BOOKING_FE.md). No free text.
CANCEL_REASONS = ("NOT_SATISFIED", "TOO_EXPENSIVE", "MOVING", "OTHER")

# What booking-api's history says. booking-api refuses a blank reason (422
# "Choose a reason"), so it is always sent, with the app's reason after it
# when there is one. The same form as booking-api's routine cancel.
CANCEL_TEXT = "Cancelled by the customer in the app"

# booking-api's own cap on a page. A customer with more live bookings than
# LOOKUP_MAX_PAGES pages is not looked through past it: such an id is not
# treated as single.
LOOKUP_PAGE_SIZE = 50
LOOKUP_MAX_PAGES = 20


def single_cancel_response(request, booking_id):
    """
    The answer for a SINGLE booking's id, or None for any other id, which the
    group cancel then answers exactly as before.
    """
    authorization = request.META.get("HTTP_AUTHORIZATION", "")
    row, refused = upcoming_row(
        booking_id, authorization, tenant_id=request.META.get("HTTP_X_TENANT_ID"),
    )
    if refused is not None:
        return refused
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
    (row, None): the caller's own Upcoming row with this id.
    (None, None): no such row.
    (None, Response): booking-api refused the lookup, with its answer.
    """
    wanted = str(booking_id).lower()
    for page in range(1, LOOKUP_MAX_PAGES + 1):
        query = urllib.parse.urlencode(
            {"filter": "upcoming", "page": page, "pageSize": LOOKUP_PAGE_SIZE}
        )
        try:
            code, body = list_bookings(query, authorization=authorization, tenant_id=tenant_id)
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        if code != 200:
            return None, Response(body, status=code)
        if not isinstance(body, dict):
            raise BookingApiDown()
        rows = body.get("results") or []
        for row in rows:
            if isinstance(row, dict) and str(row.get("id", "")).lower() == wanted:
                return row, None
        if not rows or page * LOOKUP_PAGE_SIZE >= (body.get("count") or 0):
            break
    return None, None


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

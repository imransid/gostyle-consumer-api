"""
`is_rated` and `can_rate` on every booking object of GET /bookings and
GET /booking/:id (docs/rate-your-visit-fe-contract.md, section 3).

    is_rated   a review exists for this booking (only ever on a completed one)
    can_rate   the same value as `can_rate` in GET /booking/:id/review

A party's object (its id is the group's) stands for the caller's own lane,
the booking GET /booking/:id/review opens for that id. On a party's read,
each member row gets the two for that member's lane, and `can_rate` only on
the caller's own: only the owner of a booking can rate it.
"""

import logging

from django.utils import timezone

from . import visits
from .models import Review

logger = logging.getLogger(__name__)


def add_to_rows(customer_id, rows):
    """GET /bookings: both keys on every row of the page, with one read per table."""
    rows = [row for row in rows if isinstance(row, dict)]
    try:
        lanes = visits.lanes_for([row.get("id") for row in rows], customer_id)
        flags = _flags(customer_id, list(lanes.values()))
    except visits.BookingDataUnavailable as exc:
        logger.warning("is_rated / can_rate without booking data: %s", exc)
        _fallback(rows)
        return
    for row in rows:
        lane = lanes.get(row.get("id"))
        row.update(flags[lane["id"]] if lane else _no())


def add_to_booking(customer_id, booking):
    """GET /booking/:id: both keys on the booking, and on each member of a party's read."""
    members = _members(booking)
    try:
        lane = visits.lanes_for([booking.get("id")], customer_id).get(booking.get("id"))
        member_lanes = visits.party_lanes(booking.get("id")) if members else {}
        flags = _flags(customer_id, ([lane] if lane else []) + list(member_lanes.values()))
    except visits.BookingDataUnavailable as exc:
        logger.warning("is_rated / can_rate without booking data: %s", exc)
        _fallback([booking, *members])
        return

    booking.update(flags[lane["id"]] if lane else _no())
    for member in members:
        member_lane = member_lanes.get(str(member.get("id")))
        member.update(flags[member_lane["id"]] if member_lane else _no())


def _flags(customer_id, lanes):
    """booking id -> {is_rated, can_rate} for these booking rows."""
    if not lanes:
        return {}
    ids = {lane["id"] for lane in lanes}
    reviewed = set(Review.objects.filter(booking_id__in=ids).values_list("booking_id", flat=True))
    completed = visits.completion_times([lane["id"] for lane in lanes if lane["status"] in visits.DONE])
    now = timezone.now()

    out = {}
    for lane in lanes:
        done = lane["status"] in visits.DONE
        can_rate, _ = visits.rate_state(
            status=lane["status"],
            finished_at=completed.get(lane["id"]) or lane["start_at"],
            reviewed=lane["id"] in reviewed,
            now=now,
        )
        out[lane["id"]] = {
            "is_rated": done and lane["id"] in reviewed,
            # Only the booking's owner can rate it.
            "can_rate": can_rate and lane["customer_id"] == customer_id,
        }
    return out


def _fallback(bookings):
    """
    booking-api's database could not be read. A booking with a review still
    says so, and nothing offers "Rate" until it can be checked again.
    """
    ids = [visits.as_uuid(booking.get("id")) for booking in bookings]
    reviewed = set(
        Review.objects.filter(booking_id__in=[i for i in ids if i]).values_list("booking_id", flat=True)
    )
    for booking, booking_id in zip(bookings, ids):
        booking.update({"is_rated": booking_id in reviewed, "can_rate": False})


def _members(booking):
    """The member rows of a party's read, or []."""
    members = booking.get("members")
    if booking.get("booking_type") != "GROUP" or not isinstance(members, list):
        return []
    return [member for member in members if isinstance(member, dict)]


def _no():
    return {"is_rated": False, "can_rate": False}

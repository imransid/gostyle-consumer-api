"""
The visit a review is about, read from booking-api's database.

booking-api owns the bookings. Like the map's "who is busy", this reads its
tables directly (DATABASES["booking"], read only), because a review needs
what booking-api's HTTP read does not give: which stylist did which line,
when each line started, and when the visit was completed.

A booking line's staff_id is who did it: booking-api writes the new stylist
there when one is changed (a move, a swap at the desk), and fills it in at
create time for "any available expert". Lines are never cancelled one by
one, so every line of a completed booking was done.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import DatabaseError
from django.db.models import Min, Q

from apps.booking_data.models import Booking, BookingItem, BookingStatusHistory, GroupParticipant

DB = "booking"

# booking-api's words for a visit that is done. "settled" is a completed
# visit that has been paid for; the app sees both as COMPLETED.
DONE = ("completed", "settled")

# The app's word for each of booking-api's statuses, as booking-api itself
# maps them (its domain/booking/mobile-contract.ts), in lower case, as the
# Rate your visit doc writes "completed".
APP_STATUS = {
    "draft": "booked",
    "held": "booked",
    "pending_payment": "booked",
    "pending_confirmation": "booked",
    "confirmed": "confirmed_by_salon",
    "checked_in": "checked_in",
    "in_service": "checked_in",
    "completed": "completed",
    "settled": "completed",
    "cancelled": "cancelled",
    "no_show": "cancelled",
    "rescheduled": "cancelled",
    "expired": "cancelled",
    "skipped": "cancelled",
}

# How long after the visit it can still be rated (the doc's suggestion).
REVIEW_WINDOW = timedelta(days=14)


class BookingDataUnavailable(Exception):
    """booking-api's database is not set up here (BOOKING_DB_NAME), or the read failed."""


@dataclass(frozen=True)
class Line:
    service_id: str
    service_name: str
    staff_id: uuid.UUID
    position: int
    # When the line's stylist started on it. None if booking-api kept no reservation.
    started_at: datetime | None


@dataclass(frozen=True)
class Visit:
    booking_id: uuid.UUID
    customer_id: uuid.UUID
    status: str
    branch_id: uuid.UUID
    tenant_id: uuid.UUID | None
    start_at: datetime
    completed_at: datetime | None
    lines: tuple

    @property
    def app_status(self):
        return APP_STATUS.get(self.status, self.status)

    @property
    def finished_at(self):
        # The doc: "If you don't keep a completion time, the visit's start time is fine."
        return self.completed_at or self.start_at


def rate_state(*, status, finished_at, reviewed, now):
    """
    (can_rate, reason) for one booking. The one rule behind the review form
    and every booking's `can_rate`, so the two always agree.
    """
    if reviewed:
        return False, "already_reviewed"
    if status not in DONE:
        return False, "not_completed"
    if now >= finished_at + REVIEW_WINDOW:
        return False, "window_closed"
    return True, None


def find_visit(booking_ref, customer_id):
    """
    The caller's own booking for the id the app has, or None.

    The id is the one the app uses for /booking/:id: a booking's id, or a
    party's id. A party's id means the caller's own lane in that party: each
    member's lane is their own booking, and booking-api lets an account into
    a party only once. Someone else's booking is None too, so the caller gets
    the same 404 as for an id that does not exist.
    """
    ref = as_uuid(booking_ref)
    if ref is None:
        return None
    _check_db()
    try:
        booking = Booking.objects.using(DB).filter(id=ref).first()
        if booking is None:
            booking = Booking.objects.using(DB).filter(group_id=ref, customer_id=customer_id).first()
        if booking is None or booking.customer_id != customer_id:
            return None

        items = (
            BookingItem.objects.using(DB)
            .filter(booking_id=booking.id)
            .annotate(started_at=Min("staffreservation__start_at"))
            .order_by("position")
        )
        lines = tuple(
            Line(
                service_id=str(item.service_id),
                service_name=item.service_name,
                staff_id=item.staff_id,
                position=item.position,
                started_at=item.started_at,
            )
            for item in items
        )
        completed_at = completion_times([booking.id]).get(booking.id)
    except DatabaseError as exc:
        raise BookingDataUnavailable(str(exc)) from exc

    return Visit(
        booking_id=booking.id,
        customer_id=booking.customer_id,
        status=booking.status,
        branch_id=booking.branch_id,
        tenant_id=as_uuid(booking.tenant_id),
        start_at=booking.start_at,
        completed_at=completed_at,
        lines=lines,
    )


def lanes_for(refs, customer_id):
    """
    For the ids of the bookings on a screen: ref -> the booking row it stands
    for ({id, customer_id, status, start_at}). A booking's id is that booking.
    A party's id is the caller's own lane in it. Anything else (a routine's
    id, an id that is not a uuid) is left out.
    """
    wanted = {ref: as_uuid(ref) for ref in refs}
    ids = {value for value in wanted.values() if value is not None}
    if not ids:
        return {}
    _check_db()
    try:
        rows = list(
            Booking.objects.using(DB)
            .filter(Q(id__in=ids) | Q(group_id__in=ids, customer_id=customer_id))
            .values("id", "group_id", "customer_id", "status", "start_at")
        )
    except DatabaseError as exc:
        raise BookingDataUnavailable(str(exc)) from exc

    by_id = {row["id"]: row for row in rows}
    by_party = {row["group_id"]: row for row in rows if row["customer_id"] == customer_id and row["group_id"]}
    found = {}
    for ref, value in wanted.items():
        row = by_id.get(value) or by_party.get(value)
        if row is not None:
            found[ref] = row
    return found


def party_lanes(group_ref):
    """participant id (str) -> that member's lane booking row, for one party's read."""
    group_id = as_uuid(group_ref)
    if group_id is None:
        return {}
    _check_db()
    try:
        members = dict(
            GroupParticipant.objects.using(DB)
            .filter(group_id=group_id, booking_id__isnull=False)
            .values_list("id", "booking_id")
        )
        rows = (
            Booking.objects.using(DB)
            .filter(id__in=members.values())
            .values("id", "group_id", "customer_id", "status", "start_at")
        )
        lanes = {row["id"]: row for row in rows}
    except DatabaseError as exc:
        raise BookingDataUnavailable(str(exc)) from exc
    return {str(member): lanes[lane] for member, lane in members.items() if lane in lanes}


def completion_times(booking_ids):
    """booking id -> when it was first completed, from booking-api's status history."""
    if not booking_ids:
        return {}
    rows = (
        BookingStatusHistory.objects.using(DB)
        .filter(booking_id__in=booking_ids, to_status__in=DONE)
        .values("booking_id")
        .annotate(at=Min("created_at"))
        .values_list("booking_id", "at")
    )
    try:
        return dict(rows)
    except DatabaseError as exc:
        raise BookingDataUnavailable(str(exc)) from exc


def _check_db():
    if DB not in settings.DATABASES:
        raise BookingDataUnavailable("BOOKING_DB_NAME is not set: the visit cannot be read.")


def as_uuid(value):
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None

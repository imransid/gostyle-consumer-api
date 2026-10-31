"""
The words of the notifications other services hand over (gRPC Notify).

booking-api sends facts, not sentences: a booking's code, its start and its
branch. The words live here, once, so every channel says the same thing,
the salon is called what its storefront calls it, and the time is on the
salon's own clock, not on booking-api's.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from django.db import DatabaseError

from apps.salons import timezones
from apps.salons.selectors import salon_cards_for_refs

logger = logging.getLogger(__name__)


class CannotWrite(ValueError):
    """No words for this kind, or the params lack what they need."""


@dataclass(frozen=True)
class Words:
    title: str
    body: str
    # Kept on the item and sent in the push's data.
    data: dict


def words_for(type_, params):
    writer = _WRITERS.get(type_)
    if writer is None:
        raise CannotWrite(f"no words for {type_!r}")
    return writer(params)


def _salon(branch_id):
    """The salon's card (name, timezone, storefront id), or None. A lookup
    that fails costs the salon's name in the words, never the notification."""
    if not branch_id:
        return None
    try:
        return salon_cards_for_refs([branch_id]).get(branch_id)
    except DatabaseError as e:
        logger.warning("notification words: salon %s not looked up: %s", branch_id, e)
        return None


def _when(start_at, card):
    """'Mon 2 Nov at 11:00 AM', on the salon's clock."""
    try:
        start = datetime.fromisoformat(start_at)
    except (TypeError, ValueError):
        raise CannotWrite(f"start_at {start_at!r} is not a time") from None
    if start.tzinfo is None:
        raise CannotWrite(f"start_at {start_at!r} has no offset")
    zone = timezones.resolve(card["timezone"] if card else None, card["id"] if card else None)
    local = start.astimezone(zone)
    hour = local.hour % 12 or 12
    half = "AM" if local.hour < 12 else "PM"
    return f"{local:%a} {local.day} {local:%b} at {hour}:{local:%M} {half}"


def _booking(params):
    code = (params.get("booking_code") or "").strip()
    if not code:
        raise CannotWrite("booking_code is required")
    card = _salon(params.get("branch_id"))
    start_at = params.get("start_at") or ""
    when = _when(start_at, card) if start_at else None
    data = {
        "bookingId": params.get("booking_id"),
        "bookingCode": code,
        "startAt": start_at or None,
        "salonId": card["id"] if card else None,
    }
    at = f" at {card['name']}" if card and card.get("name") else ""
    return code, at, when, data


def _confirmed(params):
    code, at, when, data = _booking(params)
    on = f" on {when}" if when else ""
    return Words("Booking confirmed", f"Your booking {code}{at}{on} is confirmed.", data)


def _rescheduled(params):
    code, at, when, data = _booking(params)
    new_time = f": {when}" if when else ""
    return Words("Booking moved", f"Your booking {code}{at} has a new time{new_time}.", data)


def _cancelled(params):
    code, at, when, data = _booking(params)
    on = f" on {when}" if when else ""
    return Words("Booking cancelled", f"Your booking {code}{at}{on} was cancelled.", data)


_WRITERS = {
    "booking.confirmed": _confirmed,
    "booking.rescheduled": _rescheduled,
    "booking.cancelled": _cancelled,
}

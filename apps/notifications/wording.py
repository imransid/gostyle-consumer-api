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
from django.utils import timezone

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


def _zone(card):
    return timezones.resolve(card["timezone"] if card else None, card["id"] if card else None)


def _local_start(start_at, card):
    """The start on the salon's clock."""
    try:
        start = datetime.fromisoformat(start_at)
    except (TypeError, ValueError):
        raise CannotWrite(f"start_at {start_at!r} is not a time") from None
    if start.tzinfo is None:
        raise CannotWrite(f"start_at {start_at!r} has no offset")
    return start.astimezone(_zone(card))


def _clock(local):
    """'11:00 AM'."""
    hour = local.hour % 12 or 12
    half = "AM" if local.hour < 12 else "PM"
    return f"{hour}:{local:%M} {half}"


def _when(start_at, card):
    """'Mon 2 Nov at 11:00 AM', on the salon's clock."""
    local = _local_start(start_at, card)
    return f"{local:%a} {local.day} {local:%b} at {_clock(local)}"


def _soon(start_at, card, now=None):
    """'today at 11:00 AM', 'tomorrow at 9:30 AM', or 'on Mon 2 Nov at
    11:00 AM': the day as the salon's clock has it, from now."""
    local = _local_start(start_at, card)
    today = (now or timezone.now()).astimezone(local.tzinfo).date()
    if local.date() == today:
        return f"today at {_clock(local)}"
    if (local.date() - today).days == 1:
        return f"tomorrow at {_clock(local)}"
    return f"on {_when(start_at, card)}"


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
    return code, at, when, data, card


def _confirmed(params):
    code, at, when, data, _card = _booking(params)
    on = f" on {when}" if when else ""
    # One item for a whole routine or party (booking-api keys it so).
    if params.get("series_id"):
        visits = params.get("visits") or ""
        many = f"{visits} visits, " if visits.isdigit() and int(visits) > 1 else ""
        starting = f"starting {when}" if when else "starting soon"
        data["seriesId"] = params["series_id"]
        return Words(
            "Routine confirmed", f"Your routine{at} is confirmed: {many}{starting}.", data
        )
    if params.get("group_id"):
        data["groupId"] = params["group_id"]
        return Words("Booking confirmed", f"Your group booking{at}{on} is confirmed.", data)
    return Words("Booking confirmed", f"Your booking {code}{at}{on} is confirmed.", data)


def _rescheduled(params):
    code, at, when, data, _card = _booking(params)
    new_time = f": {when}" if when else ""
    return Words("Booking moved", f"Your booking {code}{at} has a new time{new_time}.", data)


def _cancelled(params):
    code, at, when, data, _card = _booking(params)
    on = f" on {when}" if when else ""
    return Words("Booking cancelled", f"Your booking {code}{at}{on} was cancelled.", data)


def _reminder(params):
    code, at, _when_text, data, card = _booking(params)
    start_at = params.get("start_at") or ""
    if not start_at:
        raise CannotWrite("start_at is required for a reminder")
    return Words(
        "Appointment reminder",
        f"Your booking {code}{at} is {_soon(start_at, card)}.",
        data,
    )


def _check_in(params):
    """A desk's answer to "I am here". booking-api writes these words itself
    (they can name a member of the customer's party, whom only it can
    find), so they arrive as `title` and `body`."""
    title = (params.get("title") or "").strip()
    body = (params.get("body") or "").strip()
    if not title or not body:
        raise CannotWrite("a check-in answer needs its title and body")
    code, _at, _when, data, _card = _booking(params)
    # The keys the check-in push carried before the center, which the app
    # in customers' hands reads.
    data.update(
        {"code": code, "requestId": params.get("request_id"), "groupId": params.get("group_id")}
    )
    return Words(title, body, data)


_WRITERS = {
    "check_in.approved": _check_in,
    "check_in.rejected": _check_in,
    "check_in.expired": _check_in,
    "booking.confirmed": _confirmed,
    "booking.rescheduled": _rescheduled,
    "booking.cancelled": _cancelled,
    "booking.reminder": _reminder,
}

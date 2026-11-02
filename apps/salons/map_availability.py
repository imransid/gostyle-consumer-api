"""
The map's availability filter (MAP_FILTER_V1): which salons still have a
start a customer could book today, tomorrow, or in the next seven days
(today included), on the salon's own clock.

THE SLOT PICKER'S RULES, NOT NEW ONES. A salon has a start on a day when GET
/booking/nearest-available would offer one there for one of its services
(views._offers): the salon open that day (its published hours, not shut by
hand), a stylist rostered at this branch who can do the service alone (the
skills), inside booking-api's trading day, outside the unpaid break and the
bookings they already hold, the service's minutes and its notice (lead
time), on the 15-minute grid.

ONE NUMBER PER DAY AND CATEGORY. For each day, and each of the drawer's
service categories, the latest moment a customer can still book one of its
services: "book by", the last free start minus the service's notice. A start
at or after now exists exactly when book-by is at or after now, so the number
does not go stale as the clock moves (no refresh "as slots pass"): only a new
booking, a roster or an hours change moves it. That is what lets it be
cached for CACHE_SECONDS and read on every map move: the FE doc's small
table (5.2), kept in the cache.

WHO IS BUSY comes from booking-api's database, read-only (BOOKING_DB_NAME,
the "booking" alias the admin reads), by booking-api's own /busy rule
(BookingRepository.busyFor): a booking of this branch in a blocking status,
overlapping the window, on the stylist of each of its lines. /busy itself
needs a customer's token, and the map is public. Without that database the
filter has no honest answer: AvailabilityUnavailable, and the map answers 503,
never "everyone is free".
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError

from apps.booking_data.models import BookingItem

from . import slots, timezones
from .hours import weekly_row
from .selectors import (
    manual_states_between,
    menu_timing_rows,
    salon_categories,
    salon_stylists,
    shift_rows_between,
    stylist_service_coverage,
)

# booking-api's BLOCKING_STATES (src/domain/booking/lifecycle.ts), the
# statuses its /busy reads: these still hold the chair. Copied, because the
# map reads the database and not /busy. Keep the two the same.
BLOCKING_STATUSES = (
    "pending_payment",
    "pending_confirmation",
    "confirmed",
    "checked_in",
    "in_service",
    "completed",
    "settled",
)

# booking-api's trading day (src/domain/availability/grid.ts, DAY_START_MIN
# and DAY_END_MIN): it books 10:00 to 22:00 and nothing outside it, and /busy
# sends it as `day`, which the slot picker clamps every shift to. Copied for
# the same reason as BLOCKING_STATUSES.
ENGINE_DAY_MINUTES = (600, 1320)

# The days looked at: today and the six after it (the FE doc's "week").
DAYS = 7

# Each pick's days, as (first, last + 1) offsets from the salon's today.
WINDOWS = {"today": (0, 1), "tomorrow": (1, 2), "week": (0, DAYS)}

# How long one salon's numbers are kept. A booking made meanwhile shows
# within this long (the FE doc, 5.3: about a minute).
CACHE_SECONDS = 60
CACHE_PREFIX = "map-availability:v1"

# The key of "any service at the salon", beside the category ids.
ANY_SERVICE = "*"


class AvailabilityUnavailable(Exception):
    """Who is busy cannot be read: no booking database, or it failed."""


def available_ids(salons, window, option_ids, now):
    """
    The ids of `salons` with a start left in `window` ("today", "tomorrow"
    or "week"), for any service, or for a service in one of `option_ids`
    (the drawer's category ids) when that is not None.

    `salons` are Storefront rows with `branch_timezone` and `published_hours`
    (map_filters.filtered_map_venues reads them). `now` is aware.
    """
    first, last = WINDOWS[window]
    keys = (ANY_SERVICE,) if option_ids is None else tuple(option_ids)
    moment = now.timestamp()

    plans, todo = {}, []
    for salon in salons:
        tz = timezones.resolve(salon.branch_timezone, salon.pk)
        today = now.astimezone(tz).date()
        plan = cache.get(_cache_key(salon.pk, today))
        if plan is None:
            todo.append((salon, tz, today))
        else:
            plans[salon.pk] = (plan, today)

    for salon, today, plan in _plans(todo):
        cache.set(_cache_key(salon.pk, today), plan, CACHE_SECONDS)
        plans[salon.pk] = (plan, today)

    found = set()
    for salon_id, (plan, today) in plans.items():
        for offset in range(first, last):
            book_by = plan.get((today + timedelta(days=offset)).isoformat(), {})
            if any(book_by.get(key, float("-inf")) >= moment for key in keys):
                found.add(salon_id)
                break
    return found


def _cache_key(salon_id, today):
    return f"{CACHE_PREFIX}:{salon_id}:{today.isoformat()}"


@dataclass
class _Salon:
    """What one salon's numbers are worked out from, read once."""

    salon: object
    tz: object
    today: object
    # date -> (day_start, the salon's open span that day)
    open_days: dict = field(default_factory=dict)
    services: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)
    shifts: dict = field(default_factory=dict)
    option_of: dict = field(default_factory=dict)


def _plans(todo):
    """(salon, today, plan) for each of `todo`, with ONE read of who is busy for all of them."""
    if not todo:
        return []
    inputs = [_read(salon, tz, today) for salon, tz, today in todo]
    busy = _busy(inputs)
    return [(entry.salon, entry.today, _plan(entry, busy)) for entry in inputs]


def open_span(weekly, manual_state, day, day_start):
    """
    The hours the salon itself is open on `day`, or None when it is shut:
    views._open_span's rule, from rows already read. Shut by hand (CLOSED)
    or closed in the published weekly grid, it is shut.
    """
    if manual_state == "CLOSED":
        return None
    row = weekly_row(weekly, day.weekday())
    if not row or row.get("closed"):
        return None
    return slots.span(day_start, row.get("open"), row.get("close"))


def _read(salon, tz, today):
    """
    One salon's inputs, read lazily: a salon shut all week costs one query,
    and one with nobody to book costs two.
    """
    entry = _Salon(salon=salon, tz=tz, today=today)
    last_day = today + timedelta(days=DAYS - 1)

    hours = salon.published_hours if isinstance(salon.published_hours, dict) else {}
    manual = manual_states_between(salon.pk, today, last_day)
    for offset in range(DAYS):
        day = today + timedelta(days=offset)
        day_start = datetime.combine(day, time.min, tzinfo=tz)
        span = open_span(hours.get("weekly"), manual.get(day), day, day_start)
        if span is not None:
            entry.open_days[day] = (day_start, span)
    if not entry.open_days:
        return entry

    staff_ids = list(salon_stylists(salon).values_list("id", flat=True))
    if not staff_ids:
        return entry

    entry.services = {row["id"]: row for row in menu_timing_rows(salon)}
    if not entry.services:
        return entry

    entry.coverage = stylist_service_coverage(salon, list(entry.services), staff_ids)
    if not entry.coverage:
        return entry

    entry.shifts = {
        (row["staff_member_id"], row["shift_date"]): row
        for row in shift_rows_between(
            salon.tenant_id, salon.branch_id, list(entry.coverage),
            min(entry.open_days), max(entry.open_days),
        )
    }

    from .map_filters import service_option_id

    categories = salon_categories(salon.tenant_id)
    entry.option_of = {
        service_id: service_option_id(categories, row["category_0_id"])
        for service_id, row in entry.services.items()
    }
    return entry


def _window(entry):
    """From the salon's midnight today to the end of the day after the last: an overnight tail included."""
    start = datetime.combine(entry.today, time.min, tzinfo=entry.tz)
    return start, start + timedelta(days=DAYS + 1)


def _busy(inputs):
    """
    {(branch_id, staff_id): [(start, end), ...]} for every salon that has
    someone to book, in one read of booking-api's database.
    """
    wanted = [entry for entry in inputs if entry.coverage]
    if not wanted:
        return {}
    if "booking" not in settings.DATABASES:
        raise AvailabilityUnavailable("BOOKING_DB_NAME is not set: who is busy cannot be read.")

    windows = [_window(entry) for entry in wanted]
    rows = BookingItem.objects.using("booking").filter(
        staff_id__in={staff_id for entry in wanted for staff_id in entry.coverage},
        booking__branch_id__in={entry.salon.branch_id for entry in wanted},
        booking__status__in=BLOCKING_STATUSES,
        booking__start_at__lt=max(end for _, end in windows),
        booking__end_at__gt=min(start for start, _ in windows),
    ).values_list("booking__branch_id", "staff_id", "booking__start_at", "booking__end_at")

    try:
        rows = list(rows)
    except DatabaseError as exc:
        raise AvailabilityUnavailable(f"booking-api's database failed: {exc}") from exc

    busy = {}
    for branch_id, staff_id, start_at, end_at in rows:
        busy.setdefault((branch_id, staff_id), []).append((_aware(start_at), _aware(end_at)))
    return busy


def _aware(moment):
    """booking-api's columns are timestamptz; a naive one is read as UTC, never as local time."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt_timezone.utc)


def _plan(entry, busy):
    """
    {day: {key: book-by}} for one salon: per day, per drawer category id and
    for ANY_SERVICE, the latest moment (a timestamp) a customer can still
    book a start there. A day with nothing left is not in it.
    """
    # The slot picker's minutes for a service (SLOT_MINUTES_AS_BOOKED_V1),
    # read from views, not copied. Imported here: views imports this module.
    from .views import service_minutes

    plan = {}
    for day, (day_start, opening) in entry.open_days.items():
        engine = (
            day_start + timedelta(minutes=ENGINE_DAY_MINUTES[0]),
            day_start + timedelta(minutes=ENGINE_DAY_MINUTES[1]),
        )
        day_end = day_start + timedelta(days=1)
        best = {}
        for staff_id, covered in entry.coverage.items():
            shift = entry.shifts.get((staff_id, day))
            if shift is None:
                continue  # not rostered here that day

            working = slots.span(day_start, shift["start_time"], shift["end_time"])
            if working is None:
                continue

            # The salon's hours and the engine's day bound the shift, as in
            # views._offers.
            on_duty = (
                max(working[0], opening[0], engine[0]),
                min(working[1], opening[1], engine[1]),
            )
            if on_duty[0] >= on_duty[1]:
                continue

            blocks = [
                (start.astimezone(entry.tz), end.astimezone(entry.tz))
                for start, end in busy.get((entry.salon.branch_id, staff_id), ())
            ]
            unpaid_break = slots.break_span(day_start, shift["break_time"])
            if unpaid_break is not None:
                blocks.append(unpaid_break)

            free = slots.subtract([on_duty], blocks)
            for service_id in covered:
                row = entry.services[service_id]
                last = slots.latest_start(
                    free, service_minutes(row), day_start, day_end, day_start, day_start
                )
                if last is None:
                    continue
                book_by = (last - timedelta(minutes=row["lead_time_minutes"] or 0)).timestamp()
                for key in (ANY_SERVICE, entry.option_of.get(service_id)):
                    if key and book_by > best.get(key, float("-inf")):
                        best[key] = book_by
        if best:
            plan[day.isoformat()] = best
    return plan

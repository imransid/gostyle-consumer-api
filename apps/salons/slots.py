"""
When can this appointment actually start?

Interval arithmetic over a single salon-local day. Four things decide whether
a minute is bookable — the salon's opening hours, the stylist's shift, their
unpaid break, and the appointments they already hold — and they all reduce to
the same operation: take a list of open intervals, subtract a list of busy
ones, then walk what is left on the slot grid.

Everything here is pure and works in aware datetimes. No queryset, no model,
no clock: `now` arrives as an argument, because a function that reads the
clock itself cannot be tested at 23:59.
"""

from datetime import timedelta

from .hours import minutes_of_day

# The booking grid. Every start sits on it, counted from salon-local midnight,
# so a 15 puts starts on :00, :15, :30 and :45 whatever time the salon opens.
#
# NOT a platform column: nothing in the schema stores a per-salon grain. 15
# mirrors gostyle-booking-api's ONLINE channel (`channels.ONLINE.grainMin`),
# which is the only configured answer anywhere in the stack. The moment the
# platform grows a column, this constant is the one place to replace.
SLOT_GRID_MINUTES = 15

# How long an appointment is assumed to be when the caller names no services.
# Also not a platform column. A customer who has not picked a service yet is
# browsing, so this decides how densely the day is drawn, not what gets
# booked — `duration_min` reports it back so the app never has to guess which
# number was used.
DEFAULT_APPOINTMENT_MINUTES = 30

# An unpaid break is one hour starting at `shift.break_time`; the platform
# stores only the start and fixes the length in its own schema comment
# ("unpaid 1h break start, or null").
BREAK_MINUTES = 60


def span(day_start, open_hhmm, close_hhmm):
    """
    One "HH:MM"-"HH:MM" pair as a real interval on one salon-local day.

    Reads opening hours out of the snapshot and working hours out of a shift
    row, which store the same shape for the same reason.

    A close at or before the open is an OVERNIGHT window — 18:00 to 02:00 is
    eight hours, not minus sixteen — so it runs into the following day. None
    when either side is missing or malformed, which a caller treats as "no
    hours known", never as "open all day".
    """
    opens, closes = minutes_of_day(open_hhmm), minutes_of_day(close_hhmm)
    if opens is None or closes is None:
        return None
    if closes <= opens:
        closes += 24 * 60
    return (day_start + timedelta(minutes=opens), day_start + timedelta(minutes=closes))


def break_span(day_start, hhmm, minutes=BREAK_MINUTES):
    """
    The unpaid break as an interval, from its start time and a fixed length.

    The platform stores only when the break begins; its length is a constant
    in the schema's own words rather than a column, so it is a constant here
    too. None when the shift has no break, which is most of them.
    """
    starts_at = minutes_of_day(hhmm)
    if starts_at is None:
        return None
    begins = day_start + timedelta(minutes=starts_at)
    return (begins, begins + timedelta(minutes=minutes))


def merge(intervals):
    """Overlapping or touching (start, end) pairs, folded into the fewest."""
    ordered = sorted((i for i in intervals if i[0] < i[1]), key=lambda i: i[0])
    merged = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract(intervals, blocks):
    """
    What is left of `intervals` once every block is cut out of it.

    One block can split one interval in two — a 10:00–18:00 shift with a
    13:00 lunch is two free stretches, and an appointment may not straddle the
    gap. That split is the whole reason this is interval arithmetic rather
    than a pair of times.
    """
    free = merge(intervals)
    for block_start, block_end in merge(blocks):
        remaining = []
        for start, end in free:
            if block_end <= start or block_start >= end:
                remaining.append((start, end))       # no overlap
                continue
            if start < block_start:
                remaining.append((start, block_start))   # piece before
            if block_end < end:
                remaining.append((block_end, end))       # piece after
        free = remaining
    return free


def align_up(moment, day_start, grid_minutes=SLOT_GRID_MINUTES):
    """
    The first grid point at or after `moment`, counted from `day_start`.

    Anchored on the salon's own midnight rather than the hour, so the grid
    does not shift when a salon opens at 09:30 — and does not drift by an hour
    when the window is expressed in someone else's offset.
    """
    grid = grid_minutes * 60
    elapsed = (moment - day_start).total_seconds()
    steps = -((-elapsed) // grid)  # ceiling division, negatives included
    return day_start + timedelta(seconds=steps * grid)


def starts(free, duration_minutes, window_start, window_end, earliest, day_start,
           grid_minutes=SLOT_GRID_MINUTES):
    """
    Every bookable start inside the window, soonest first.

    `free` is what `subtract` left. `earliest` is now plus any lead time —
    a start before it is already gone. `window_end` bounds the START only: a
    45-minute service starting at 11:45 in a 10:00–12:00 window is a real
    offer as long as the stylist is free until 12:30, which is the rule the
    app relies on to show the last slot before noon.
    """
    duration = timedelta(minutes=duration_minutes)
    found = []
    for interval_start, interval_end in free:
        moment = align_up(
            max(interval_start, window_start, earliest), day_start, grid_minutes
        )
        while moment < window_end and moment + duration <= interval_end:
            found.append(moment)
            moment += timedelta(minutes=grid_minutes)
    return sorted(found)

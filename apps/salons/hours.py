# apps/salons/hours.py
"""
Opening hours: the weekly grid, today's exception, and the manual state.

Ported from gostyle-platform's opening-hours.ts and daily-status-rules.ts so
Django and the salon's own console give the SAME answer for the same salon.
A customer seeing "Open" while the console says "Closed" is a support ticket.

PURE. No Django, no database, no clock. Every function takes the local time it
should reason about rather than asking what time it is. The messy part (which
timezone, and what date is it there) happens once, at the edge, in views.

The two overrides stack and they are NOT the same kind of thing:
  - an EXCEPTION changes the HOURS for one dated day. Open/closed is still
    computed from it, exactly as it would be from the grid.
  - a MANUAL STATE replaces the ANSWER outright. "Busy" is not a time and no
    grid can produce it.
So the exception feeds the calculation, and the state overrules the result.
"""

# Monday first, matching the platform's HOURS section DAYS order.
# Python's weekday() also returns Monday as 0, so the two line up by index.
DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Mirrors the platform's StorefrontStatusState enum.
STATES = ("OPEN", "BUSY", "WALK_INS", "SPECIAL_HOURS", "CLOSED")

# Which states mean "a customer can come in right now".
OPEN_STATES = ("OPEN", "BUSY", "WALK_INS")


def _minutes(hhmm):
    """'14:30' -> 870. None for anything malformed."""
    if not isinstance(hhmm, str) or len(hhmm) != 5 or hhmm[2] != ":":
        return None
    try:
        h, m = int(hhmm[:2]), int(hhmm[3:])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def _pretty(hhmm):
    """'22:00' -> '10:00 PM'. None stays None."""
    total = _minutes(hhmm)
    if total is None:
        return None
    h, m = divmod(total, 60)
    suffix = "PM" if h >= 12 else "AM"
    return f"{h % 12 or 12}:{m:02d} {suffix}"


def weekly_row(weekly, weekday_index):
    """
    Pick today's row out of the weekly grid.

    `weekly` is snapshot HOURS.weekly, a list of
    {day, closed, open, close}. Defensive: it is JSONB and old by definition.
    """
    if not isinstance(weekly, list):
        return None
    wanted = DAY_KEYS[weekday_index]
    for row in weekly:
        if isinstance(row, dict) and row.get("day") == wanted:
            return row
    return None


def is_within(open_hhmm, close_hhmm, now_hhmm):
    """
    Is now inside the window? Handles OVERNIGHT windows.

    The old code compared strings directly, so a salon open 18:00 to 02:00
    reported closed all evening: "18:00" <= "20:00" < "02:00" is False.
    Comparing minutes and detecting the wrap fixes it.
    """
    start, end, now = _minutes(open_hhmm), _minutes(close_hhmm), _minutes(now_hhmm)
    if start is None or end is None or now is None:
        return None

    if end > start:              # normal day, 09:00 to 22:00
        return start <= now < end
    if end < start:              # overnight, 18:00 to 02:00
        return now >= start or now < end
    return False                 # start == end, a zero-length window


def resolve(weekly, exception, state, weekday_index, now_hhmm):
    """
    The one function that answers the whole question.

    weekly        snapshot HOURS.weekly, or None
    exception     today's storefront_status_exception as a dict, or None
    state         today's manual state string, or None
    weekday_index Python weekday(), Monday = 0
    now_hhmm      current local time as "HH:MM"

    Returns a dict: is_open, status, hours_today, closes_at.
    is_open is None when nothing is known, so the caller can hide the row
    rather than claiming a salon is shut.
    """
    # Step 1: which hours apply today. An exception REPLACES the weekly row.
    if isinstance(exception, dict):
        if exception.get("closed"):
            row = {"closed": True}
        else:
            row = {
                "closed": False,
                "open": exception.get("open"),
                "close": exception.get("close"),
            }
    else:
        row = weekly_row(weekly, weekday_index)

    # Step 2: compute open/closed from those hours.
    if row is None:
        is_open, hours_today, closes_at = None, None, None
    elif row.get("closed"):
        is_open, hours_today, closes_at = False, "Closed", None
    else:
        opens, closes = row.get("open"), row.get("close")
        is_open = is_within(opens, closes, now_hhmm)
        o, c = _pretty(opens), _pretty(closes)
        hours_today = f"{o} - {c}" if o and c else None
        if is_open is True:
            closes_at = f"Closes at {c}" if c else None
        elif is_open is False:
            closes_at = f"Opens at {o}" if o else None
        else:
            closes_at = None

    # Step 3: a manual state overrules the computed answer.
    status = state if state in STATES else ("OPEN" if is_open else "CLOSED" if is_open is False else None)
    if state in STATES:
        is_open = state in OPEN_STATES
        if state == "CLOSED":
            hours_today, closes_at = "Closed", None

    return {
        "is_open": is_open,
        "status": status,
        "hours_today": hours_today,
        "closes_at": closes_at,
    }
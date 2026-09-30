"""
A stylist's steady day off, worked out from the roster.

The platform stores no "day off". It stores dated shifts, inside a branch's
weekly roster (a roster week starts on Monday). So a day off is read from
what is NOT there (Rafa, Q7, docs/EXPERT_PROFILE_AUDIT.md):

    a weekday the salon is OPEN,
    with no shift at this salon's branch in ANY rostered week
    of the 4 roster weeks that end with the current week,
    with at least 2 rostered weeks to judge from.

A rostered week is a week with at least one shift. Anything less certain is
no answer (null): too little roster, a day missed in one week only, or a
salon that published no hours, where a closed day cannot be told from a day
off.

It is display text, the same on the Stylists tab, the Expert step, the
service detail's experts and the expert profile. It agrees with the Time
step, which offers nothing on a day with no shift.

Pure: rows in, text out. The reads are `selectors.roster_shift_days` and the
salon's published snapshot (`views.days_off_for`).
"""

from datetime import timedelta

from .hours import weekly_row

# How many roster weeks are looked at, ending with the current one.
WEEKS = 4

# Fewer rostered weeks than this is too little to call a day "steady".
MIN_ROSTERED_WEEKS = 2

# Monday first: date.weekday() order, and the order the text is written in.
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def week_window(today):
    """
    `(first, last)`: the Mondays of the first and the last of the WEEKS
    roster weeks that end with the week `today` is in.

    `today` is the salon's own date. The current week counts whole: a roster
    is planned ahead, so its later days are already there.
    """
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(weeks=WEEKS - 1), this_monday


def open_weekdays(weekly):
    """
    The weekdays the salon opens (0 = Monday), from its published weekly
    hours (snapshot HOURS.weekly). Empty when it published none.

    A day is open when it has a row that is not marked closed: the same test
    the Time step uses before it offers a start.
    """
    return frozenset(
        index for index in range(len(DAY_NAMES))
        if (row := weekly_row(weekly, index)) and not row.get("closed")
    )


def day_off(shifts, open_days):
    """
    One stylist's steady day off as text ("Tuesday", "Friday, Saturday"), or
    None.

    `shifts` is that stylist's shifts at this salon's branch inside the week
    window: rows with `week_start` (the roster week's Monday) and
    `shift_date`. `open_days` is `open_weekdays` of the salon.
    """
    weeks = {row["week_start"] for row in shifts}
    if len(weeks) < MIN_ROSTERED_WEEKS:
        return None

    worked = {row["shift_date"].weekday() for row in shifts}
    off = [day for day in range(len(DAY_NAMES)) if day in open_days and day not in worked]
    return ", ".join(DAY_NAMES[day] for day in off) or None


def days_off(shift_rows, open_days):
    """
    staff id to `day_off`, for every stylist with a shift in `shift_rows`
    (`selectors.roster_shift_days`). A stylist with no shift at all is not in
    the answer: the caller reads that as None.
    """
    by_staff = {}
    for row in shift_rows:
        by_staff.setdefault(row["staff_member_id"], []).append(row)
    return {staff_id: day_off(rows, open_days) for staff_id, rows in by_staff.items()}

"""
Which timezone is a salon in?

branch.timezone is NULLABLE, and the two places that needed it both did the
same thing with that fact:

    ZoneInfo(salon.branch_timezone or "Asia/Dubai")

which is a guess wearing the clothes of an answer. A branch in Glasgow with no
timezone set gets judged on Dubai's clock, four hours out, and says so
nowhere — the card reports "Open" at 6am and "Closed" at 8pm, and nothing
connects that to a null column.

THIS MODULE DOES NOT FIX THAT. The fallback is deliberately identical:
removing it would 500 the discovery list for every branch with a null
timezone, which today is most of them. What it adds is a WARNING naming the
salon, so the size and shape of the problem becomes visible in Loki before
anyone decides what the right answer is. `manage.py branch_timezones` reports
the same gap from the database side, for the branches nobody has looked at
yet.
"""

import logging
import zoneinfo

logger = logging.getLogger(__name__)

# The timezone assumed when a branch has none. GoStyle launched in the UAE and
# every salon on the platform was in it, which is how this became a default
# rather than a question. It stops being true the first time a branch is
# onboarded outside the Gulf, and nothing in the schema will announce that day.
DEFAULT_TIMEZONE = "Asia/Dubai"


def resolve(name, salon_id=None):
    """
    A ZoneInfo for a branch's timezone, falling back to DEFAULT_TIMEZONE.

    `name` is branch.timezone, which may be None or "". BOTH fall back, which
    is the same thing `or` did at the two call sites before this helper
    existed. The point here is the log line, not different behaviour.

    An unrecognised timezone string still raises ZoneInfoNotFoundError exactly
    as it did before. That is a BROKEN row rather than a missing one, and a
    branch whose timezone reads "GMT+4" wants fixing on the platform, not
    quietly rounding to Dubai here.
    """
    if name:
        return zoneinfo.ZoneInfo(name)

    logger.warning(
        "Branch timezone is not set for salon %s; assuming %s. "
        "Opening hours for this salon may be wrong.",
        salon_id if salon_id is not None else "<unknown>",
        DEFAULT_TIMEZONE,
        # Also as its own field: the JSON formatter emits anything from
        # `extra`, so Loki can count and group these rather than grep the
        # message body. See config/observability.py.
        extra={"salon_id": str(salon_id) if salon_id is not None else None},
    )
    return zoneinfo.ZoneInfo(DEFAULT_TIMEZONE)

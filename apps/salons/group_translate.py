"""
Group bookings: the mobile shape in, the engine's shape out, and back.

gostyle-booking-api plans and books a party (its `/v1/bookings/availability/
group`, `/v1/groups/holds` and `/v1/groups/:id/confirm`). It speaks its own
dialect: camelCase, `participants[]` with `serviceIds`, and time as a trading
day plus MINUTES FROM MIDNIGHT ON ITS OWN CLOCK. The app speaks snake_case,
`members[]` matched by `ref`, and ISO instants. Everything that turns one
into the other lives here.

PURE, like slots.py and hours.py: no network, no database, no clock. The
engine's clock arrives as an `EngineClock` read from its `/v1/bookings/
settings`, the salon's timezone arrives as a ZoneInfo, and "now" arrives as
an argument -- so every rule below is testable at 23:59 on any date.

THE ENGINE'S CLOCK IS NOT THE SALON'S. booking-api reads every branch on one
fixed UTC offset (today +06:00, Asia/Dhaka) while salons here default to
Asia/Dubai (+04:00). An instant survives the trip either way; a wall-clock
minute does not. So instants are converted to the engine's minutes on the way
out and back to instants on the way in, and never compared as "HH:MM" across
the two clocks. The offset is read from the engine, never copied: the day it
changes there, this follows.
"""

import re
from collections import namedtuple
from datetime import date, datetime, time, timedelta, timezone as dt_timezone

from . import slots

# The day view's grid. Half-hour starts, anchored on salon-local midnight.
GROUP_STEP_MINUTES = 30

# The most starts one engine call may ask about. booking-api's own cap
# (MAX_PARTY_STARTS in domain/availability/party-starts.ts): its whole trading
# day at half-hour steps. It refuses 25 with a 400, so a longer day is asked
# in more than one call.
MAX_GROUP_STARTS = 24

# The app's three sections of the day, by the salon-local minute a slot
# starts at: [from, to).
BANDS = (
    ("Morning", 0, 12 * 60),
    ("Afternoon", 12 * 60, 17 * 60),
    ("Evening", 17 * 60, None),
)

_HHMM = re.compile(r"^(\d{2}):(\d{2})$")


EngineClock = namedtuple("EngineClock", ["offset_min", "from_min", "to_min"])
EngineClock.__doc__ = """
booking-api's clock and trading day, as its `/v1/bookings/settings` publishes
them: `branch.utcOffsetMinutes`, and `tradingWindow.fromMin`/`toMin` -- the
minutes of ITS day that it will plan at all (600 and 1320 today).
"""


# ------------------------------------------------------------ the two clocks


def engine_zone(clock):
    """The engine's fixed offset, as a tzinfo."""
    return dt_timezone(timedelta(minutes=clock.offset_min))


def to_engine(moment, clock):
    """
    An aware instant as the engine names it: (trading day, minute of day).

    Example, engine at +06:00: 15:00+04:00 is 11:00Z is 17:00 on the engine's
    clock -- minute 1020. The engine's single mobile booking converts
    `start_time` exactly this way (mobile-contract.ts, toBranchMoment), so a
    group and a single booking at the same instant land on the same minute.
    """
    local = moment.astimezone(engine_zone(clock))
    return local.date().isoformat(), local.hour * 60 + local.minute


def from_engine(day_iso, minute, clock):
    """The engine's (trading day, minute) back into an aware instant."""
    midnight = datetime.combine(
        date.fromisoformat(day_iso), time.min, tzinfo=engine_zone(clock)
    )
    return midnight + timedelta(minutes=minute)


def engine_span(day, clock):
    """
    The instants the engine will plan on its trading day `day`.

    Everything asked of the engine in one call must fall on ONE of its days,
    and the day asked is the salon's calendar date: for a salon a couple of
    hours from the engine's offset, the two days overlap for all of the
    engine's window, and a start outside this span is one it would refuse.
    """
    return (
        from_engine(day.isoformat(), clock.from_min, clock),
        from_engine(day.isoformat(), clock.to_min, clock),
    )


def hhmm_minutes(text):
    """ "17:30" -> 1050. ValueError for anything else. """
    match = _HHMM.match(text or "")
    if match is None:
        raise ValueError(f"not an HH:MM time: {text!r}")
    return int(match.group(1)) * 60 + int(match.group(2))


# ------------------------------------------------------------ the party


def booker_first(members):
    """
    The order members are sent to the engine: the booker, then the rest.

    POSITION IS MEANING there. `ORGANIZER` puts the whole party's cost on the
    FIRST participant, and the confirm step matches lanes to participants by
    position. The app's own order is kept for everyone else, and every answer
    is mapped back to it before it leaves.

    Returns indices into `members`. A party with no `self` member -- every
    availability request, which carries no identities -- keeps the app's
    order as it is.
    """
    booker = next((i for i, m in enumerate(members) if m.get("kind") == "self"), None)
    if booker is None:
        return list(range(len(members)))
    return [booker] + [i for i in range(len(members)) if i != booker]


def member_minutes(members, minutes_by_service):
    """How long each member's visit takes, in the app's order."""
    return [
        sum(minutes_by_service.get(sid, 0) for sid in m["service_ids"])
        for m in members
    ]


def label(member):
    """
    The member as the engine's refusals will quote them ("Dana: one or more
    services do not exist"): their name, or, where the app sent none -- every
    availability request -- their ref.
    """
    return member.get("name") or f"Member {member['ref']}"


def participants(members, order, *, planning=False):
    """
    `members` as the engine's `participants`, booker first.

    `self` and `registered` members carry their account as `customerId`, so
    each lane is that person's own booking and appears in their own list.
    A guest carries only a name, and the engine files their lane under the
    group.

    `planning` drops both identity fields. The availability route has no
    place for them and refuses the call outright if they are sent.
    """
    out = []
    for i in order:
        m = members[i]
        p = {"label": label(m), "serviceIds": list(m["service_ids"])}
        if not planning:
            if m.get("kind") in ("self", "registered"):
                p["customerId"] = str(m["id"])
            else:
                p["guestName"] = m["name"]
        if m.get("stylist_id"):
            p["preferredStaffId"] = str(m["stylist_id"])
        out.append(p)
    return out


def plan_body(branch_id, day_iso, minutes, members, order):
    """POST /v1/bookings/availability/group, many starts in one list."""
    return {
        "branchId": str(branch_id),
        "day": day_iso,
        "targetMins": list(minutes),
        "mode": "TOGETHER",
        "participants": participants(members, order, planning=True),
    }


# ------------------------------------------------------------ when


def offered_starts(open_span, engine_span_, day_start, longest_minutes,
                   step=GROUP_STEP_MINUTES):
    """
    Every start the salon offers the party on this day, soonest first.

    A start is offered when the salon is open, the engine will plan it, and
    the LONGEST visit in the party still finishes before the salon closes and
    before the engine's day ends. Arriving together, the longest visit is the
    one that decides.

    The clock plays no part: a start already gone is still OFFERED, and the
    view draws it unavailable without asking the engine about it.

    The interval work is slots.starts, the same walk the single-booking
    picker uses, on a half-hour grid counted from salon-local midnight.
    """
    if open_span is None:
        return []

    free = (max(open_span[0], engine_span_[0]), min(open_span[1], engine_span_[1]))
    if free[0] >= free[1]:
        return []

    return slots.starts(
        [free], longest_minutes,
        window_start=day_start,
        window_end=day_start + timedelta(days=2),
        earliest=day_start,
        day_start=day_start,
        grid_minutes=step,
    )


def batches(starts, size=MAX_GROUP_STARTS):
    """`starts` in runs of at most `size`: one engine call each."""
    return [starts[i:i + size] for i in range(0, len(starts), size)]


# ------------------------------------------------------------ answers back


def stylist_card(staff_id, cards):
    """
    The stylist the engine chose: `{id, name}`.

    A staff id the salon's roster does not know still comes back, with its id
    and no name, rather than disappearing from the member it belongs to.
    """
    known = cards.get(str(staff_id))
    if known is not None:
        return known
    return {"id": str(staff_id), "name": None}


def _plan(entry, day_iso, members, order, clock, tz, cards):
    """
    One start the engine says fits, as the app's `plan` (in the app's order)
    and the instant the last member finishes.

    ValueError when a lane per member is missing: guessing which lane
    belongs to whom would put someone with the wrong stylist.
    """
    lanes = entry.get("lanes") or []
    if len(lanes) != len(order):
        raise ValueError("a planned start is missing a lane")

    by_member = {}
    for position, lane in zip(order, lanes):
        by_member[position] = {
            "ref": members[position]["ref"],
            "stylist": stylist_card(lane["staffId"], cards),
            "start": from_engine(day_iso, lane["startMin"], clock).astimezone(tz).isoformat(),
            "end": from_engine(day_iso, lane["endMin"], clock).astimezone(tz).isoformat(),
        }
    last = from_engine(day_iso, max(lane["endMin"] for lane in lanes), clock)
    return [by_member[i] for i in range(len(members))], last


def day_slots(offered, answers, day_iso, members, order, clock, tz, cards, longest_minutes):
    """
    Every offered start as the app draws it: `{start, end, available, plan}`.

    `answers` maps a start to the engine's entry for it. A start with no
    entry was never asked -- it has passed, or is inside the services'
    notice -- and is unavailable, like one the engine says does not fit.

    `end` is when the last member finishes: the engine's figure where the
    party fits, the longest visit where it does not, so a struck-through row
    still shows how long the party would have taken.
    """
    found = []
    for start in offered:
        entry = answers.get(start)
        if entry is not None and entry.get("feasible"):
            plan, last = _plan(entry, day_iso, members, order, clock, tz, cards)
            found.append({
                "start": start.astimezone(tz).isoformat(),
                "end": last.astimezone(tz).isoformat(),
                "available": True,
                "plan": plan,
            })
        else:
            found.append({
                "start": start.astimezone(tz).isoformat(),
                "end": (start + timedelta(minutes=longest_minutes)).astimezone(tz).isoformat(),
                "available": False,
                "plan": [],
            })
    return found


def banded(offered, day_slots_, day_start):
    """
    The day's slots under `Morning`, `Afternoon` and `Evening`, in that
    order, by the salon-local time each one starts. A band with nothing in
    it is left out.
    """
    def wall_minute(start):
        # The wall clock, not minutes elapsed since midnight: on a day the
        # clocks change, 17:00 is still Evening.
        local = start.astimezone(day_start.tzinfo)
        return (local.date() - day_start.date()).days * 24 * 60 + local.hour * 60 + local.minute

    bands = []
    for name, lower, upper in BANDS:
        rows = [
            row for start, row in zip(offered, day_slots_)
            if lower <= wall_minute(start) and (upper is None or wall_minute(start) < upper)
        ]
        if rows:
            bands.append({"label": name, "slots": rows})
    return bands

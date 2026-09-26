"""
Routine bookings: the app's times on booking-api's clock, and back.

PURE: no requests, no database. series_views.py owns those.

booking-api reads every branch on one fixed clock (+06:00 today, read from
its settings, never assumed: EngineClock), and the app speaks the salon's own
clock (+04:00 in Dubai). So EVERY time crosses over, both ways, including the
plain HH:MM ones that carry no offset:

    app 16:30 at +04:00  ->  booking-api 18:30 at +06:00
    booking-api 18:30    ->  app 16:30

A time is converted WITH ITS DATE, as one instant, because near midnight the
two clocks are on different days: the app's 23:00 on the 5th is booking-api's
01:00 on the 6th. An ISO instant (`start_time`) already carries its offset
and is simply put on the salon's clock; its `date` is then the salon-local
day of that instant, as for a group booking.
"""

from datetime import date, datetime, time, timezone as dt_timezone

from .group_translate import engine_zone, hhmm_minutes

# The app's money figures, which booking-api checks against its own and
# refuses with the right one (amount_mismatch). Sent only when the app sent
# them: a dry run may leave them out.
MONEY_FIELDS = ("amount_without_tax", "tax_amount", "discount", "total")


class TimeNotConstant(ValueError):
    """The routine's one time is not one time on booking-api's clock.

    booking-api stores ONE start minute for a routine. A salon clock that
    changes offset between two of its days (daylight saving) would put one
    of them an hour out. No Gulf salon does; this says so rather than
    booking it wrong the day one does.
    """


# ------------------------------------------------------------ one time


def _hhmm(moment):
    return moment.strftime("%H:%M")


def to_engine_time(day, hhmm, tz, clock):
    """The salon's (day, "HH:MM") as booking-api's (day ISO, "HH:MM")."""
    minutes = hhmm_minutes(hhmm)
    local = datetime.combine(day, time(minutes // 60, minutes % 60), tzinfo=tz)
    there = local.astimezone(engine_zone(clock))
    return there.date().isoformat(), _hhmm(there)


def from_engine_time(day_iso, hhmm, tz, engine_tz):
    """booking-api's (day ISO, "HH:MM") as the salon's (day ISO, "HH:MM")."""
    minutes = hhmm_minutes(hhmm)
    there = datetime.combine(
        date.fromisoformat(day_iso), time(minutes // 60, minutes % 60), tzinfo=engine_tz
    )
    here = there.astimezone(tz)
    return here.date().isoformat(), _hhmm(here)


def on_clock(value, tz):
    """An ISO instant on the salon's clock. Anything else as it came."""
    if not isinstance(value, str):
        return value
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        return value
    return moment.astimezone(tz).isoformat()


def _local_day(value, fallback):
    """The salon-local day of an ISO instant already on the salon's clock."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date().isoformat()
        except ValueError:
            pass
    return fallback


def engine_zone_of(answer):
    """
    booking-api's clock, read off its own answer (`created_at` carries the
    offset it writes every time with). None when it cannot be read.
    """
    stamp = answer.get("created_at") if isinstance(answer, dict) else None
    try:
        moment = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
    except ValueError:
        return None
    if moment is None or moment.utcoffset() is None:
        return None
    return dt_timezone(moment.utcoffset())


# ------------------------------------------------------------ the request


def engine_body(data, *, branch_id, tz, clock, sent):
    """
    The body for POST /v1/mobile-booking/series, every time on booking-api's
    clock.

    `data` is the request as series_serializers validated it; `sent` is the
    request as it arrived, for the products and the money figures, which are
    the app's to state and booking-api's to check.

    WITHOUT A TIME the days go as they are: there is no instant to convert,
    and booking-api's trading day (10:00 to 22:00 at +06:00) is the salon's
    08:00 to 20:00 on the same date. The times that come back are converted
    (present_preview).
    """
    hhmm = data.get("time")
    start_date = data.get("start_date")
    dates = data.get("dates")

    engine_time = None
    if hhmm is not None:
        days = list(dates) if dates else ([start_date] if start_date else [])
        converted = [to_engine_time(d, hhmm, tz, clock) for d in days]
        times = {t for _, t in converted}
        if len(times) > 1:
            raise TimeNotConstant(
                "This salon's clock changes between these days, so one time "
                "cannot be booked for all of them. Book them separately."
            )
        if converted:
            engine_time = converted[0][1]
            if dates:
                dates = [d for d, _ in converted]
            else:
                start_date = converted[0][0]
        else:
            # No day to anchor the time to: booking-api refuses the request
            # for the missing day, in its own words, with the time as sent.
            engine_time = hhmm

    body = {
        "dry_run": bool(data.get("dry_run")),
        "salon_id": str(branch_id),
        "services": [{"id": str(s["id"])} for s in data["services"]],
        "stylist_id": str(data["stylist_id"]) if data.get("stylist_id") else None,
        "frequency": data["frequency"],
        "start_date": start_date.isoformat() if isinstance(start_date, date) else start_date,
        "sessions": data.get("sessions"),
        "dates": [d.isoformat() if isinstance(d, date) else d for d in dates] if dates else None,
        "time": engine_time,
        "payment_plan": data["payment_plan"],
        "picks": [
            _engine_pick(p, tz, clock) for p in data.get("picks") or []
        ],
    }
    products = sent.get("products") if isinstance(sent, dict) else None
    if products:
        body["products"] = list(products)
    for key in MONEY_FIELDS:
        if isinstance(sent, dict) and key in sent:
            body[key] = sent[key]
    return body


def _engine_pick(pick, tz, clock):
    day, hhmm = to_engine_time(pick["date"], pick["time"], tz, clock)
    return {
        "index": pick["index"],
        "date": day,
        "time": hhmm,
        "stylist_id": str(pick["stylist_id"]) if pick.get("stylist_id") else None,
    }


# ------------------------------------------------------------ the answers


def _session(s, tz):
    """One session (preview or hub) on the salon's clock."""
    out = dict(s)
    for key in ("start_time", "end_time"):
        if key in out:
            out[key] = on_clock(out[key], tz)
    out["date"] = _local_day(out.get("start_time"), out.get("date"))
    if isinstance(out.get("alternatives"), list):
        out["alternatives"] = [_alternative(a, tz) for a in out["alternatives"]]
    return out


def _alternative(a, tz):
    out = dict(a)
    start = on_clock(out.get("start_time"), tz)
    out["start_time"] = start
    if isinstance(start, str):
        try:
            moment = datetime.fromisoformat(start)
            out["date"], out["time"] = moment.date().isoformat(), _hhmm(moment)
        except ValueError:
            pass
    return out


def _first_engine_day(answer):
    """booking-api's day of the routine's first session, before conversion."""
    sessions = answer.get("sessions") or []
    ordered = sorted(
        (s for s in sessions if isinstance(s, dict) and s.get("date")),
        key=lambda s: s.get("index", 0),
    )
    return ordered[0]["date"] if ordered else None


def _routine_time(answer, tz, engine_tz):
    """The routine's own HH:MM, from booking-api's clock to the salon's."""
    hhmm = answer.get("time")
    day = _first_engine_day(answer)
    if not isinstance(hhmm, str) or day is None or engine_tz is None:
        return hhmm
    try:
        return from_engine_time(day, hhmm, tz, engine_tz)[1]
    except ValueError:
        return hhmm


def present_preview(answer, *, tz, engine_tz):
    """
    booking-api's dry run on the salon's clock: the routine's time, every
    session's start and end (and so its date), every alternative's date and
    time, and the times free on every day.

    A free time is booking-api's HH:MM on the first bookable day. One that
    lands on another salon-local date is not a time on that day at all, and
    is dropped rather than shown against the wrong date.
    """
    out = dict(answer)
    out["time"] = _routine_time(answer, tz, engine_tz)
    out["sessions"] = [_session(s, tz) for s in answer.get("sessions") or []]

    times = answer.get("available_times")
    if isinstance(times, list) and engine_tz is not None:
        bookable = [
            s for s in answer.get("sessions") or []
            if isinstance(s, dict) and not s.get("later") and s.get("date")
        ]
        day = bookable[0]["date"] if bookable else _first_engine_day(answer)
        converted = []
        for hhmm in times:
            try:
                here_day, here = from_engine_time(day, hhmm, tz, engine_tz)
            except (TypeError, ValueError):
                continue
            if here_day == day:
                converted.append(here)
        out["available_times"] = converted
    return out


def present_hub(answer, *, salon_id, tz, engine_tz):
    """
    booking-api's routine on the salon's clock, naming the salon the app
    knows. `pause.until` is a date and stays one.
    """
    out = dict(answer)
    if salon_id is not None:
        out["salon_id"] = str(salon_id)
    if tz is None:
        return out
    out["time"] = _routine_time(answer, tz, engine_tz)
    out["sessions"] = [_session(s, tz) for s in answer.get("sessions") or []]
    if isinstance(answer.get("next_session"), dict):
        out["next_session"] = _session(answer["next_session"], tz)
    if "created_at" in out:
        out["created_at"] = on_clock(out["created_at"], tz)
    return out


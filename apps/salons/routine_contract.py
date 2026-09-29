"""
The app team's routine contract, both ways (docs/routine-booking-fe-contract.md
and docs/routine-booking-fe-contract-draft.md, with the decisions in
docs/ROUTINE_FE_CONTRACT_AUDIT.md): the contract's words and ISO times to
booking-api's series route (POST/PATCH /v1/mobile-booking/series), and
booking-api's answers back to the contract's shapes.

PURE: no requests, no database, no clock. routine_views.py (C3 to C5) owns
those and hands in what they found: the salon's zone, booking-api's clock,
who can do the services, the salon's hours as a function, names, avatars.

The time conversions are series_translate's, already proven both ways (the
salon's clock to booking-api's +06:00 and back, with the date, near
midnight too). This file adds only what the contract says differently:

    start_time (ISO, any offset)  ->  the salon's day and HH:MM  ->  booking-api's
    cadence week/fortnight/month  ->  WEEKLY / EVERY_2_WEEKS / MONTHLY
    service_ids                   ->  services [{id}]
    sessions[] (create)           ->  picks, for a session not on its cadence slot
    free / refusal / reason       ->  available / reason (one, in the contract's order)
    frequency (answers)           ->  cadence

A refusal found here is a Refusal (field, code, message[, expected]): the
view answers it in our envelope, 422.
"""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from . import series_translate as st

# ------------------------------------------------------------ words

CADENCES = {"week": "WEEKLY", "fortnight": "EVERY_2_WEEKS", "month": "MONTHLY"}
CADENCE_OF = {frequency: word for word, frequency in CADENCES.items()}

# v1 is paid at the salon (D2, draft §4).
PAYMENT_PLAN = "PAY_AT_SALON"

# What every request of the contract asks booking-api for (steps B2 to B6).
CONTRACT_OPTIONS = {
    "check_later": True,
    "with_reasons": True,
    "alternative_rule": "SAME_STYLIST_FORWARD",
    "alternatives_max": 12,
    "strict_picks": True,
}

# The one reason a session shows, when more than one is true (draft §7).
# The first three are the salon's hours, which only this service knows;
# the last is booking-api's.
REASON_ORDER = ("salon_closed", "outside_hours", "too_soon", "stylist_unavailable")

# How many alternatives the app is shown, after the ones outside the salon's
# hours are dropped (booking-api sends up to CONTRACT_OPTIONS["alternatives_max"]).
ALTERNATIVES_SHOWN = 3

# booking-api's codes, in the contract's words.
CODE_RENAMES = {
    "session_not_free": "slot_taken",
    "invalid_frequency": "invalid_cadence",
}

VALIDATION = "Please correct the highlighted fields."


class Refusal(Exception):
    """One field the contract refuses, as our envelope names it."""

    def __init__(self, field, code, message, expected=None):
        super().__init__(message)
        self.field, self.code, self.message, self.expected = field, code, message, expected

    def as_error(self):
        error = {"field": self.field, "code": self.code, "message": self.message}
        if self.expected is not None:
            error["expected"] = self.expected
        return error


# ------------------------------------------------------------ one time


def parse_instant(value, field):
    """
    An ISO 8601 time WITH an offset, as an aware datetime. Any offset is read
    as that instant (Q7, as the single create does); none is `invalid_time`,
    and so is a time with seconds, which HH:MM could not carry.
    """
    if not isinstance(value, str):
        raise Refusal(field, "invalid_time", "Send an ISO 8601 time with an offset.")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise Refusal(field, "invalid_time", "Send an ISO 8601 time with an offset.") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise Refusal(
            field, "invalid_time",
            "The time needs its offset, for example 2026-09-20T18:00:00+04:00.",
        )
    if moment.second or moment.microsecond:
        raise Refusal(field, "invalid_time", "A time is to the minute.")
    return moment


def salon_day_and_time(value, tz, field):
    """start_time on the salon's clock: (its day, "HH:MM"), and the instant."""
    moment = parse_instant(value, field).astimezone(tz)
    return moment.date(), moment.strftime("%H:%M"), moment


def frequency_of(cadence):
    """The contract's cadence as booking-api's frequency, or `invalid_cadence`."""
    if not isinstance(cadence, str) or cadence not in CADENCES:
        raise Refusal("cadence", "invalid_cadence", "cadence is week, fortnight or month.")
    return CADENCES[cadence]


# ------------------------------------------------------------ Any Available Expert


def stylists_doing_all(coverage, service_ids):
    """
    Who can do EVERY one of these services alone, measured as the Expert
    step measures it (selectors.stylist_service_coverage: staff id to the
    services they can do). Sorted, so the same salon always sends the same
    list.
    """
    wanted = {str(s) for s in service_ids}
    return sorted(
        str(staff) for staff, done in coverage.items() if wanted <= {str(s) for s in done}
    )


# ------------------------------------------------------------ requests


def preview_body(request, *, branch_id, tz, clock, candidates):
    """
    The preview (POST /booking/routine-preview) as booking-api's dry run.

    `request` is the contract's body: salon_id, service_ids, stylist_id
    (optional), start_time, cadence, sessions. `candidates` are the stylists
    who do all the services (stylists_doing_all), used only when no
    stylist_id is sent: none is `no_stylist_available`.
    """
    frequency = frequency_of(request.get("cadence"))
    day, hhmm, _ = salon_day_and_time(request.get("start_time"), tz, "start_time")
    stylist = request.get("stylist_id") or None
    if stylist is None and not candidates:
        raise Refusal(
            "stylist_id", "no_stylist_available",
            "Nobody at this salon does all of these services. Choose other services.",
        )
    data = {
        "dry_run": True,
        "services": [{"id": str(s)} for s in request.get("service_ids") or []],
        "stylist_id": str(stylist) if stylist else None,
        "frequency": frequency,
        "start_date": day,
        "sessions": request.get("sessions"),
        "dates": None,
        "time": hhmm,
        "payment_plan": PAYMENT_PLAN,
        "picks": [],
    }
    body = st.engine_body(data, branch_id=branch_id, tz=tz, clock=clock, sent={})
    body.update(CONTRACT_OPTIONS)
    if stylist is None:
        body["stylist_candidates"] = list(candidates)
    return body


def plan_request(create):
    """
    The create's own plan, asked for first (a dry run, as the old create
    does): the preview request the create's body stands for. start_time is
    the anchor (Q1), the count is how many sessions were sent.
    """
    return {
        "salon_id": create.get("salon_id"),
        "service_ids": [s.get("id") for s in create.get("services") or [] if isinstance(s, dict)],
        "stylist_id": create.get("stylist_id"),
        "start_time": create.get("start_time"),
        "cadence": create.get("cadence"),
        "sessions": len(create.get("sessions") or []),
    }


def _fils(value):
    """Decimal AED as whole fils, or None when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        return int((Decimal(str(value)) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return None


def check_payment(create):
    """
    Draft §4: pay at the salon only, for now. The contract's payment fields
    must say exactly that. services[].amount is NOT checked (Q5): only the
    four totals are, by booking-api (amount_mismatch).
    """
    if create.get("payment_status") != "DRAFT":
        raise Refusal("payment_status", "invalid_payment_status", "payment_status is DRAFT.")
    if _fils(create.get("advance_paid_amount")) != 0:
        raise Refusal(
            "advance_paid_amount", "amount_mismatch",
            "Nothing is paid in the app for now: advance_paid_amount is 0.", expected=0,
        )
    total = _fils(create.get("total"))
    due = _fils(create.get("due_amount"))
    if due is None or total is None or due != total:
        raise Refusal(
            "due_amount", "amount_mismatch",
            "Everything is paid at the salon: due_amount is the total.",
            expected=create.get("total") if total is not None else None,
        )
    products = create.get("products", [])
    if not isinstance(products, list) or products:
        raise Refusal("products", "products_not_supported", "Products cannot be added to a routine yet.")
    if create.get("promo_code") is not None:
        raise Refusal("promo_code", "invalid_promo", "A promo code cannot be used on a routine.")
    if create.get("status") != "BOOKED":
        raise Refusal("status", "invalid_status", "status is BOOKED.")
    if create.get("booking_type") != "ROUTINE":
        raise Refusal("booking_type", "invalid_booking_type", "booking_type is ROUTINE.")


def plan_slots(preview):
    """Each session's cadence slot, from booking-api's plan on the salon's clock."""
    ordered = sorted(preview.get("sessions") or [], key=lambda s: s.get("index", 0))
    return [datetime.fromisoformat(s["start_time"]) for s in ordered]


def plan_minutes(preview):
    """How long one session runs, from booking-api's plan (the services' length)."""
    first = min(preview.get("sessions") or [], key=lambda s: s.get("index", 0))
    start = datetime.fromisoformat(first["start_time"])
    end = datetime.fromisoformat(first["end_time"])
    return int((end - start).total_seconds() // 60)


def session_picks(sessions, *, slots, minutes, tz):
    """
    The create's sessions[], checked, as booking-api's picks.

    - indexes 0, 1, 2, ... in order (`invalid_sessions`), one per slot;
    - each `date` is the salon's date of its start_time (`date_mismatch`);
    - each end_time is its start_time plus the services' length
      (`invalid_window`), as for a single booking (Q8);
    - a session on its cadence slot is no pick; any other becomes one, on
      the salon's clock (engine_body puts it on booking-api's). Whether that
      time is one the alternatives rule allows is booking-api's to say
      (strict_picks: `session_not_offered`), never guessed here.
    """
    if not isinstance(sessions, list) or [
        s.get("index") if isinstance(s, dict) else None for s in sessions
    ] != list(range(len(sessions))) or len(sessions) != len(slots):
        raise Refusal("sessions", "invalid_sessions", "Number the sessions 0, 1, 2, ... in order.")

    picks = []
    for i, session in enumerate(sessions):
        start = parse_instant(session.get("start_time"), f"sessions[{i}].start_time")
        local = start.astimezone(tz)
        if session.get("date") != local.date().isoformat():
            raise Refusal(
                f"sessions[{i}].date", "date_mismatch",
                f"date says {session.get('date')} and start_time is {local.date().isoformat()} "
                "at the salon.",
            )
        end = parse_instant(session.get("end_time"), f"sessions[{i}].end_time")
        if end - start != timedelta(minutes=minutes):
            expected = (local + timedelta(minutes=minutes)).isoformat()
            raise Refusal(
                f"sessions[{i}].end_time", "invalid_window",
                f"These services run {minutes} minutes, so the visit ends at {expected}.",
            )
        if start != slots[i]:
            picks.append({
                "index": i, "date": local.date(), "time": local.strftime("%H:%M"), "stylist_id": None,
            })
    return picks


def create_body(create, *, branch_id, tz, clock, candidates, slots, minutes):
    """
    The create (POST /booking/routine) as booking-api's real create: the
    payment fields checked (§4), the sessions turned into picks, the four
    totals passed on for booking-api to check. `slots` and `minutes` come
    from the plan asked for first (plan_slots, plan_minutes).
    """
    check_payment(create)
    picks = session_picks(create.get("sessions"), slots=slots, minutes=minutes, tz=tz)
    body = preview_body(
        plan_request(create), branch_id=branch_id, tz=tz, clock=clock, candidates=candidates,
    )
    body["dry_run"] = False
    body["picks"] = [st._engine_pick(p, tz, clock) for p in picks]
    for key in st.MONEY_FIELDS:
        if key in create:
            body[key] = create[key]
    return body


def move_request(body, session_id, tz):
    """
    PATCH /booking/<id>/sessions/<session_id> as today's RESCHEDULE, on the
    salon's clock: routine_views (C5) holds it to the salon's hours and puts
    it on booking-api's clock, as the old route does.
    """
    day, hhmm, _ = salon_day_and_time(body.get("start_time"), tz, "start_time")
    out = {
        "action": "RESCHEDULE",
        "session_id": str(session_id),
        "date": day.isoformat(),
        "time": hhmm,
        "dry_run": body.get("dry_run") is True,
    }
    if body.get("stylist_id"):
        out["stylist_id"] = str(body["stylist_id"])
    return out


# ------------------------------------------------------------ the salon's hours


def hours_codes(start, open_span, engine_span_, earliest, longest_minutes):
    """
    EVERY hours reason that applies to this start, in REASON_ORDER. The same
    tests as group_translate.start_refusal, which answers only the first by
    its own order (salon_closed, too_soon, outside_hours); the contract puts
    outside_hours before too_soon.
    """
    codes = []
    if open_span is None:
        codes.append("salon_closed")
    else:
        finish = start + timedelta(minutes=longest_minutes)
        latest_finish = min(open_span[1], engine_span_[1])
        if start < max(open_span[0], engine_span_[0]) or finish > latest_finish:
            codes.append("outside_hours")
    if start < earliest:
        codes.append("too_soon")
    return codes


def final_reason(codes, engine_reason):
    """The one reason shown: the first of REASON_ORDER that applies."""
    found = set(codes)
    if engine_reason:
        found.add(engine_reason)
    for code in REASON_ORDER:
        if code in found:
            return code
    return None


# ------------------------------------------------------------ answers


def present_preview(answer, *, tz, engine_tz, hours, names):
    """
    booking-api's dry run as the contract's preview (§2):

        {cadence, sessions: [{index, date, start_time, end_time, available,
         reason?, stylist, alternatives}], per_session_total, plan_total}

    `hours(start)` is every hours code for a start on the salon's clock
    (hours_codes). A session is available only when booking-api says free
    AND the salon's hours allow it; `reason` only when not, the first in
    REASON_ORDER. `stylist` is who would take it, null when not available.
    Alternatives outside the salon's hours are dropped FIRST, then the first
    ALTERNATIVES_SHOWN are kept, as ISO times on the salon's clock.
    """
    salon = st.present_preview(answer, tz=tz, engine_tz=engine_tz)
    sessions = []
    for s in sorted(salon.get("sessions") or [], key=lambda x: x.get("index", 0)):
        codes = hours(datetime.fromisoformat(s["start_time"]))
        available = s.get("free") is True and not codes
        item = {
            "index": s.get("index"),
            "date": s.get("date"),
            "start_time": s.get("start_time"),
            "end_time": s.get("end_time"),
            "available": available,
        }
        if not available:
            engine_reason = s.get("reason") or ("stylist_unavailable" if s.get("free") is False else None)
            item["reason"] = final_reason(codes, engine_reason) or "stylist_unavailable"
        stylist = s.get("stylist_id")
        item["stylist"] = (
            {"id": stylist, "name": names.get(stylist)} if available and stylist else None
        )
        kept = [
            a["start_time"] for a in s.get("alternatives") or []
            if isinstance(a.get("start_time"), str)
            and not hours(datetime.fromisoformat(a["start_time"]))
        ]
        item["alternatives"] = kept[:ALTERNATIVES_SHOWN]
        sessions.append(item)

    plan = ((answer.get("money") or {}).get("plans") or {}).get(PAYMENT_PLAN) or {}
    per_session = (plan.get("sessions") or [{}])[0].get("total")
    return {
        "cadence": CADENCE_OF.get(answer.get("frequency")),
        "sessions": sessions,
        "per_session_total": per_session,
        "plan_total": plan.get("total"),
    }


def _local_day(value):
    return datetime.fromisoformat(value).date().isoformat() if isinstance(value, str) else None


def _session_on_clock(session, tz):
    out = dict(session)
    for key in ("start_time", "end_time"):
        out[key] = st.on_clock(out.get(key), tz)
    if isinstance(out.get("start_time"), str):
        out["date"] = _local_day(out["start_time"])
    return out


def present_routine(answer, *, salon_id, tz, avatars):
    """
    booking-api's routine as one booking (view=booking, B7) in the
    contract's shape (draft §3 and §5): `frequency` becomes `cadence`, in its
    place; every time goes onto the salon's clock (the top, each session,
    created_at) and each date is the salon's date of its start;
    `stylists[].avatar_url`, which booking-api never has, comes from our own
    stylist list (`avatars`: stylist id to url).
    """
    out = {}
    for key, value in answer.items():
        if key == "frequency":
            out["cadence"] = CADENCE_OF.get(value)
        else:
            out[key] = value
    if salon_id is not None:
        out["salon_id"] = str(salon_id)
    for key in ("start_time", "end_time", "created_at"):
        if key in out:
            out[key] = st.on_clock(out[key], tz)
    if isinstance(out.get("start_time"), str):
        out["date"] = _local_day(out["start_time"])
    out["sessions"] = [_session_on_clock(s, tz) for s in answer.get("sessions") or []]
    out["stylists"] = [
        {**s, "avatar_url": avatars.get(s.get("id"), s.get("avatar_url"))}
        for s in answer.get("stylists") or []
    ]
    return out


# ------------------------------------------------------------ refusals


def _field(field, *, route, picks):
    """booking-api's field name as the contract's."""
    if not isinstance(field, str):
        return field
    if field.startswith("picks[") and "]" in field:
        j = field[len("picks["):field.index("]")]
        if j.isdigit() and picks is not None and int(j) < len(picks):
            return f"sessions[{picks[int(j)]['index']}]"
        return "sessions"
    if field in ("picks", "dates"):
        return "sessions"
    if field == "frequency":
        return "cadence"
    if field in ("start_date", "time", "date"):
        return "start_time"
    if field == "services":
        return "service_ids" if route == "preview" else "services"
    if field == "stylist_candidates":
        return "stylist_id"
    if field in ("session_id", "action"):
        return None
    return field


def _not_found(message="No such session in this routine."):
    return {
        "detail": message,
        "code": "not_found",
        "errors": [{"field": None, "code": "not_found", "message": message}],
    }


def _field_of_message(message):
    """"property foo should not exist" -> foo; "sessions must be ..." -> sessions."""
    words = message.split() if isinstance(message, str) else []
    if len(words) >= 2 and words[0] == "property":
        return words[1]
    return words[0] if words else None


def translate_refusal(status, body, *, route, picks=None):
    """
    A refusal from booking-api, in the contract's words: (status, body).

    `route` is "preview", "create" or "move"; `picks` are the picks the
    create sent (picks[j] becomes the session it stood for, sessions[i]).

    - booking-api's mobile envelope: codes renamed (session_not_free is
      slot_taken, invalid_frequency is invalid_cadence) and fields renamed;
      on a move, "that session is not part of this routine" is a 404;
    - booking-api's OTHER envelope (audit F7: its global pipe's 400, and
      IDEMPOTENCY_KEY_REUSED): turned into ours, 422 validation_error and
      409 idempotency_key_reused;
    - anything else goes back as it came.
    """
    if not isinstance(body, dict):
        return status, body

    if "errors" not in body and "statusCode" in body:
        if status == 400:
            messages = body.get("message")
            messages = messages if isinstance(messages, list) else [messages]
            errors = [
                {
                    "field": _field(_field_of_message(m), route=route, picks=picks),
                    "code": "invalid",
                    "message": str(m),
                }
                for m in messages if m
            ]
            return 422, {"detail": VALIDATION, "code": "validation_error", "errors": errors}
        if body.get("code") == "IDEMPOTENCY_KEY_REUSED":
            message = "That Idempotency-Key was already used for a different request."
            return 409, {
                "detail": message,
                "code": "idempotency_key_reused",
                "errors": [{"field": None, "code": "idempotency_key_reused", "message": message}],
            }
        return status, body

    errors = body.get("errors")
    if not isinstance(errors, list):
        return status, body

    if route == "move" and any(
        isinstance(e, dict) and e.get("code") == "invalid_sessions" and e.get("field") == "session_id"
        for e in errors
    ):
        return 404, _not_found()

    renamed = []
    for e in errors:
        if not isinstance(e, dict):
            renamed.append(e)
            continue
        out = dict(e)
        out["code"] = CODE_RENAMES.get(e.get("code"), e.get("code"))
        out["field"] = _field(e.get("field"), route=route, picks=picks)
        renamed.append(out)

    first = renamed[0] if renamed and isinstance(renamed[0], dict) else {}
    out = dict(body)
    out["errors"] = renamed
    if status == 422:
        out["code"] = "validation_error"
    elif first.get("code"):
        out["code"] = first["code"]
    if status == 409 and first.get("message"):
        out["detail"] = first["message"]
    return status, out

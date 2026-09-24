"""
Group bookings for the app: when a whole party fits, and booking it.

    POST /api/v1/booking/group-availability   every start of the day, and who fits
    POST /api/v1/booking/group                hold the party, then confirm it

gostyle-booking-api does the planning and owns the bookings; this service
does what apps/salons/booking_api.py does for a single booking -- resolve the
salon, translate the app's shape into the engine's, forward it with the
caller's own token, and translate the answer back. The translation itself is
group_translate.py, which is pure; this module owns the requests, the clock
and the database reads.

Kept apart from views.py on purpose: nothing a single booking does changes
here, and the helpers it shares are imported from there untouched.
"""

import hashlib
import logging
import uuid
from datetime import date, datetime, time, timedelta

from django.core.cache import cache
from django.http import Http404
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    ErrorDetail,
    UnsupportedMediaType,
    ValidationError,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import ConsumerAccount

from . import group_translate as gt
from . import timezones
from .booking_api import (
    BookingApiUnavailable,
    confirm_group,
    engine_clock,
    hold_group,
    plan_group,
    release_group_hold,
)
from .group_serializers import (
    REGISTERED,
    SELF,
    GroupAvailabilityRequestSerializer,
    GroupBookingRequestSerializer,
)
from .money import major
from .selectors import (
    booking_route,
    salon_profile,
    salon_stylists,
    service_timing_rows,
    stylist_service_coverage,
)
from .views import _OUR_ENVELOPE, BookingApiDown, _idempotency_key, _open_span

logger = logging.getLogger(__name__)

# How long a group booking in flight keeps its key. Longer than the slowest
# path it guards -- settings, hold, confirm and a release, each bounded by
# BOOKING_API_TIMEOUT -- and short enough that a worker that died mid-request
# does not lock the customer out for long.
IN_PROGRESS_SECONDS = 90

# How long a finished answer is replayed to a retry of the same request.
RECEIPT_SECONDS = 24 * 60 * 60

_RECEIPT_NAMESPACE = "gostyle-customer-api/group-booking"

_STATUS_UNKNOWN = (
    "We could not confirm whether this group booking went through. Check "
    "your bookings before trying again."
)


def _now(tz):
    """The clock, in one place, so a test can stand at any moment."""
    return datetime.now(tz)


# ------------------------------------------------------------ shared steps


def _json_only(request):
    """415 before any work, as POST /booking does."""
    media_type = (request.content_type or "").split(";")[0].strip().lower()
    if media_type != "application/json":
        raise UnsupportedMediaType(media_type or "none")


def _validated(serializer_class, request, **context):
    serializer = serializer_class(data=request.data, context=context)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def _refuse(field, message, code):
    """A 422 in this project's envelope, on one field."""
    return ValidationError({field: [ErrorDetail(message, code=code)]})


def _salon_and_route(salon_id):
    """
    The salon, and the branch and tenant booking-api knows it by.

    Both, or a 404. A salon this service cannot show is not one it can book,
    and a salon that resolves to no single branch is exactly the case
    booking_route refuses to guess about.
    """
    salon = salon_profile(salon_id)
    if salon is None:
        raise Http404("Salon not found")
    route = booking_route(str(salon.id))
    if route is None:
        raise Http404("Salon not found")
    return salon, route


def _service_rows(salon, members, field):
    """
    The salon's own row for every service the party picked, keyed by id.

    `foreign_id` when any is not this salon's: another salon's, archived, or
    switched off for online booking -- measured against the same set GET
    /salon/<id>/services lists. booking-api would refuse it too, but in its
    own shape and quoting a member's name.
    """
    ids = sorted({sid for m in members for sid in m["service_ids"]})
    rows = {str(row["id"]): row for row in service_timing_rows(salon, ids)}
    if any(sid not in rows for sid in ids):
        raise _refuse(field, "One or more services are not offered by this salon.", "foreign_id")
    return rows


def _roster(salon):
    """Every stylist at the salon as the app draws one here, keyed by id."""
    return {
        str(s.id): {
            "id": str(s.id),
            "name": " ".join(filter(None, [s.first_name, s.last_name])) or None,
        }
        for s in salon_stylists(salon)
    }


def _check_stylists(salon, members, roster):
    """
    Every stylist the app chose: at this salon, once each, and able to do
    all of that member's services.

    `foreign_id` for one off the roster. `stylist_repeated` for one chosen
    for two members: everyone starts together, and booking-api gives every
    member their own professional, so that party fits at no time at all --
    better said now than as a day of struck-through slots. `stylist_mismatch`
    for one who cannot do the member's work, measured exactly as the Expert
    step measures it (GET /salon/<id>/stylists?service_ids=), and as
    booking-api does: one person holding every skill the member's services
    need.
    """
    chosen = [m for m in members if m["stylist_id"]]
    if not chosen:
        return

    if any(m["stylist_id"] not in roster for m in chosen):
        raise _refuse("stylist_id", "That stylist does not work at this salon.", "foreign_id")

    picked = [m["stylist_id"] for m in chosen]
    if len(set(picked)) != len(picked):
        raise _refuse(
            "stylist_id",
            "Everyone in the party needs their own stylist. Choose a different "
            "one, or let the salon assign one.",
            "stylist_repeated",
        )

    service_ids = sorted({sid for m in chosen for sid in m["service_ids"]})
    coverage = stylist_service_coverage(
        salon, [uuid.UUID(s) for s in service_ids], [uuid.UUID(s) for s in picked]
    )
    covered = {str(staff): {str(s) for s in done} for staff, done in coverage.items()}
    for m in chosen:
        if not set(m["service_ids"]) <= covered.get(m["stylist_id"], set()):
            who = f"{m['name']}: t" if m.get("name") else "T"
            raise _refuse(
                "stylist_id",
                f"{who}hat stylist cannot do all of these services. Choose "
                "another, or let the salon assign one.",
                "stylist_mismatch",
            )


def _bookable_accounts(ids):
    """
    {id: full_name} for those of `ids` that are active, verified accounts.

    The test GET /user/lookup applies, so an id lookup would never have
    returned is not one a party can be booked for.
    """
    wanted = []
    for raw in ids:
        try:
            wanted.append(uuid.UUID(raw))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return {}
    return {
        str(pk): name
        for pk, name in ConsumerAccount.objects.filter(
            id__in=wanted, is_active=True, account_verified=True
        ).values_list("id", "full_name")
    }


def _name_members(booker, members):
    """
    Every member's display name, and `unknown_user` for an account that
    cannot be booked for.

    Missing, unverified and disabled all answer `unknown_user` alike, as
    they all answer the same 404 from GET /user/lookup. The account's own
    name wins over one the app typed; a guest is the name the app sent.
    """
    names = _bookable_accounts([m["id"] for m in members if m["kind"] == REGISTERED])
    for m in members:
        if m["kind"] == SELF:
            m["name"] = getattr(booker, "full_name", "") or m["name"]
        elif m["kind"] == REGISTERED:
            if m["id"] not in names:
                raise _refuse(
                    "id",
                    "That account could not be found. Add this person as a guest instead.",
                    "unknown_user",
                )
            m["name"] = names[m["id"]] or m["name"]


def _party_timing(rows, members):
    """
    The longest visit in the party, and the notice the party needs.

    The same figures the single-booking picker uses: the platform's staged
    minutes where a service has stages, its catalogue duration otherwise,
    and the longest lead time any chosen service asks for.
    """
    minutes = {
        sid: row["stage_minutes"] or row["duration_minutes"] or 0
        for sid, row in rows.items()
    }
    longest = max(gt.member_minutes(members, minutes))
    lead = max([row["lead_time_minutes"] or 0 for row in rows.values()] or [0])
    return longest, lead


def _catalogue(rows):
    """Each chosen service's name and price, as the services tab shows them."""
    return {
        sid: {
            "name": row.get("name"),
            "price": major(row.get("branch_price_minor") or row.get("price_minor")),
        }
        for sid, row in rows.items()
    }


def _clock(authorization, route):
    try:
        return engine_clock(
            authorization=authorization,
            tenant_id=route["tenant_id"],
            branch_id=route["branch_id"],
        )
    except BookingApiUnavailable as exc:
        raise BookingApiDown() from exc


# ------------------------------------------------------------ availability


_PARTY_422 = (
    "This project's envelope. `invalid_party_size` (not 2 to 8 members), "
    "`duplicate_ref`, `member_no_services`, `foreign_id` (a service or "
    "stylist not at this salon), `stylist_repeated` (one stylist chosen for "
    "two members), `stylist_mismatch` (a stylist who cannot do that member's "
    "services)."
)


@extend_schema(
    summary="Every start of the day, and whether the whole party fits",
    description=(
        "Every half-hour start the salon offers the party on `date`, in "
        "Morning / Afternoon / Evening bands. A start is `available` only if "
        "every member fits; then `plan` says who is with whom, and when. "
        "Starts already gone, or inside the services' notice, are offered "
        "but unavailable.\n\n"
        "Nothing is held: another customer may take a time before the party "
        "is booked. See docs/BOOKING_GROUP_API.md §3."
    ),
    request=GroupAvailabilityRequestSerializer,
    responses={
        200: OpenApiResponse(
            description="The day, banded. A closed salon is `bands: []`.",
            examples=[
                OpenApiExample(
                    "One band",
                    value={
                        "date": "2026-10-11",
                        "bands": [{
                            "label": "Afternoon",
                            "slots": [
                                {
                                    "start": "2026-10-11T15:00:00+04:00",
                                    "end": "2026-10-11T16:00:00+04:00",
                                    "available": True,
                                    "plan": [
                                        {"ref": 0, "stylist": {"id": "7d1e…", "name": "Maya E."},
                                         "start": "2026-10-11T15:00:00+04:00",
                                         "end": "2026-10-11T16:00:00+04:00"},
                                        {"ref": 1, "stylist": {"id": "91aa…", "name": "Anya"},
                                         "start": "2026-10-11T15:00:00+04:00",
                                         "end": "2026-10-11T15:45:00+04:00"},
                                    ],
                                },
                                {
                                    "start": "2026-10-11T15:30:00+04:00",
                                    "end": "2026-10-11T16:30:00+04:00",
                                    "available": False,
                                    "plan": [],
                                },
                            ],
                        }],
                    },
                ),
            ],
        ),
        404: OpenApiResponse(response=_OUR_ENVELOPE, description="No such salon."),
        415: OpenApiResponse(response=_OUR_ENVELOPE, description="Not application/json."),
        422: OpenApiResponse(response=_OUR_ENVELOPE, description=_PARTY_422),
        503: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description="booking-api unreachable, or answering in a shape this cannot read.",
        ),
    },
)
class GroupAvailabilityView(APIView):
    """POST /api/v1/booking/group-availability"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        _json_only(request)
        data = _validated(GroupAvailabilityRequestSerializer, request)
        salon, route = _salon_and_route(data["salon_id"])
        members = data["members"]
        rows = _service_rows(salon, members, "service_ids")
        roster = _roster(salon)
        _check_stylists(salon, members, roster)

        tz = timezones.resolve(salon.branch_timezone, salon.id)
        day = data["date"]
        day_start = datetime.combine(day, time.min, tzinfo=tz)
        answer = {"date": day.isoformat(), "bands": []}

        open_span = _open_span(salon, tz, day, day_start)
        if open_span is None:
            # Asked of nobody: a closed salon offers no starts.
            return Response(answer)

        longest, lead = _party_timing(rows, members)
        authorization = request.META.get("HTTP_AUTHORIZATION", "")
        clock = _clock(authorization, route)
        offered = gt.offered_starts(
            open_span, gt.engine_span(day, clock), day_start, longest
        )
        earliest = _now(tz) + timedelta(minutes=lead)
        askable = [start for start in offered if start >= earliest]

        order = gt.booker_first(members)
        engine_day = day.isoformat()
        answers = {}
        for batch in gt.batches(askable):
            code, entries = self._plan(batch, engine_day, clock, route, members, order, authorization)
            if code is not None:
                return Response(entries, status=code)
            answers.update(zip(batch, entries))

        try:
            found = gt.day_slots(
                offered, answers, engine_day, members, order, clock, tz, roster, longest
            )
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("booking-api group plan answer unreadable: %s", exc)
            raise BookingApiDown() from exc

        return Response({**answer, "bands": gt.banded(offered, found, day_start)})

    @staticmethod
    def _plan(batch, engine_day, clock, route, members, order, authorization):
        """
        One engine call for up to MAX_GROUP_STARTS starts.

        Returns (None, entries) -- one per start, in order -- or (status,
        body) for a refusal to forward as it is.
        """
        minutes = []
        for start in batch:
            asked_day, minute = gt.to_engine(start, clock)
            # offered_starts() put every start on this one engine day. If
            # that ever stops being true, asking would plan the wrong date.
            if asked_day != engine_day:
                logger.error(
                    "group start %s falls on engine day %s, not %s",
                    start.isoformat(), asked_day, engine_day,
                )
                raise BookingApiDown()
            minutes.append(minute)

        try:
            code, planned = plan_group(
                gt.plan_body(route["branch_id"], engine_day, minutes, members, order),
                authorization=authorization,
                tenant_id=route["tenant_id"],
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc

        if code not in (200, 201):
            if code == 400:
                # booking-api refusing a body THIS service built is a
                # translation bug here, not a mistake by the app.
                logger.warning("booking-api refused a group plan body: %r", planned)
            return code, planned

        entries = planned.get("starts") if isinstance(planned, dict) else None
        if not isinstance(entries, list) or len(entries) != len(batch):
            # Guessing which entry belongs to which start would put a party
            # at the wrong time.
            logger.warning("booking-api answered %r for %d starts", planned, len(batch))
            raise BookingApiDown()
        return None, entries


# ------------------------------------------------------------ booking


class SlotTaken(APIException):
    """409 slot_taken: the party no longer fits at that time. Nothing was booked."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "That time no longer fits the whole party. Pick another time."
    default_code = "slot_taken"


def _engine_sentence(body):
    """booking-api's refusal in words, when it sent one worth showing."""
    message = body.get("message") if isinstance(body, dict) else None
    return message if isinstance(message, str) and message.strip() else None


class GroupRequestInProgress(APIException):
    """409: the same group booking is already being made by another request."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = (
        "This group booking is already being made. Wait a moment, then check "
        "your bookings."
    )
    default_code = "request_in_progress"


def _status_unknown(group_id):
    """
    503 group_status_unknown, in this project's envelope plus `group_id`.

    Returned rather than raised: the envelope handler rebuilds every raised
    error into {detail, code, errors} and would drop the id -- which is the
    one thing the customer, or support, needs to find the party.
    """
    return Response(
        {
            "detail": _STATUS_UNKNOWN,
            "code": "service_unavailable",
            "errors": [
                {"field": None, "code": "group_status_unknown", "message": _STATUS_UNKNOWN}
            ],
            "group_id": group_id,
        },
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def _receipt_key(request):
    """
    One Redis key for the whole hold-then-confirm pair.

    Built on the same key POST /booking uses -- the caller's Idempotency-Key,
    or a hash of the customer and the body they sent -- and hashed again with
    the customer's id, because a key the CALLER chose carries no customer:
    two customers who both sent "abc" must not share a receipt.
    """
    digest = hashlib.sha256()
    digest.update(_RECEIPT_NAMESPACE.encode())
    digest.update(str(getattr(request.user, "id", "")).encode())
    digest.update(_idempotency_key(request).encode())
    return f"group-booking:{digest.hexdigest()}"


def _fingerprint(request):
    return hashlib.sha256(request.body or b"").hexdigest()


def _claim(key, fingerprint):
    """
    Mark the key in progress. None when this request now owns it, the stored
    receipt when another request got there first.

    REDIS DOWN IS NOT A REFUSAL. The booking goes ahead unprotected and the
    failure is logged, the rule booking-api's own idempotency store follows:
    refusing every group booking during a cache outage would break working
    requests to guard against a retry that may never come.
    """
    mark = {"state": "in_progress", "fingerprint": fingerprint}
    try:
        for _ in range(2):
            if cache.add(key, mark, timeout=IN_PROGRESS_SECONDS):
                return None
            existing = cache.get(key)
            if existing is not None:
                return existing
            # Expired between the two calls: claim it again.
    except Exception:  # noqa: BLE001 -- any cache failure means the same thing
        logger.warning("group booking receipts unavailable; booking unprotected", exc_info=True)
    return None


def _remember(key, receipt):
    try:
        cache.set(key, receipt, timeout=RECEIPT_SECONDS)
    except Exception:  # noqa: BLE001
        # The booking already happened. Failing the response now would send
        # the app into the very retry this was meant to absorb.
        logger.warning("could not store a group booking receipt", exc_info=True)


def _forget(key):
    try:
        cache.delete(key)
    except Exception:  # noqa: BLE001
        logger.warning("could not clear a group booking mark", exc_info=True)


def _replay(receipt, fingerprint):
    """The answer a stored receipt gives to a request that finds it."""
    if receipt.get("fingerprint") != fingerprint:
        # Only a caller-chosen key can land here: a derived key IS the body.
        raise ValidationError(ErrorDetail(
            "This Idempotency-Key was already used for a different group booking.",
            code="idempotency_key_reused",
        ))
    state = receipt.get("state")
    if state == "done":
        return Response(receipt["body"], status=receipt["status"])
    if state == "unknown":
        return _status_unknown(receipt.get("group_id"))
    raise GroupRequestInProgress()


_MEMBER_EXAMPLE = {
    "ref": 0,
    "id": None,
    "booking_code": "GS-1280",
    "user_id": "11111111-1111-4111-8111-111111111111",
    "name": "Sarah Kassem",
    "kind": "self",
    "age_group": "adult",
    "services": [{"id": "0b7c…", "name": "Signature Fade", "amount": 250.0}],
    "products": [],
    "stylist": {"id": "7d1e…", "name": "Anna Petrova"},
    "start_time": "2026-10-11T15:00:00+04:00",
    "end_time": "2026-10-11T16:00:00+04:00",
    "total": 250.0,
}


@extend_schema(
    summary="Book the whole party",
    description=(
        "Holds every member's lane at `start_time`, then confirms them: one "
        "booking per member at the salon's desk, everyone arriving together, "
        "or nothing at all.\n\n"
        "`self` is the signed-in customer; `registered` members are booked "
        "on their own accounts, `guest`s by name. Every figure is the "
        "server's: prices from the salon's own records, no VAT, discount, "
        "child price or deposit (booking-api has none for a group), and "
        "nothing paid in the app -- `payment_status` is PAY_AFTER_CHECK_IN.\n\n"
        "RETRY-SAFE WITHOUT A HEADER. The same request sent again within 24 "
        "hours replays the first answer rather than booking a second party; "
        "sent while the first is still running it is 409 "
        "`request_in_progress`. A refusal is not remembered, so fixing the "
        "request and sending it again works. See docs/BOOKING_GROUP_API.md, "
        "including what a group booking does NOT carry (§8)."
    ),
    request=GroupBookingRequestSerializer,
    responses={
        201: OpenApiResponse(
            description="Booked. One booking code per member.",
            examples=[
                OpenApiExample(
                    "Booked",
                    value={
                        "id": "b2c6b0a9-17aa-4be8-91b2-11668d3b2e5a",
                        "salon_id": "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5",
                        "booking_type": "GROUP",
                        "status": "CONFIRMED_BY_SALON",
                        "payment_status": "PAY_AFTER_CHECK_IN",
                        "date": "2026-10-11",
                        "start_time": "2026-10-11T15:00:00+04:00",
                        "end_time": "2026-10-11T16:00:00+04:00",
                        "members": [_MEMBER_EXAMPLE],
                        "currency": "AED",
                        "amount_without_tax": 250.0,
                        "tax_amount": None,
                        "discount": 0.0,
                        "promo_code": None,
                        "total": 250.0,
                        "deposit_percent": 0,
                        "deposit_amount": 0.0,
                        "advance_paid_amount": 0.0,
                        "due_amount": 250.0,
                        "pass_qr_code": None,
                        "expires_at": None,
                        "created_at": "2026-10-09T14:02:11+04:00",
                    },
                )
            ],
        ),
        404: OpenApiResponse(response=_OUR_ENVELOPE, description="No such salon."),
        409: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description=(
                "`slot_taken`: the party no longer fits at that time, and "
                "nothing was booked. `request_in_progress`: the same request "
                "is still being made."
            ),
        ),
        415: OpenApiResponse(response=_OUR_ENVELOPE, description="Not application/json."),
        422: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description=(
                _PARTY_422 + " Also `invalid_member_kind` (not exactly one "
                "`self`, `self` not the signed-in account, or one account "
                "twice), `member_id_required`, `unknown_user`, "
                "`products_not_supported`, `packages_not_supported`; on "
                "`start_time`: `offset_required`, `date_mismatch`, "
                "`salon_closed`, `too_soon`, `outside_hours`; and "
                "`idempotency_key_reused`."
            ),
        ),
        503: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description=(
                "`booking_api_unavailable`: nothing was booked, and a retry is "
                "safe. `group_status_unknown`: booking-api stopped answering "
                "after the party was held, and it may have been booked -- the "
                "body carries `group_id`, and the app should send the "
                "customer to their bookings rather than retry."
            ),
        ),
    },
)
class GroupBookingCreateView(APIView):
    """POST /api/v1/booking/group"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        _json_only(request)
        # FIRST, before request.data: DRF parses by reading the stream, and
        # Django refuses request.body once the stream has been read. The key
        # and the fingerprint are both taken from the bytes the app sent.
        key, fingerprint = _receipt_key(request), _fingerprint(request)

        data = _validated(GroupBookingRequestSerializer, request, booker_id=request.user.id)
        salon, route = _salon_and_route(data["salon_id"])
        members = data["members"]
        _name_members(request.user, members)
        rows = _service_rows(salon, members, "services")
        roster = _roster(salon)
        _check_stylists(salon, members, roster)

        tz = timezones.resolve(salon.branch_timezone, salon.id)
        start = data["start_time"]
        if start.astimezone(tz).date() != data["date"]:
            raise _refuse(
                "start_time",
                "start_time must fall on `date` in the salon's own time.",
                "date_mismatch",
            )

        existing = _claim(key, fingerprint)
        if existing is not None:
            return _replay(existing, fingerprint)

        try:
            kind, code, body = self._book(request, data, salon, route, tz, rows, roster)
        except Exception:
            # A refusal, or booking-api unreachable before anything was
            # booked. Nothing to remember: the same request may be sent again
            # as it is. (A worker killed mid-request skips this on purpose;
            # its mark lapses after IN_PROGRESS_SECONDS.)
            _forget(key)
            raise

        if kind == "done":
            _remember(key, {"state": "done", "fingerprint": fingerprint, "status": code, "body": body})
            return Response(body, status=code)
        if kind == "unknown":
            # Remembered, so a retry hears the same thing instead of holding
            # a second party beside one that may already be booked.
            _remember(key, {"state": "unknown", "fingerprint": fingerprint, "group_id": body})
            return _status_unknown(body)

        _forget(key)
        return Response(body, status=code)

    def _book(self, request, data, salon, route, tz, rows, roster):
        """
        Hold, then confirm. Returns (kind, status, body).

        kind is "done" (booked: status 201 and the app's body), "refused"
        (booking-api's answer, forwarded as it is) or "unknown" (body is the
        group id). Raises for anything this service refuses itself,
        `slot_taken` included.
        """
        authorization = request.META.get("HTTP_AUTHORIZATION", "")
        members = data["members"]
        start, day = data["start_time"], data["date"]
        day_start = datetime.combine(day, time.min, tzinfo=tz)

        longest, lead = _party_timing(rows, members)
        clock = _clock(authorization, route)
        engine_day, minute = gt.to_engine(start, clock)

        refusal = gt.start_refusal(
            start,
            _open_span(salon, tz, day, day_start),
            gt.engine_span(date.fromisoformat(engine_day), clock),
            earliest=_now(tz) + timedelta(minutes=lead),
            longest_minutes=longest,
        )
        if refusal is not None:
            code, message = refusal
            raise _refuse("start_time", message, code)

        order = gt.booker_first(members)
        tenant = route["tenant_id"]

        # ---- hold. Every lane or none.
        try:
            code, held = hold_group(
                gt.hold_body(route["branch_id"], engine_day, minute, members, order),
                authorization=authorization, tenant_id=tenant,
            )
        except BookingApiUnavailable as exc:
            # Unknown whether a hold was placed; if it was, nothing was
            # booked on it and it lapses on its own in fifteen minutes.
            raise BookingApiDown() from exc
        if code == status.HTTP_409_CONFLICT:
            # The party does not fit at that time. booking-api says why in
            # words ("Only 1 professional can cover this party"), worth
            # showing as they are.
            raise SlotTaken(_engine_sentence(held))
        if code not in (200, 201):
            if code == 400:
                logger.warning("booking-api refused a group hold body: %r", held)
            return "refused", code, held

        group_id = (held or {}).get("groupId")
        hold_id = (held or {}).get("holdId")
        if not group_id or not hold_id:
            logger.error("booking-api held a group without naming it: %r", held)
            raise BookingApiDown()

        # ---- confirm. Anything but a booked party gives the lanes back.
        try:
            code, confirmed = confirm_group(
                group_id, gt.confirm_body(hold_id, members, order),
                authorization=authorization, tenant_id=tenant,
            )
        except BookingApiUnavailable:
            # The one ambiguous moment: the confirm may have landed. Releasing
            # is safe either way and its answer says which side we are on --
            # `released: true` means the hold was still there, so nothing was
            # booked. Anything else, and the party may exist.
            if self._release(hold_id, authorization, tenant) is True:
                raise BookingApiDown() from None
            logger.warning(
                "group %s: confirm unanswered and the hold was not released; status unknown",
                group_id,
            )
            return "unknown", None, group_id

        if code not in (200, 201):
            # Nothing was written: booking-api's confirm is one transaction.
            # Free the party's professionals now rather than in fifteen
            # minutes, or a retry competes with its own leftover hold.
            self._release(hold_id, authorization, tenant)
            if code == status.HTTP_409_CONFLICT:
                # Re-planned on confirm and no longer fits.
                raise SlotTaken(_engine_sentence(confirmed))
            if code == status.HTTP_410_GONE:
                # The hold lapsed between the two calls. Nothing was booked,
                # and what the customer can do is the same: pick again.
                raise SlotTaken()
            return "refused", code, confirmed

        try:
            body, problems = gt.booking_from_confirm(
                held, confirmed or {},
                day_iso=engine_day, members=members, order=order, clock=clock,
                tz=tz, cards=roster, catalogue=_catalogue(rows),
                salon_id=str(salon.id), date_iso=day.isoformat(),
                created_at=_now(tz).replace(microsecond=0).isoformat(),
            )
        except Exception:  # noqa: BLE001 -- the party is booked; nothing may undo that
            # Booked, but in a shape this cannot read. Saying so beats
            # saying "failed", which the app would retry into a second party.
            logger.error("group %s confirmed but its answer is unreadable: %r",
                         group_id, confirmed, exc_info=True)
            return "unknown", None, group_id
        if problems:
            logger.error("group %s confirmed with unreadable parts: %s", group_id, problems)

        return "done", status.HTTP_201_CREATED, body

    @staticmethod
    def _release(hold_id, authorization, tenant):
        """
        True when THIS call freed the hold. False, or None, otherwise.

        Best effort: a failure here is logged and the hold lapses on its own.
        """
        try:
            code, body = release_group_hold(
                hold_id, authorization=authorization, tenant_id=tenant
            )
        except BookingApiUnavailable:
            logger.warning("could not release group hold %s; it lapses on its own", hold_id)
            return None
        if code != 200 or not isinstance(body, dict):
            logger.warning("releasing group hold %s answered %s", hold_id, code)
            return None
        return body.get("released")

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
from datetime import date, datetime, time, timedelta, timezone as dt_timezone

from django.conf import settings
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
from apps.accounts.privacy import name_for_salons

from . import group_translate as gt
from . import timezones
from .booking_api import (
    BookingApiUnavailable,
    cancel_group_booking,
    confirm_group,
    create_group_booking,
    engine_clock,
    hold_group,
    plan_group,
    read_group_booking,
    release_group_hold,
)
from .group_serializers import (
    REGISTERED,
    SELF,
    GroupAvailabilityRequestSerializer,
    GroupBookingRequestSerializer,
)
from .menu import service_price
from .money import major
from .selectors import (
    booking_route,
    salon_cards_for_refs,
    salon_profile,
    salon_stylists,
    service_timing_rows,
    stylist_service_coverage,
)
from .views import (
    _OUR_ENVELOPE,
    BookingApiDown,
    _as_parts,
    _bookable_packages,
    _check_in_fields,
    _idempotency_key,
    _member_check_in_fields,
    _open_span,
    _salon_card,
    service_minutes,
)

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


def _visits(salon, members, field):
    """
    What each member's visit performs, for this service's own checks: with
    PACKAGE_BOOKING_V1 on, a package among a member's services counts as its
    parts. Returns `(visits, packages)`.

    `visits` are copies of the members whose `service_ids` are the services
    performed: a package's parts in its place, each as many times as the
    package holds it ("2 x Cut" is two cuts), read by the slot search's own
    expansion (views._bookable_packages), once for the whole party. The
    foreign_id check, the stylist who must do every part, the party's length
    (each member's parts end to end) and its notice all read these.
    `packages` is what was found, `{id: (content, parts)}`, so the old path
    can name and price a package's line.

    THE MEMBERS KEEP THEIR LINES AS SENT. booking-api expands a package
    itself (party-member.ts, on availability, hold, confirm and its mobile
    route), so it is sent the package id, never the parts.

    A package of this salon's business that packages.can_book refuses is 422
    package_not_bookable on `field`. Any other id is left as sent, and the
    foreign_id check answers it as before. Off, or no package in the party:
    the members themselves, and with the switch off nothing is read.
    """
    if not settings.PACKAGE_BOOKING_V1:
        return members, {}
    ids = list(dict.fromkeys(sid for m in members for sid in m["service_ids"]))
    packages = _bookable_packages(salon, ids, field=field)
    if not packages:
        return members, {}
    visits = [
        {**m, "service_ids": [str(sid) for sid in _as_parts(m["service_ids"], packages)]}
        for m in members
    ]
    return visits, packages


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


def _who_can_do(salon, members, staff_ids):
    """
    {staff id: the services of `members`' visits that person can do alone},
    for `staff_ids`, measured exactly as the Expert step measures it (GET
    /salon/<id>/stylists?service_ids=, stylist_service_coverage): one person
    holding every skill a service needs. One read for all of them. A staff
    id that is not a uuid is shown to do nothing.
    """
    staff = [s for s in dict.fromkeys(gt.staff_key(s) for s in staff_ids) if s]
    service_ids = sorted({sid for m in members for sid in m["service_ids"]})
    if not staff or not service_ids:
        return {}
    coverage = stylist_service_coverage(
        salon, [uuid.UUID(s) for s in service_ids], [uuid.UUID(s) for s in staff]
    )
    return {str(who): {str(s) for s in done} for who, done in coverage.items()}


def _able(covered):
    """`can_do(member, staff_id)` over `_who_can_do`'s answer: every service of the member's visit."""
    def can_do(member, staff_id):
        return staff_id is not None and set(member["service_ids"]) <= covered.get(staff_id, set())
    return can_do


def _checks_picks():
    """
    Are booking-api's own picks checked: who it gives a member with no chosen
    stylist (GroupAvailabilityView._qualified, the old path's
    _qualified_hold, and after the fact on GROUP_BOOKING_V2).

    With PACKAGE_BOOKING_V1, which brings them: a package is often more than
    one person's work (a cut and nails), and booking-api checks no skills.
    Off, every party is planned, held and confirmed exactly as before.
    """
    return settings.PACKAGE_BOOKING_V1


def _check_stylists(salon, members, roster):
    """
    Every stylist the app chose: at this salon, once each, and able to do
    all of that member's services.

    `foreign_id` for one off the roster. `stylist_repeated` for one chosen
    for two members: everyone starts together, and booking-api gives every
    member their own professional, so that party fits at no time at all --
    better said now than as a day of struck-through slots. `stylist_mismatch`
    for one who cannot do the member's work, measured exactly as the Expert
    step measures it (`_who_can_do`).

    NOT booking-api's check too. It gives each member one professional for
    all of their services, but checks no skills for the platform's services
    (SKILLS_UNVERIFIED counts every professional as qualified), so this is
    the only check a chosen stylist gets. A member with no chosen stylist is
    given whoever booking-api finds free: with PACKAGE_BOOKING_V1 on, its
    picks are checked the same way once it answers (`_checks_picks`).
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

    can_do = _able(_who_can_do(salon, chosen, picked))
    for m in chosen:
        if not can_do(m, m["stylist_id"]):
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
    # The name the salon may have: these become the party's lanes at
    # booking-api, which the desk reads ("Show my name to salons").
    return {
        str(pk): name_for_salons(name, show)
        for pk, name, show in ConsumerAccount.objects.filter(
            id__in=wanted, is_active=True, account_verified=True
        ).values_list("id", "full_name", "show_name_to_salons")
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
            m["name"] = (
                name_for_salons(
                    getattr(booker, "full_name", ""),
                    getattr(booker, "show_name_to_salons", True),
                )
                or m["name"]
            )
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

    The same figures the single-booking picker uses (service_minutes): the
    platform's staged minutes where a service has stages, its catalogue
    duration otherwise, or with SLOT_MINUTES_AS_BOOKED_V1 on the catalogue's
    minutes booking-api books; and the longest lead time any chosen service
    asks for.
    """
    minutes = {sid: service_minutes(row) for sid, row in rows.items()}
    longest = max(gt.member_minutes(members, minutes))
    lead = max([row["lead_time_minutes"] or 0 for row in rows.values()] or [0])
    return longest, lead


def _catalogue(rows, packages=None):
    """
    Each chosen service's name and price, as the services tab shows them
    (menu.service_price: the branch price when set, as booking-api charges).

    And each chosen package's (`_visits`, PACKAGE_BOOKING_V1): the name and
    price the Package Details screen shows. A package is one line at its
    price, which is what booking-api charges for it, its parts carrying the
    price between them.
    """
    catalogue = {
        sid: {
            "name": row.get("name"),
            "price": service_price(row),
        }
        for sid, row in rows.items()
    }
    for pid, (content, _parts) in (packages or {}).items():
        catalogue[pid] = {"name": content["name"], "price": major(content["price_minor"])}
    return catalogue


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
    "services, a package's every part included), and with PACKAGE_BOOKING_V1 "
    "on `package_not_bookable` (a package of this salon that cannot be booked "
    "here now)."
)


@extend_schema(
    summary="Every start of the day, and whether the whole party fits",
    description=(
        "Every half-hour start the salon offers the party on `date`, in "
        "Morning / Afternoon / Evening bands. A start is `available` only if "
        "every member fits; then `plan` says who is with whom, and when. "
        "With PACKAGE_BOOKING_V1 on, every stylist in a `plan` can do all of "
        "that member's services, a package's included: booking-api does not "
        "check skills, so its picks are checked here, and a start with "
        "nobody free who can is unavailable. Starts already gone, or inside "
        "the services' notice, are offered but unavailable.\n\n"
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
        # What each member performs, a package as its parts. booking-api is
        # still asked about `members`, the ids as sent: it expands a package
        # itself.
        visits, _ = _visits(salon, members, "service_ids")
        rows = _service_rows(salon, visits, "service_ids")
        roster = _roster(salon)
        _check_stylists(salon, visits, roster)

        tz = timezones.resolve(salon.branch_timezone, salon.id)
        day = data["date"]
        day_start = datetime.combine(day, time.min, tzinfo=tz)
        answer = {"date": day.isoformat(), "bands": []}

        open_span = _open_span(salon, tz, day, day_start)
        if open_span is None:
            # Asked of nobody: a closed salon offers no starts.
            return Response(answer)

        longest, lead = _party_timing(rows, visits)
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
            if _checks_picks():
                answers = self._qualified(
                    salon, answers, visits, members, order, engine_day, clock, route, authorization
                )
            found = gt.day_slots(
                offered, answers, engine_day, members, order, clock, tz, roster, longest
            )
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("booking-api group plan answer unreadable: %s", exc)
            raise BookingApiDown() from exc

        return Response({**answer, "bands": gt.banded(offered, found, day_start)})

    def _qualified(self, salon, answers, visits, members, order, engine_day, clock, route,
                   authorization):
        """
        `answers` with every start booking-api says fits checked, lane by
        lane: each member given someone who can do all of that member's
        services (`_who_can_do`, a package's every part included), or the
        start is not available.

        booking-api checks no skills for the platform's services, so a member
        with no chosen stylist gets whoever it finds free first, and the same
        roster order gives the same wrong pick at every start where that
        person is free. Striking those starts would strike most of a day a
        party can be served on, so a start whose plan is wrong is first
        shared out again among the professionals booking-api planned there
        (gt.qualified_assignment: they are free at that start) and asked
        again with every member pinned to their share: only booking-api
        knows whether someone free for a cut is free for a longer package.
        The starts one sharing fixes are asked in one call. A start with no
        sharing, or that does not fit pinned, is unavailable; a plan that is
        already right is kept as it came, and nothing more is asked.

        `visits` are the members as performed (`_visits`); `members` are as
        the app sent them, which is what booking-api is asked about.
        """
        picks = {
            start: gt.lane_staff(entry.get("lanes"), order)
            for start, entry in answers.items()
            if isinstance(entry, dict) and entry.get("feasible")
        }
        if not picks:
            return answers
        can_do = _able(_who_can_do(salon, visits, [s for staff in picks.values() for s in staff]))

        checked, repairs = dict(answers), {}
        for start, staff in picks.items():
            if all(can_do(m, s) for m, s in zip(visits, staff)):
                continue
            checked[start] = {"feasible": False}
            pins = gt.qualified_assignment(visits, staff, can_do)
            if pins is not None:
                repairs.setdefault(tuple(pins), []).append(start)

        for pins, starts in repairs.items():
            pinned = [{**m, "stylist_id": pin} for m, pin in zip(members, pins)]
            for batch in gt.batches(sorted(starts)):
                code, entries = self._plan(batch, engine_day, clock, route, pinned, order, authorization)
                if code is not None:
                    # The same party with its stylists named: nothing new to
                    # refuse. Those starts stay unavailable.
                    logger.warning("booking-api refused a group plan with its stylists pinned: %s %r",
                                   code, entries)
                    continue
                for start, entry in zip(batch, entries):
                    if (isinstance(entry, dict) and entry.get("feasible")
                            and gt.lane_staff(entry.get("lanes"), order) == list(pins)):
                        checked[start] = entry
        return checked

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


# slot_taken's words when booking-api held the party with someone who cannot
# do a member's services and nobody it held can (GroupBookingCreateView._qualified_hold).
NO_ABLE_STYLIST = (
    "There is no stylist free at that time who can do all of each member's "
    "services. Pick another time."
)


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
        idempotency_key = _idempotency_key(request)

        data = _validated(GroupBookingRequestSerializer, request, booker_id=request.user.id)
        salon, route = _salon_and_route(data["salon_id"])
        members = data["members"]
        _name_members(request.user, members)
        # What each member performs, a package as its parts, named: the
        # checks read `visits`, and booking-api is sent `members`, the lines
        # as the app sent them.
        visits, packages = _visits(salon, members, "services")
        rows = _service_rows(salon, visits, "services")
        roster = _roster(salon)
        _check_stylists(salon, visits, roster)

        tz = timezones.resolve(salon.branch_timezone, salon.id)
        start = data["start_time"]
        if start.astimezone(tz).date() != data["date"]:
            raise _refuse(
                "start_time",
                "start_time must fall on `date` in the salon's own time.",
                "date_mismatch",
            )

        if settings.GROUP_BOOKING_V2:
            # One call to booking-api's mobile route. Every check above has
            # already run, exactly as for the old path.
            return self._book_v2(request, data, salon, route, tz, rows, idempotency_key, visits)

        existing = _claim(key, fingerprint)
        if existing is not None:
            return _replay(existing, fingerprint)

        try:
            kind, code, body = self._book(
                request, data, salon, route, tz, rows, roster, visits, packages
            )
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

    def _book(self, request, data, salon, route, tz, rows, roster, visits, packages):
        """
        Hold, then confirm. Returns (kind, status, body).

        kind is "done" (booked: status 201 and the app's body), "refused"
        (booking-api's answer, forwarded as it is) or "unknown" (body is the
        group id). Raises for anything this service refuses itself,
        `slot_taken` included.

        `visits` and `packages` come from `_visits`: the party's length and
        notice are read from the parts, the professionals held are checked
        against them (`_qualified_hold`), and a package's line is named and
        priced as one line, at the package's price.
        """
        authorization = request.META.get("HTTP_AUTHORIZATION", "")
        members = data["members"]
        start, day = data["start_time"], data["date"]
        day_start = datetime.combine(day, time.min, tzinfo=tz)

        longest, lead = _party_timing(rows, visits)
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
        branch = route["branch_id"]

        # ---- hold. Every lane or none.
        held, refused = self._hold(
            gt.hold_body(branch, engine_day, minute, members, order), authorization, tenant
        )
        if refused is not None:
            return ("refused", *refused)

        # ---- who it holds, checked: booking-api checks no skills.
        confirming, staff = members, None
        if _checks_picks():
            held, refused, staff = self._qualified_hold(
                salon, held, visits, members, order, branch, engine_day, minute,
                authorization, tenant,
            )
            if refused is not None:
                return ("refused", *refused)
            # Every member pinned to the professional held for them:
            # booking-api re-plans on confirm, and left to itself it could
            # move a member to someone else free, checked by nobody.
            confirming = [{**m, "stylist_id": who} for m, who in zip(members, staff)]
        group_id, hold_id = held["groupId"], held["holdId"]

        # ---- confirm. Anything but a booked party gives the lanes back.
        try:
            code, confirmed = confirm_group(
                group_id, gt.confirm_body(hold_id, confirming, order),
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
                tz=tz, cards=roster, catalogue=_catalogue(rows, packages),
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
        if staff is not None:
            try:
                booked_with = gt.lane_staff((confirmed or {}).get("bookings"), order)
            except ValueError:
                booked_with = None
            if booked_with != staff:
                # Booked, so nothing to undo; but not with the professionals checked.
                logger.error("group %s confirmed with stylists other than the ones it held: %r",
                             group_id, confirmed)

        return "done", status.HTTP_201_CREATED, body

    @staticmethod
    def _hold(body, authorization, tenant):
        """
        One hold. Returns `(held, None)`, or `(None, (status, body))` for
        booking-api's refusal to forward as it is. Raises SlotTaken when the
        party does not fit, and BookingApiDown when booking-api cannot be
        reached or names no hold.
        """
        try:
            code, held = hold_group(body, authorization=authorization, tenant_id=tenant)
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
            return None, (code, held)
        if not (held or {}).get("groupId") or not (held or {}).get("holdId"):
            logger.error("booking-api held a group without naming it: %r", held)
            raise BookingApiDown()
        return held, None

    def _qualified_hold(self, salon, held, visits, members, order, branch, engine_day, minute,
                        authorization, tenant):
        """
        The hold with each member's professional checked, as `(held, refused,
        staff)`: `staff` is who it holds for each member, in the app's order,
        and `refused` booking-api's refusal to forward when there is one.

        booking-api checks no skills for the platform's services
        (`_check_stylists`), so a member with no chosen stylist can be held
        with someone who cannot do all of their services. Then that hold is
        given back, and the party held again once, with the professionals it
        held shared out so each member's can (gt.qualified_assignment) and
        every member pinned to theirs: the same people at the same time,
        booking-api deciding whether they are free for the lanes they now
        take. No such sharing, or no fit pinned, is slot_taken: nothing is
        booked. A hold that is already right is kept as it came.

        A hold whose lanes cannot be read is given back too, and 503: a party
        this cannot check is not confirmed.
        """
        try:
            staff = gt.lane_staff(held.get("lanes"), order)
        except ValueError:
            logger.error("booking-api held a group without a lane per member: %r", held)
            self._release(held["holdId"], authorization, tenant)
            raise BookingApiDown() from None

        can_do = _able(_who_can_do(salon, visits, staff))
        if all(can_do(m, who) for m, who in zip(visits, staff)):
            return held, None, staff

        self._release(held["holdId"], authorization, tenant)
        pins = gt.qualified_assignment(visits, staff, can_do)
        if pins is None:
            raise SlotTaken(NO_ABLE_STYLIST)

        pinned = [{**m, "stylist_id": pin} for m, pin in zip(members, pins)]
        held, refused = self._hold(
            gt.hold_body(branch, engine_day, minute, pinned, order), authorization, tenant
        )
        if refused is not None:
            return None, refused, None
        try:
            again = gt.lane_staff(held.get("lanes"), order)
        except ValueError:
            again = None
        if again != pins:
            logger.error("booking-api held group %s with other stylists than were pinned: %r",
                         held.get("groupId"), held)
            self._release(held["holdId"], authorization, tenant)
            raise SlotTaken(NO_ABLE_STYLIST)
        return held, None, pins

    def _book_v2(self, request, data, salon, route, tz, rows, idempotency_key, visits):
        """
        GROUP_BOOKING_V2: the party in ONE call to POST
        /v1/mobile-booking/group, saved to pay at the salon.

        The start is checked here first, against the salon's hours and the
        services' notice, the same as the old path. Then booking-api holds,
        prices (child price, VAT, products, deposit) and books every member,
        or none. Its answer is the app's shape already; only the salon, the
        clock and the date are put back to the salon's.

        A package is a line of a member's `services`, sent on as it came
        (gt.mobile_group_body): booking-api expands and prices it. Only the
        start's checks read `visits`, the parts (`_visits`).

        RETRY-SAFE through booking-api's own Idempotency-Key store: the same
        key POST /booking derives (the caller's, or a hash of the customer
        and the body), so sending the same party again returns the party
        already booked rather than a second one. No Redis receipt is needed
        for one call.
        """
        authorization = request.META.get("HTTP_AUTHORIZATION", "")
        members = data["members"]
        start, day = data["start_time"], data["date"]
        day_start = datetime.combine(day, time.min, tzinfo=tz)

        longest, lead = _party_timing(rows, visits)
        clock = _clock(authorization, route)
        engine_day, _ = gt.to_engine(start, clock)
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

        try:
            code, answer = create_group_booking(
                gt.mobile_group_body(route["branch_id"], start, members, request.data),
                authorization=authorization,
                idempotency_key=idempotency_key,
                tenant_id=route["tenant_id"],
            )
        except BookingApiUnavailable as exc:
            # Safe to send again: the same key answers with the party if
            # this one did land.
            raise BookingApiDown() from exc

        if code != status.HTTP_201_CREATED or not isinstance(answer, dict):
            if code == 400:
                logger.warning("booking-api refused a mobile group body: %r", answer)
            return Response(answer, status=code)
        if _checks_picks():
            self._log_unable_stylists(salon, answer, visits)
        return Response(gt.present_group(answer, salon_id=salon.id, tz=tz), status=code)

    @staticmethod
    def _log_unable_stylists(salon, answer, visits):
        """
        booking-api's own picks on its mobile route, checked once the party
        is booked: an ERROR for each member with no chosen stylist who was
        booked with someone unable to do all of their services, for the
        salon to put right.

        Only after, and only logged. That route holds and confirms in one
        call, and booking-api checks no skills for the platform's services
        (`_check_stylists`). Checking first and pinning the stylists would
        send a retry a different body under the same Idempotency-Key, which
        booking-api refuses instead of answering with the party already
        booked. The app books the stylists availability's `plan` showed,
        checked, by sending them as `stylist_id`.

        Never fails the answer: the party is booked.
        """
        try:
            by_ref = {m.get("ref"): m for m in answer.get("members") or [] if isinstance(m, dict)}
            booked = []
            for member in visits:
                if member["stylist_id"]:
                    continue
                stylist = (by_ref.get(member["ref"]) or {}).get("stylist")
                who = gt.staff_key(stylist.get("id")) if isinstance(stylist, dict) else None
                if who is not None:
                    booked.append((member, who))
            if not booked:
                return
            can_do = _able(_who_can_do(salon, [m for m, _ in booked], [who for _, who in booked]))
            for member, who in booked:
                if not can_do(member, who):
                    logger.error(
                        "group %s: member %s was booked with stylist %s, who cannot do all of "
                        "their services (booking-api checks no skills for the platform's)",
                        answer.get("id"), member["ref"], who,
                    )
        except Exception:  # noqa: BLE001 -- the party is booked; nothing may undo that
            logger.warning("could not check the stylists of group %s", answer.get("id"),
                           exc_info=True)

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


# ------------------------------------------------------------ read and cancel


def _account_names(user_ids):
    """{user_id: full_name} for the accounts in a party, for display."""
    wanted = []
    for raw in user_ids:
        try:
            wanted.append(uuid.UUID(str(raw)))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return {}
    return {
        str(pk): name
        for pk, name in ConsumerAccount.objects.filter(id__in=wanted).values_list("id", "full_name")
    }


def _stored_party(body):
    """
    A party booking-api has stored, as the app reads it: the salon card, the
    salon's own clock, and the account names booking-api does not keep.
    """
    ref = body.get("salon_id")
    card = salon_cards_for_refs([ref]).get(ref) if isinstance(ref, str) else None
    # The card carries the salon's own zone, so no second lookup.
    tz = timezones.resolve(card.get("timezone"), card["id"]) if card else None
    names = _account_names([m.get("user_id") for m in body.get("members") or []])
    out = gt.present_group(body, salon_id=card["id"] if card else None, tz=tz, names=names)
    out["salon"] = _salon_card(card, full=True)
    return out


def _member_check_in(party, now):
    """
    GROUP SELF CHECK-IN on the party's read (GROUP_SELF_CHECK_IN_V1): may the
    caller check in on their own lane now, and when does that open. The
    lane's id is booking-api's `booking_id` on the same row, passed through
    as it came.

    ALL THREE, or nothing is added: SELF_CHECK_IN_V1 (the route the member
    uses), GROUP_SELF_CHECK_IN_V1, and booking-api's own switch, seen as
    `is_me` on every member row (its MOBILE_GROUP_SELF_CHECK_IN puts the key
    on every row or on none). Without it this service does not guess which
    row is the caller's.

    Then EVERY member row gets all three keys (can_check_in,
    check_in_opens_at, can_scan_chair), one shape for all: the rule's answer
    on the `is_me` row (views._member_check_in_fields), false, null and false
    on the rest. `is_me` is read strictly, `is True`, as `is_booker` is.

    NEVER THE BOOKER'S OWN ROW (`kind: "self"`): false and null there,
    whatever the window says. The booker's way is We are here, which asks for
    every member, guests included. A tap on the booker's lane alone would,
    once approved, take the GROUP row's button away (it follows the booker's
    lane), and the guests could not be checked in from the app at all. The
    server decides it, so no app build can forget. `is_me` and `booking_id`
    on that row are booking-api's facts and are left as they came.
    """
    if not (settings.SELF_CHECK_IN_V1 and settings.GROUP_SELF_CHECK_IN_V1):
        return party
    members = party.get("members")
    if not isinstance(members, list) or not members:
        return party
    if not all(isinstance(m, dict) and "is_me" in m for m in members):
        return party
    for m in members:
        if m.get("is_me") is True and m.get("kind") != "self":
            m.update(_member_check_in_fields(m, now))
        else:
            m.update({"can_check_in": False, "check_in_opens_at": None, "can_scan_chair": False})
    return party


def _booker_check_in(party, caller_id, now):
    """
    GROUP CHECK-IN at the top of the party's read (GROUP_CHECK_IN_V1): the
    booker's We are here, so the party screen can draw it however it was
    opened (from My Bookings, a push, a deep link, after the app was killed).

    `is_booker`: the caller is the party's `self` member, the one booking-api
    files as the organiser, which is what the My Bookings GROUP row's
    `is_booker` says too. Worked out here: booking-api's party read carries no
    such key. Read strictly: exactly one `self` row, with the caller's id.

    Then can_check_in, check_in_opens_at and can_scan_chair from THE SAME
    function as that row (views._check_in_fields), fed the same facts: the
    party's status (the booker's own lane), its start and its end. So the
    list and this screen agree, and can_scan_chair is always false for a
    party (it has no chair).

    OFF unless SELF_CHECK_IN_V1 and GROUP_CHECK_IN_V1, the two switches the
    booker's route needs here: no key at all, the read exactly as it was.
    """
    if not (settings.SELF_CHECK_IN_V1 and settings.GROUP_CHECK_IN_V1):
        return party
    members = party.get("members")
    selves = [
        m for m in (members if isinstance(members, list) else [])
        if isinstance(m, dict) and m.get("kind") == "self"
    ]
    party["is_booker"] = (
        len(selves) == 1
        and str(selves[0].get("user_id") or "").lower() == str(caller_id).lower()
    )
    party.update(_check_in_fields(party, now))
    return party


def read_group_response(request, group_id):
    """
    GET /api/v1/booking/<id> for a party (GROUP_BOOKING_V2). Called by
    BookingDetailView when the id is not a single booking's.
    """
    try:
        code, body = read_group_booking(
            group_id, authorization=request.META.get("HTTP_AUTHORIZATION", "")
        )
    except BookingApiUnavailable as exc:
        raise BookingApiDown() from exc
    if code != status.HTTP_200_OK or not isinstance(body, dict):
        return Response(body, status=code)
    # Only this read: the cancel's answer is the same party, but no button
    # belongs on it. One clock for the member rows and the party's own keys.
    now = datetime.now(dt_timezone.utc)
    party = _member_check_in(_stored_party(body), now)
    return Response(_booker_check_in(party, request.user.id, now))


@extend_schema(
    summary="Cancel a whole party, or a single booking",
    description=(
        "Behind GROUP_BOOKING_V2. The booker only: every member is cancelled "
        "together, or, if one cannot be (already checked in, say), none is "
        "and the answer is 409 `cannot_cancel`. Sending it again after a "
        "part-way failure finishes the job. Answers with the party, read "
        "back. No body.\n\n"
        "A SINGLE booking's id: behind SINGLE_BOOKING_ACTIONS_V1 (off: 404, "
        "as for any id that is not a party). The type is looked up on the "
        "first page of the caller's own Upcoming shelf; an id not found "
        "there, or a lookup that fails, is treated as a party's, so a "
        "party's cancel never depends on it. The body is optional: `reason` is one "
        "of NOT_SATISFIED, TOO_EXPENSIVE, MOVING or OTHER, else 422 "
        "`invalid_cancel_reason`; left out, null or blank is no reason. "
        "booking-api decides the refund, and its answer comes back as it "
        "came: 201 with `refund`, `kept`, `lateCancel` and `explanation`, or "
        "its refusal. A routine's id, or one of its sessions, is 404 here."
    ),
    request={
        "application/json": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": ["NOT_SATISFIED", "TOO_EXPENSIVE", "MOVING", "OTHER"],
                    "description": "Single booking only, optional. A party's cancel reads no body.",
                }
            },
        }
    },
    responses={
        200: OpenApiResponse(description="Cancelled. The party."),
        201: OpenApiResponse(description="A single booking, cancelled: booking-api's answer with the refund."),
        404: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description="No such party or booking, not the booker, not the caller's, or a flag is off.",
        ),
        409: OpenApiResponse(response=_OUR_ENVELOPE, description="`cannot_cancel`: nothing was cancelled."),
        415: OpenApiResponse(response=_OUR_ENVELOPE, description="Single booking: a body that is not JSON."),
        422: OpenApiResponse(response=_OUR_ENVELOPE, description="Single booking: `invalid_cancel_reason`."),
        503: OpenApiResponse(response=_OUR_ENVELOPE, description="booking-api unreachable."),
    },
)
class GroupBookingCancelView(APIView):
    """POST /api/v1/booking/<id>/cancel"""

    permission_classes = [IsAuthenticated]

    def post(self, request, booking_id):
        if settings.SINGLE_BOOKING_ACTIONS_V1:
            # A single booking's id is answered there; any other id comes
            # back as None and goes on below, as before the flag. Imported
            # here, as single_views imports this module.
            from .single_views import single_cancel_response

            single = single_cancel_response(request, booking_id)
            if single is not None:
                return single
        if not settings.GROUP_BOOKING_V2:
            raise Http404("Not found")
        try:
            code, body = cancel_group_booking(
                booking_id, authorization=request.META.get("HTTP_AUTHORIZATION", "")
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        if code != status.HTTP_200_OK or not isinstance(body, dict):
            return Response(body, status=code)
        return Response(_stored_party(body))


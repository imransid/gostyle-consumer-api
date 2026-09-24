"""
Group bookings for the app: when a whole party fits.

    POST /api/v1/booking/group-availability   every start of the day, and who fits

gostyle-booking-api does the planning and owns the bookings; this service
does what apps/salons/booking_api.py does for a single booking -- resolve the
salon, translate the app's shape into the engine's, forward it with the
caller's own token, and translate the answer back. The translation itself is
group_translate.py, which is pure; this module owns the requests, the clock
and the database reads.

Kept apart from views.py on purpose: nothing a single booking does changes
here, and the helpers it shares are imported from there untouched.
"""

import logging
import uuid
from datetime import datetime, time, timedelta

from django.http import Http404
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework.exceptions import (
    ErrorDetail,
    UnsupportedMediaType,
    ValidationError,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import group_translate as gt
from . import timezones
from .booking_api import BookingApiUnavailable, engine_clock, plan_group
from .group_serializers import GroupAvailabilityRequestSerializer
from .selectors import (
    booking_route,
    salon_profile,
    salon_stylists,
    service_timing_rows,
    stylist_service_coverage,
)
from .views import _OUR_ENVELOPE, BookingApiDown, _open_span

logger = logging.getLogger(__name__)

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

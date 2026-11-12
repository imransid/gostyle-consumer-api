"""
One location reading from the app, measured against the customer's visits.

For each salon the customer has a confirmed visit at, starting within
ALERT_LOOKAHEAD and not yet over, the reading is checked against that
salon's rings (zones.py), innermost first:

  * each ring remembers whether the customer is inside it (GeofenceState),
    so ENTERING is told apart from BEING there, and leaving is logged too;
  * a ring alerts ONCE per salon per day, the first time a reading puts the
    customer inside it: the very first reading of the day counts, so a
    customer who opens the app already at the door still hears;
  * when a reading is inside an inner ring, the rings outside it are marked
    as passed without an alert, so walking in fast (or a first reading at the
    door) is one alert, not three, and no "you are near" comes after
    "welcome";
  * the alert goes through the Notification Center's notify(), which keeps
    it, applies the customer's switches and queues the push for the worker.
    Nothing here waits on the push service.

The same key (dedupe_key) for the same ring, salon and day makes notify()
answer the first alert again, so two readings at once cannot send two.

EVERY DECISION IS LOGGED, the ones that send nothing included: no visit, no
salon pin, no service area, a reading too blurred for a ring, an alert held
back and why.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.notifications import wording
from apps.notifications.models import NotificationDelivery
from apps.notifications.service import notify

from . import sources, zones
from .models import GeofenceState

logger = logging.getLogger(__name__)

# A visit is watched from this long before it starts until it ends.
ALERT_LOOKAHEAD = timedelta(hours=3)
# GET /me/geofences lists the rings of the visits in the next day, so the app
# can ask the phone to watch them before ALERT_LOOKAHEAD begins.
REGION_LOOKAHEAD = timedelta(hours=24)
# A reading older than this says where the customer was, not where they are.
MAX_READING_AGE = timedelta(minutes=5)
# A reading further ahead than this is a phone clock that is wrong.
MAX_CLOCK_AHEAD = timedelta(minutes=2)
# A ring's state is of no use once its day is over; a customer's older rows
# are cleared on their next reading.
KEEP_STATE = timedelta(days=7)
# How late an alert may still go out (the kind's own deliver_within is the
# same). Never after the visit ends.
DELIVER_WITHIN = timedelta(minutes=10)
# Self check-in's window, booking-api's (apps/salons/views.CHECK_IN_OPENS_MINUTES).
CHECK_IN_OPENS = timedelta(minutes=30)
# booking-api's booking types "I am here" takes by the booking's own id
# (apps/salons/views.CHECK_IN_BOOKING_TYPES, in booking-api's spelling). A
# party's lane checks in through the party, so its alert does not offer it.
CHECK_IN_TYPES = ("single", "routine")

KIND = {
    zones.ARRIVAL: "visit.arrived",
    zones.AUTO_TRIGGER: "visit.almost_there",
    zones.PRIMARY: "visit.nearby",
}

# Why a visit was not measured at all.
SALON_NOT_FOUND = "salon_not_found"
NO_COORDINATES = "no_coordinates"
NO_SERVICE_AREA = "no_service_area"

# Why a ring inside did not alert.
VISIT_STARTED = "visit_started"
VISIT_ENDED = "visit_ended"
INNER_RING_FIRST = "inner_ring_first"


@dataclass(frozen=True)
class Reading:
    latitude: float
    longitude: float
    # Metres, the phone's own radius of uncertainty. None: not sent.
    accuracy_m: float | None
    recorded_at: object


def is_stale(reading, now):
    return reading.recorded_at < now - MAX_READING_AGE


def evaluate(account, reading, now=None):
    """Measure one reading. Returns one result per salon visit, for the
    answer to the app. Raises sources.Unavailable when a source cannot be read."""
    now = now or timezone.now()
    _forget_old(account, now)
    visits = sources.visits_for(account.pk, now, ALERT_LOOKAHEAD)
    if not visits:
        logger.debug("service area: account %s has no visit within %s", account.pk, ALERT_LOOKAHEAD)
        return []

    # One salon, one day, one walk to the door: a party one customer booked
    # is several bookings at one salon. The earliest still to come stands
    # for them (visits come earliest first).
    first = {}
    for visit in visits:
        first.setdefault((visit.branch_id, visit.trading_day), visit)
    salons = sources.salons_for(branch_id for branch_id, _day in first)
    return [
        _measure(account, reading, visit, salons.get(str(visit.branch_id)), now)
        for visit in first.values()
    ]


def regions(account, now=None):
    """The rings of the customer's visits in the next REGION_LOOKAHEAD, for
    the phone to watch (GET /me/geofences)."""
    now = now or timezone.now()
    visits = sources.visits_for(account.pk, now, REGION_LOOKAHEAD)
    first = {}
    for visit in visits:
        first.setdefault((visit.branch_id, visit.trading_day), visit)
    salons = sources.salons_for(branch_id for branch_id, _day in first)
    out = []
    for visit in first.values():
        salon = salons.get(str(visit.branch_id))
        if _skip_reason(salon, visit, log=False):
            continue
        for zone, radius_m in salon.service_area.rings_m:
            out.append(
                {
                    "id": f"{visit.booking_id}:{zone}",
                    "booking_id": str(visit.booking_id),
                    "salon_id": salon.salon_id,
                    "zone": zone,
                    "latitude": salon.lat,
                    "longitude": salon.lng,
                    "radius_m": round(radius_m),
                    "starts_at": visit.start_at,
                    "ends_at": visit.end_at,
                }
            )
    return out


def _skip_reason(salon, visit, log=True):
    """Why this visit cannot be measured, or None."""
    if salon is None:
        reason = SALON_NOT_FOUND
        if log:
            logger.warning(
                "service area: booking %s: branch %s has no live storefront, nothing to measure against",
                visit.booking_id,
                visit.branch_id,
            )
    elif salon.lat is None or salon.lng is None or (salon.lat == 0 and salon.lng == 0):
        # 0,0 is in the sea off Africa: a pin nobody set, not a salon.
        reason = NO_COORDINATES
        if log:
            logger.warning(
                "service area: booking %s: salon %s (branch %s) has no map pin (branch lat/lng)",
                visit.booking_id,
                salon.salon_id,
                visit.branch_id,
            )
    elif salon.service_area is None or not salon.service_area.rings_m:
        reason = NO_SERVICE_AREA
        if log:
            # Values saved but all unusable is a setup to fix; none saved is
            # a salon that chose no rings.
            logger.log(
                logging.WARNING if _problems(salon) else logging.INFO,
                "service area: booking %s: salon %s has no usable ring in Build -> Service Area%s",
                visit.booking_id,
                salon.salon_id,
                _problems(salon),
            )
    else:
        return None
    return reason


def _problems(salon):
    area = salon.service_area if salon else None
    return f" ({'; '.join(area.problems)})" if area and area.problems else ""


def _measure(account, reading, visit, salon, now):
    result = {
        "booking_id": str(visit.booking_id),
        "salon_id": salon.salon_id if salon else None,
        "distance_m": None,
        "skipped": _skip_reason(salon, visit),
        "zones": [],
        "sent": [],
    }
    if result["skipped"]:
        return result
    if salon.service_area.problems:
        logger.warning(
            "service area: salon %s (branch %s) has rings left out%s",
            salon.salon_id,
            visit.branch_id,
            _problems(salon),
        )

    distance = zones.distance_m(reading.latitude, reading.longitude, salon.lat, salon.lng)
    result["distance_m"] = round(distance)
    with transaction.atomic():
        states = _locked_states(account, visit, salon.service_area.rings_m, now)
        inner_inside = False
        for zone, radius_m in salon.service_area.rings_m:
            state = states[zone]
            inside = zones.reading_inside(distance, radius_m, reading.accuracy_m, state.inside)
            if inside is None:
                logger.debug(
                    "service area: account %s %s ring of salon %s: accuracy %s m is wider than the %g m ring, held",
                    account.pk,
                    zone,
                    salon.salon_id,
                    reading.accuracy_m,
                    radius_m,
                )
            elif inside != state.inside:
                state.inside = inside
                if inside:
                    state.entered_at = now
                else:
                    state.exited_at = now
                logger.info(
                    "service area: account %s %s the %s ring of salon %s (%d m, ring %g m, booking %s)",
                    account.pk,
                    "entered" if inside else "left",
                    zone,
                    salon.salon_id,
                    round(distance),
                    radius_m,
                    visit.booking_id,
                )

            if state.inside and state.notified_at is None:
                if inner_inside:
                    state.notified_at = now
                    state.suppressed_reason = INNER_RING_FIRST
                    logger.info(
                        "service area: account %s %s ring of salon %s passed with no alert: %s (booking %s)",
                        account.pk,
                        zone,
                        salon.salon_id,
                        INNER_RING_FIRST,
                        visit.booking_id,
                    )
                elif _alert(account, visit, salon, zone, radius_m, distance, state, now):
                    result["sent"].append(KIND[zone])
            if state.inside:
                inner_inside = True

            state.booking_id = visit.booking_id
            state.last_distance_m = round(distance)
            state.last_reading_at = reading.recorded_at
            state.updated_at = now
            state.save()
            result["zones"].append(
                {
                    "zone": zone,
                    "radius_m": round(radius_m),
                    "inside": state.inside,
                    "notified": state.notified_at is not None,
                }
            )
    return result


def _locked_states(account, visit, rings_m, now):
    """{zone: GeofenceState} for this visit's salon and day, every ring's row
    made if missing, all locked until the transaction ends: a second reading
    sent at the same moment waits here and then sees the first one's answer."""
    keys = {"account": account, "branch_id": visit.branch_id, "trading_day": visit.trading_day}
    for zone, _radius in rings_m:
        GeofenceState.objects.get_or_create(
            **keys, zone=zone, defaults={"booking_id": visit.booking_id, "created_at": now, "updated_at": now}
        )
    rows = GeofenceState.objects.select_for_update().filter(**keys, zone__in=[zone for zone, _ in rings_m])
    return {row.zone: row for row in rows}


def _gate(zone, visit, now):
    """Why a ring the customer is inside must not alert now, or None."""
    if now >= visit.end_at:
        return VISIT_ENDED
    if zone == zones.PRIMARY and now >= visit.start_at:
        # "You are near" once the visit has begun is a customer running late,
        # and the inner rings speak to that.
        return VISIT_STARTED
    return None


def can_check_in(visit, now):
    """Whether "I am here" is open for this visit right now, by the rule
    GET /bookings gives `can_check_in` (apps/salons/views._can_check_in)."""
    return (
        settings.SELF_CHECK_IN_V1
        and visit.booking_type in CHECK_IN_TYPES
        and visit.group_id is None
        and visit.start_at - CHECK_IN_OPENS <= now < visit.end_at
    )


def _alert(account, visit, salon, zone, radius_m, distance, state, now):
    """Send this ring's alert, or write down why not. True when sent."""
    reason = _gate(zone, visit, now)
    if reason:
        if state.suppressed_reason != reason:
            logger.info(
                "service area: account %s inside the %s ring of salon %s, no alert: %s (booking %s)",
                account.pk,
                zone,
                salon.salon_id,
                reason,
                visit.booking_id,
            )
        state.suppressed_reason = reason
        return False

    type_ = KIND[zone]
    words = wording.words_for(
        type_,
        {
            "booking_id": str(visit.booking_id),
            "booking_code": visit.code,
            "branch_id": str(visit.branch_id),
            "start_at": visit.start_at.isoformat(),
            "zone": zone,
            "can_check_in": "true" if can_check_in(visit, now) else "false",
        },
    )
    out = notify(
        account,
        type_,
        dedupe_key=f"geo:{zone}:{visit.branch_id}:{visit.trading_day.isoformat()}",
        title=words.title,
        body=words.body,
        data=words.data,
        target_type="booking",
        target_id=visit.booking_id,
        occurred_at=now,
        deliver_by=min(now + DELIVER_WITHIN, visit.end_at),
        source="customer-api",
    )
    state.notified_at = now
    state.suppressed_reason = ""
    if not out.created:
        logger.info(
            "service area: account %s %s for salon %s already sent (notification %s)",
            account.pk,
            type_,
            salon.salon_id,
            out.notification.pk,
        )
        return False
    pushes = NotificationDelivery.objects.filter(notification=out.notification).count()
    logger.info(
        "service area: account %s %s for salon %s (%d m, ring %g m, booking %s): notification %s, %s",
        account.pk,
        type_,
        salon.salon_id,
        round(distance),
        radius_m,
        visit.booking_id,
        out.notification.pk,
        "push queued" if pushes else "kept in the Notification Center only (reminders or push switched off)",
    )
    return True


def _forget_old(account, now):
    GeofenceState.objects.filter(account=account, trading_day__lt=(now - KEEP_STATE).date()).delete()

"""
What a location reading is measured against, read from the services that
own it. Nothing here writes.

  * the customer's visits: booking-api's database (DATABASES["booking"],
    read only), as Rate your visit reads it (apps/reviews/visits.py);
  * each salon's centre, name and clock: its storefront card
    (salon_cards_for_refs, the booking drawer's own), whose map pin is the
    branch's lat/lng;
  * each salon's Service Area: the platform's Build wizard, step
    `service-area`, of the build that went live as the branch's tenant
    (build.operational_tenant_id). That step is the only place the numbers
    are kept: go-live copies them nowhere else, and an ACTIVE build cannot
    be edited, so it is the live value.

One function per source, so the tests can stand in for each.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError
from django.db.models import F, Q

from apps.booking_data.models import Booking
from apps.platform_data.models import Branch, BuildStep
from apps.salons.selectors import salon_cards_for_refs

from .zones import parse_service_area

DB = "booking"

# booking-api's `confirmed`, the app's CONFIRMED_BY_SALON: the one status
# self check-in takes too (apps/salons/views._can_check_in). A booking not
# yet confirmed, or already checked in, is not a walk to the salon to watch.
VISIT_STATUSES = ("confirmed",)

# No visit runs longer than this. It gives the read a lower bound on
# start_at, so it stays a short range of booking_customer_idx
# (customer_id, start_at) instead of the customer's whole history.
LONGEST_VISIT = timedelta(hours=12)

# More than this many live visits in the next day is not a real customer.
MAX_VISITS = 20

# The legacy step number of `service-area` (step-keys.ts STEP_KEY_TO_NO), for
# a row written before the platform filled step_key in.
SERVICE_AREA_STEP_NO = 11


class Unavailable(Exception):
    """A source could not be read: booking-api's database is not set up here
    (BOOKING_DB_NAME), or a read failed."""


@dataclass(frozen=True)
class Visit:
    booking_id: uuid.UUID
    code: str
    branch_id: uuid.UUID
    # The visit's day on the salon's own clock, as booking-api keeps it.
    trading_day: object
    start_at: object
    end_at: object
    booking_type: str
    group_id: uuid.UUID | None


@dataclass(frozen=True)
class Salon:
    branch_id: str
    # The storefront id: what every salon endpoint here calls the salon.
    salon_id: str
    name: str | None
    timezone: str | None
    lat: float | None
    lng: float | None
    # None: the salon's build has no `service-area` step.
    service_area: object


def visits_for(customer_id, now, lookahead):
    """The customer's confirmed visits that have not ended and start within
    `lookahead` from now, earliest first."""
    if DB not in settings.DATABASES:
        raise Unavailable("BOOKING_DB_NAME is not set: the visits cannot be read.")
    try:
        rows = list(
            Booking.objects.using(DB)
            .filter(
                customer_id=customer_id,
                status__in=VISIT_STATUSES,
                start_at__gte=now - LONGEST_VISIT,
                start_at__lte=now + lookahead,
                end_at__gt=now,
            )
            .order_by("start_at")
            .values(
                "id", "code", "branch_id", "trading_day", "start_at", "end_at", "booking_type", "group_id"
            )[:MAX_VISITS]
        )
    except DatabaseError as exc:
        raise Unavailable(str(exc)) from exc
    return [
        Visit(
            booking_id=row["id"],
            code=row["code"],
            branch_id=row["branch_id"],
            trading_day=row["trading_day"],
            start_at=row["start_at"],
            end_at=row["end_at"],
            booking_type=row["booking_type"],
            group_id=row["group_id"],
        )
        for row in rows
    ]


def salons_for(branch_ids):
    """{branch id as a string: Salon} for each branch with a live storefront.
    A branch without one is left out."""
    refs = sorted({str(branch_id) for branch_id in branch_ids})
    if not refs:
        return {}
    try:
        cards = salon_cards_for_refs(refs)
        tenants = {
            str(branch_id): tenant_id
            for branch_id, tenant_id in Branch.objects.filter(id__in=refs).values_list("id", "tenant_id")
        }
        areas = service_areas_for(set(tenants.values()))
    except DatabaseError as exc:
        raise Unavailable(str(exc)) from exc
    salons = {}
    for ref in refs:
        card = cards.get(ref)
        if card is None:
            continue
        salons[ref] = Salon(
            branch_id=ref,
            salon_id=card["id"],
            name=card.get("name"),
            timezone=card.get("timezone"),
            lat=card.get("lat"),
            lng=card.get("lng"),
            service_area=areas.get(tenants.get(ref)),
        )
    return salons


def service_areas_for(tenant_ids):
    """{tenant id: ServiceArea} from the `service-area` step of the build
    each tenant went live from. A tenant with two such builds takes the one
    accepted last."""
    tenant_ids = {tenant for tenant in tenant_ids if tenant is not None}
    if not tenant_ids:
        return {}
    rows = (
        BuildStep.objects.filter(build__operational_tenant_id__in=tenant_ids)
        .filter(Q(step_key="service-area") | Q(step_key__isnull=True, step_no=SERVICE_AREA_STEP_NO))
        .order_by(F("build__accepted_at").desc(nulls_last=True))
        .values_list("build__operational_tenant_id", "payload")
    )
    areas = {}
    for tenant_id, payload in rows:
        if tenant_id not in areas:
            areas[tenant_id] = parse_service_area(payload)
    return areas

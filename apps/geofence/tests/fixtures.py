"""
A salon, its Service Area and a customer's visits, for the geofence tests.

booking-api's database and the platform's tables are not in the test
database, so sources.visits_for and sources.salons_for are stood in for, as
apps/reviews/tests fakes the visit reads. The salon card the alert's words
look up (wording.salon_cards_for_refs) is stood in for too. Everything of
this service's own (GeofenceState, the Notification Center) is real.
"""

import math
import uuid
from contextlib import contextmanager
from datetime import timedelta
from unittest import mock

from django.utils import timezone

from apps.geofence import sources, zones

SALON_POINT = (25.2048, 55.2708)
BRANCH = uuid.UUID("5a1d3c2b-7e4f-4a8b-9c6d-0e1f2a3b4c5d")
STOREFRONT = "7c2d9a4e-1b3f-4e6a-8d5c-9f0e1a2b3c4d"
OTHER_BRANCH = uuid.UUID("6b2e4d3c-8f5a-4b9c-8d7e-1f2a3b4c5d6e")
OTHER_STOREFRONT = "8d3e0b5f-2c4a-4f7b-9e6d-0a1b2c3d4e5f"
OTHER_POINT = (25.0800, 55.1400)

# The bug report's sheet.
SHEET = {"primaryRadiusM": 800, "autoTriggerM": 200, "arrivalRadiusM": 50, "homeRadiusKm": 15}

METRES_PER_DEGREE = 2 * math.pi * zones.EARTH_RADIUS_M / 360


def north_of(point, metres):
    """A point exactly `metres` north (along the meridian, exact for haversine)."""
    return (point[0] + metres / METRES_PER_DEGREE, point[1])


def salon(branch=BRANCH, storefront=STOREFRONT, point=SALON_POINT, payload=SHEET, name="Demo Salon & Spa"):
    return sources.Salon(
        branch_id=str(branch),
        salon_id=storefront,
        name=name,
        timezone="Asia/Dubai",
        lat=point[0] if point else None,
        lng=point[1] if point else None,
        service_area=zones.parse_service_area(payload) if payload is not None else None,
    )


def visit(starts_in=timedelta(hours=1), minutes=60, branch=BRANCH, code="GS-1500", group_id=None, now=None):
    start = (now or timezone.now()) + starts_in
    return sources.Visit(
        booking_id=uuid.uuid4(),
        code=code,
        branch_id=branch,
        trading_day=start.date(),
        start_at=start,
        end_at=start + timedelta(minutes=minutes),
        booking_type="single",
        group_id=group_id,
    )


def card(s):
    return {"id": s.salon_id, "name": s.name, "timezone": s.timezone}


@contextmanager
def world(visits, salons):
    """The customer's visits and the salons, as the sources would read them."""
    by_branch = {s.branch_id: s for s in salons}

    def salons_for(branch_ids):
        return {str(b): by_branch[str(b)] for b in branch_ids if str(b) in by_branch}

    def cards(refs):
        return {ref: card(by_branch[ref]) for ref in refs if ref in by_branch}

    with (
        mock.patch.object(sources, "visits_for", return_value=list(visits)) as visits_for,
        mock.patch.object(sources, "salons_for", side_effect=salons_for),
        mock.patch("apps.notifications.wording.salon_cards_for_refs", side_effect=cards),
    ):
        yield visits_for

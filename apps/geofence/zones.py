"""
A salon's Service Area, as rings around the salon, with no database.

The platform's Build wizard (step `service-area`, Build -> Service Area) keeps
four distances. Three of them are rings a customer on the way to a visit
walks through, innermost first:

    arrival       Arrival Radius               e.g. 50 m   (form: "I'm here")
    auto_trigger  Auto-Trigger Distance        e.g. 200 m  (form: "prep starts")
    primary       Primary Appointment Radius   e.g. 800 m  (form: "meters from pin")

The keys are the admin form's own (hq-master-admin-frontend-next,
wizard-step-payload.ts): it saves its whole `geo` state, so primaryRadius,
autoTrigger, arrivalRadius and homeRadiusKm, plus primaryRadiusM for the API.
The form's defaults are exactly 800 / 200 / 50 / 15 km.

The fourth, the Home-visit Radius (e.g. 15 km), is how far the salon travels
to a customer's home. Nobody walks through it on the way in, so it is read
and checked but never sets anything off.

EVERY DISTANCE HERE IS IN METRES. The wizard writes three of them in metres
and the home-visit one in kilometres; parse_service_area converts once, at
the edge, and every name says its unit.
"""

import math
from dataclasses import dataclass

# The mean Earth radius apps/salons/geo.py uses (EARTH_RADIUS_KM), in metres,
# so the map and the alerts never disagree about a distance.
EARTH_RADIUS_M = 6_371_000.0

# Below this a phone's GPS cannot tell inside from outside, and a value this
# small is nearly always kilometres typed into a metres box (0.8 for 800 m).
MIN_RADIUS_M = 10
# The storefront MAP section's own cap (radiusKm at most 50). A ring wider
# than that is not "near the salon" any more.
MAX_RADIUS_M = 50_000
# The platform's own cap on primaryRadiusM (step-field-specs.ts), for the
# home-visit radius, which is a travel range, not a ring.
MAX_HOME_RADIUS_M = 500_000

ARRIVAL = "arrival"
AUTO_TRIGGER = "auto_trigger"
PRIMARY = "primary"
# Innermost first: the order the rings are checked in, and the order they
# must nest in (arrival < auto_trigger < primary).
ZONES = (ARRIVAL, AUTO_TRIGGER, PRIMARY)

# The payload keys each ring is read from; the first one present wins. The
# `M` spelling is the Build API v2 one (gostyle-platform
# docs/salon-build-v2-fe-guide.md); the bare one is the wizard's older field
# name (docs/nhl/admin/build-handover-backend-guide.md, step 11), which the
# platform's own review document still reads (review-document.ts:838-839).
# Both are metres.
RING_KEYS_M = {
    PRIMARY: ("primaryRadiusM", "primaryRadius"),
    AUTO_TRIGGER: ("autoTriggerM", "autoTrigger"),
    ARRIVAL: ("arrivalRadiusM", "arrivalRadius"),
}
HOME_KEYS_KM = ("homeRadiusKm",)

# How far past a ring a customer must be before they count as gone: a tenth
# of the ring, never less than this. Inside that band the last answer holds,
# so a fix wobbling on the line does not flap in and out.
MIN_EXIT_MARGIN_M = 10


def distance_m(lat1, lng1, lat2, lng2):
    """
    The great-circle (haversine) distance between two points, in metres.
    Arguments are (latitude, longitude) pairs in degrees, latitude FIRST.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    half_dphi = (phi2 - phi1) / 2
    half_dlambda = math.radians(lng2 - lng1) / 2
    a = math.sin(half_dphi) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(half_dlambda) ** 2
    # min(): rounding can push `a` a hair over 1 for two opposite points.
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def exit_margin_m(radius_m):
    return max(MIN_EXIT_MARGIN_M, radius_m * 0.1)


def reading_inside(distance_m, radius_m, accuracy_m, was_inside):
    """
    True inside the ring, False outside it, None when this reading cannot
    tell for this ring.

    ON THE LINE IS INSIDE (distance == radius). A fix whose accuracy circle
    is wider than the ring itself cannot place the customer in it or out of
    it, so it changes nothing: a 65 m indoor fix says nothing about a 50 m
    arrival ring. Between the ring and its exit margin the last answer holds.
    """
    if accuracy_m is not None and accuracy_m > radius_m:
        return None
    if distance_m <= radius_m:
        return True
    if distance_m > radius_m + exit_margin_m(radius_m):
        return False
    return bool(was_inside)


@dataclass(frozen=True)
class ServiceArea:
    # ((zone, radius_m), ...), innermost first, only the rings that passed
    # every check. Empty: this salon sets nothing off.
    rings_m: tuple
    home_radius_m: float | None
    # What was wrong with the saved values, in words, for the log. A ring
    # named here was left out of rings_m.
    problems: tuple

    def radius_m(self, zone):
        return dict(self.rings_m).get(zone)


def number(value):
    """A finite number from a JSON value (a number, or a number in a string,
    as a form field may send it), or None. True and False are not numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _first(payload, keys):
    for key in keys:
        value = payload.get(key)
        if value is not None and value != "":
            return key, value
    return None, None


def parse_service_area(payload):
    """
    The ServiceArea in a `service-area` step payload, or None for a payload
    that is not an object.

    A ring is left out, with the reason in `problems`, when its value is not
    a number, is under MIN_RADIUS_M or over MAX_RADIUS_M, or does not sit
    strictly inside the ring outside it. Checked from the outside in, so a
    bad inner ring never costs the good outer ones.
    """
    if not isinstance(payload, dict):
        return None

    problems = []
    radii_m = {}
    for zone in ZONES:
        key, raw = _first(payload, RING_KEYS_M[zone])
        if key is None:
            continue
        value_m = number(raw)
        if value_m == 0:
            # The admin form saves an emptied box as 0
            # (step-service-area.tsx: parseInt(...) || 0): the ring is off.
            continue
        if value_m is None:
            problems.append(f"{key}={raw!r} is not a number")
        elif not MIN_RADIUS_M <= value_m <= MAX_RADIUS_M:
            problems.append(f"{key}={value_m:g} m is outside {MIN_RADIUS_M}..{MAX_RADIUS_M} m")
        else:
            radii_m[zone] = value_m

    outer = None
    for zone in reversed(ZONES):
        if zone not in radii_m:
            continue
        if outer is not None and radii_m[zone] >= outer[1]:
            problems.append(
                f"{zone} ring {radii_m[zone]:g} m is not inside the {outer[0]} ring {outer[1]:g} m"
            )
            del radii_m[zone]
            continue
        outer = (zone, radii_m[zone])

    home_radius_m = None
    key, raw = _first(payload, HOME_KEYS_KM)
    if key is not None:
        value_km = number(raw)
        if value_km is None:
            problems.append(f"{key}={raw!r} is not a number")
        elif not 0 < value_km * 1000 <= MAX_HOME_RADIUS_M:
            problems.append(f"{key}={value_km:g} km is outside 0..{MAX_HOME_RADIUS_M // 1000} km")
        else:
            home_radius_m = value_km * 1000

    return ServiceArea(
        rings_m=tuple((zone, radii_m[zone]) for zone in ZONES if zone in radii_m),
        home_radius_m=home_radius_m,
        problems=tuple(problems),
    )

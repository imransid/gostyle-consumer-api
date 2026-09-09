"""
Map-viewport arithmetic. Pure: no Django, no DB, no clock.

The map endpoint receives a region the way MapKit and react-native-maps
describe one — a centre plus a span — and Postgres wants two corners. That
conversion is the only logic in the endpoint worth testing, and platform
tables do not exist in the test database, so it lives here rather than inside
the selector where no test could reach it.
"""

import math


def bounding_box(latitude, longitude, latitude_delta, longitude_delta):
    """
    Convert a map region (centre + span) into (sw_lat, sw_lng, ne_lat, ne_lng).

    Returns None when any component is missing: a half-specified region is not
    a viewport, and guessing the absent half would silently return the wrong
    salons rather than every salon.

    The deltas are the FULL span, not the radius, which is what both mobile map
    libraries mean by `latitudeDelta`. Halving them is therefore correct and is
    the one thing here that is easy to get backwards.
    """
    if None in (latitude, longitude, latitude_delta, longitude_delta):
        return None

    # abs(), because a negative span is a client bug that would otherwise
    # produce an inverted box matching nothing at all, with a 200 and an empty
    # list to explain it.
    half_lat = abs(latitude_delta) / 2.0
    half_lng = abs(longitude_delta) / 2.0

    return (
        latitude - half_lat,
        longitude - half_lng,
        latitude + half_lat,
        longitude + half_lng,
    )


# Mean Earth radius in kilometres. The SAME constant `with_distance` uses for
# its haversine annotation, and deliberately so: a bounding box computed from
# a slightly different Earth than the distance it prefilters would let salons
# fall through the gap between the two, which reads as "the radius filter
# randomly drops one salon" and is close to impossible to find afterwards.
EARTH_RADIUS_KM = 6371.0


def radius_box(latitude, longitude, radius_km):
    """
    Convert a centre and a radius in KILOMETRES into
    (sw_lat, sw_lng, ne_lat, ne_lng).

    A PREFILTER, never the answer. The box wraps the circle, so its corners
    lie outside the radius: everything this returns still has to survive the
    exact haversine test. The point of it is that four plain comparisons are
    something Postgres can reason about, and trigonometry over every branch in
    the country is not.

    Returns None when anything is missing or the radius is not positive, which
    the caller reads as "no radius filter" rather than "a radius of nothing".

    A box that would cross the antimeridian is CLAMPED at +/-180 rather than
    split into two ranges. GoStyle operates in the Gulf, so that case cannot
    arise here; a correct split is an OR of two longitude ranges and belongs
    with whoever first needs it.
    """
    if None in (latitude, longitude, radius_km):
        return None
    if radius_km <= 0:
        return None

    # A degree of latitude is ~111 km everywhere on Earth, so this one is just
    # arc length over radius.
    half_lat = math.degrees(radius_km / EARTH_RADIUS_KM)

    # THE COS(LATITUDE) TERM IS THE WHOLE POINT OF THIS FUNCTION. A degree of
    # longitude shrinks towards the poles, so reusing half_lat for both axes —
    # which is what this looks like it should be — builds a box far too wide.
    # At Dubai's 25 N it is 10% too wide and the error grows with latitude.
    # The haversine behind it would still cut the extras, so the bug never
    # shows up as a wrong result: only as a query that read rows it did not
    # need to, which is exactly the cost the box existed to avoid.
    shrink = abs(math.cos(math.radians(latitude)))
    if shrink < 1e-12:
        # Standing on a pole. Every meridian is within the radius.
        half_lng = 180.0
    else:
        half_lng = min(math.degrees(radius_km / (EARTH_RADIUS_KM * shrink)), 180.0)

    return (
        max(latitude - half_lat, -90.0),
        max(longitude - half_lng, -180.0),
        min(latitude + half_lat, 90.0),
        min(longitude + half_lng, 180.0),
    )


def format_distance(km):
    """
    A distance in kilometres as the string the card prints. None stays None.

    Under a kilometre it reads in metres, because "0.4 km" is not how anyone
    describes a four-minute walk. Above it, ONE decimal: the number is
    computed from a branch's stored lat/lng, which is a pin somebody dropped
    on a map during onboarding, so a second decimal would be claiming
    ten-metre precision that the input never had.
    """
    if km is None:
        return None
    try:
        value = float(km)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None

    if value < 1:
        metres = int(round(value * 1000))
        # 0.9996 km rounds to 1000 m, which should read as "1.0 km" and not as
        # a four-digit metre count.
        if metres < 1000:
            return f"{metres} m"
    return f"{value:.1f} km"

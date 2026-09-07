"""
Map-viewport arithmetic. Pure: no Django, no DB, no clock.

The map endpoint receives a region the way MapKit and react-native-maps
describe one — a centre plus a span — and Postgres wants two corners. That
conversion is the only logic in the endpoint worth testing, and platform
tables do not exist in the test database, so it lives here rather than inside
the selector where no test could reach it.
"""


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

"""
Which country a point is in, offline, on this server (MAP_COUNTRY_V1).

The borders are data/country_borders.json.gz, built by
scripts/build_country_borders.py from Natural Earth's 1:10m countries (public
domain, no account, no key). No geocoding API: a customer's position never
leaves this process, and nothing here can be slow, rate limited or billed.

"In a country" is the mobile team's rule (docs/map-country-fe-contract.md,
section 3): inside its borders, or, when no country holds the point, the
nearest country within 25 km. The second half is for the coast: the marina,
the beach, Palm Jumeirah and other reclaimed land that land-only border data
leaves in the sea.

The customer and the salons are judged by these same borders: the customer in
`position_country`, every salon in map_country.refresh_salon_countries.

The borders load once per process, on first use (about 4 MB in memory).
"""

import gzip
import json
import math
import threading
from array import array
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .geo import EARTH_RADIUS_KM

BORDERS_FILE = Path(__file__).resolve().parent / "data" / "country_borders.json.gz"

# The FE doc's "nearest country within 25 km" (section 3).
NEAREST_KM = 25.0

# The phone's fix arrives rounded to 2 decimals (about 1 km); the lookup and
# its cache use the same rounding, whatever the caller sent.
POSITION_DECIMALS = 2

KM_PER_DEGREE = 2 * math.pi * EARTH_RADIUS_KM / 360


@dataclass(frozen=True)
class Country:
    code: str  # ISO 3166-1 alpha-2, upper case ("AE")
    name: str  # in English ("United Arab Emirates")


class _Polygon:
    """One polygon: its box and its rings (outer first, then holes), as whole-number steps."""

    __slots__ = ("box", "rings")

    def __init__(self, rings):
        self.rings = rings
        xs = [x for ring_xs, _ in rings for x in ring_xs]
        ys = [y for _, ring_ys in rings for y in ring_ys]
        self.box = (min(xs), min(ys), max(xs), max(ys))


class _Shape:
    __slots__ = ("country", "box", "polygons")

    def __init__(self, country, polygons):
        self.country = country
        self.polygons = polygons
        boxes = [polygon.box for polygon in polygons]
        self.box = (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )


class _Borders:
    def __init__(self, payload):
        self.scale = payload["scale"]
        self.source = payload["source"]
        self.shapes = []
        for entry in payload["countries"]:
            country = Country(code=entry["code"], name=entry["name"])
            polygons = [_Polygon([_decode(ring) for ring in rings]) for rings in entry["polygons"]]
            self.shapes.append(_Shape(country, polygons))
        self.by_code = {shape.country.code: shape.country for shape in self.shapes}


def _decode(steps):
    """[x0, y0, dx1, dy1, ...] back to two arrays of whole-number positions."""
    xs, ys = array("i"), array("i")
    x = y = 0
    for i in range(0, len(steps), 2):
        x += steps[i]
        y += steps[i + 1]
        xs.append(x)
        ys.append(y)
    return xs, ys


_borders = None
_lock = threading.Lock()


def borders():
    """The borders, read from disk once per process."""
    global _borders
    if _borders is None:
        with _lock:
            if _borders is None:
                with gzip.open(BORDERS_FILE, "rt", encoding="utf-8") as handle:
                    _borders = _Borders(json.load(handle))
    return _borders


def country_by_code(code):
    """The Country for an ISO code, or None for a code the borders do not know."""
    return borders().by_code.get((code or "").upper())


def country_at(latitude, longitude):
    """
    The Country the point is in, or None (out at sea, more than 25 km from
    every country). Exact: no rounding, no cache. For a customer, use
    `position_country`.
    """
    data = borders()
    x = longitude * data.scale
    y = latitude * data.scale

    for shape in data.shapes:
        if not _in_box(shape.box, x, y):
            continue
        for polygon in shape.polygons:
            if _in_box(polygon.box, x, y) and _inside(polygon, x, y):
                return shape.country

    return _nearest(data, latitude, x, y)


@lru_cache(maxsize=16384)
def _cached_country(latitude, longitude):
    return country_at(latitude, longitude)


def position_country(latitude, longitude):
    """
    `country_at` for a customer: rounded to 2 decimals first and cached by
    that rounded position, in this process only. Borders do not move, and the
    position is never written anywhere (not Redis, not a log).
    """
    return _cached_country(round(latitude, POSITION_DECIMALS), round(longitude, POSITION_DECIMALS))


def _in_box(box, x, y, margin_x=0.0, margin_y=0.0):
    return box[0] - margin_x <= x <= box[2] + margin_x and box[1] - margin_y <= y <= box[3] + margin_y


def _inside(polygon, x, y):
    """Even-odd over every ring, so a hole (a lake, an enclave) is outside."""
    inside = False
    for xs, ys in polygon.rings:
        xj, yj = xs[-1], ys[-1]
        for xi, yi in zip(xs, ys):
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            xj, yj = xi, yi
    return inside


def _nearest(data, latitude, x, y):
    """
    The country with the border point nearest to (x, y), when it is within
    NEAREST_KM, else None. Distances on a flat map around the point: at 25 km
    that is off by far less than a metre per kilometre.
    """
    km_y = KM_PER_DEGREE / data.scale
    km_x = km_y * max(math.cos(math.radians(latitude)), 0.01)
    margin_x = NEAREST_KM / km_x
    margin_y = NEAREST_KM / km_y
    low_x, high_x = x - margin_x, x + margin_x
    low_y, high_y = y - margin_y, y + margin_y

    best_country, best = None, NEAREST_KM * NEAREST_KM
    for shape in data.shapes:
        if not _in_box(shape.box, x, y, margin_x, margin_y):
            continue
        for polygon in shape.polygons:
            if not _in_box(polygon.box, x, y, margin_x, margin_y):
                continue
            for xs, ys in polygon.rings:
                xj, yj = xs[-1], ys[-1]
                for xi, yi in zip(xs, ys):
                    # Both ends past the same side of the 25 km square: too far.
                    if not (
                        (xi < low_x and xj < low_x)
                        or (xi > high_x and xj > high_x)
                        or (yi < low_y and yj < low_y)
                        or (yi > high_y and yj > high_y)
                    ):
                        distance = _segment_km2((xj - x) * km_x, (yj - y) * km_y, (xi - x) * km_x, (yi - y) * km_y)
                        if distance < best:
                            best_country, best = shape.country, distance
                    xj, yj = xi, yi
    return best_country


def _segment_km2(ax, ay, bx, by):
    """The squared distance from (0, 0) to the segment A-B, in km squared."""
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length))
    px, py = ax + t * dx, ay + t * dy
    return px * px + py * py

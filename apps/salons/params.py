"""
Reading the /discover query string.

PURE. No Django, no database, no clock. It takes anything that answers
`.get(name)` — a QueryDict, a plain dict — and returns a settled dict of
Python values, or raises ParamError naming the parameter at fault. That is the
reason it is a module and not twenty lines inside the view: query strings are
where the interesting mistakes live (a radius with no centre, a boolean
spelled "yes", a latitude of 500), and none of them are reachable by a test
that would first need the platform tables to exist.

ABSENT IS NOT INVALID, and that distinction is the whole design here:

  * A parameter that was not sent, or was sent empty, means "no filter". The
    app sends `latitude=` with nothing after it when the customer denies
    location permission, and that must not be an error.
  * A parameter that WAS sent and cannot be read is a 422 naming it. The old
    view caught ValueError and carried on, so `lat=25,19` — a comma, from a
    European locale — quietly dropped the location and returned the national
    list with a 200 and a filter that looked applied. That is the failure mode
    this repo already refuses elsewhere; see the closing comment in
    selectors.map_venues.
"""


class ParamError(ValueError):
    """One unreadable query parameter, carrying the name to blame."""

    def __init__(self, param, message):
        self.param = param
        self.message = message
        super().__init__(f"{param}: {message}")


# Generous on the way in, because a boolean reaches an HTTP API in about six
# spellings and none of them is wrong on purpose. Anything OUTSIDE these two
# sets is still an error: `is_open_now=maybe` is a client bug, and answering it
# with "false" would hide open salons and look like empty inventory.
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})

# What the mobile contract offers, plus "unisex" — which the documented API has
# accepted since before the app had a category chip, and which real salons are.
CATEGORY_CHOICES = frozenset({"all", "gents", "ladies", "unisex"})

SORT_CHOICES = frozenset({"distance", "rating"})

# An ILIKE '%…%' over a term this long is not a search anyone typed.
SEARCH_MAX_LENGTH = 100


def _raw(params, *names):
    """
    The first accepted spelling that was actually sent, as (name, value).

    /discover shipped with `lat`/`lng`; the app asks for `latitude`/
    `longitude`. Both work. The name comes back alongside the value so an
    error message can blame the parameter the caller actually typed rather
    than the one this file happens to prefer.
    """
    for name in names:
        value = params.get(name)
        if value is not None and str(value).strip() != "":
            return name, str(value).strip()
    return names[0], None


def _boolean(params, *names, default=False):
    name, raw = _raw(params, *names)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    raise ParamError(name, f"Expected true or false, got {raw!r}.")


def _number(params, *names, minimum=None, maximum=None):
    name, raw = _raw(params, *names)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ParamError(name, f"Expected a number, got {raw!r}.") from None

    # float() happily accepts "nan" and "inf". Either one poisons every
    # comparison it touches: a bounding box built from NaN matches no row and
    # returns an empty list that looks like an honest "no salons near you".
    if value != value or value in (float("inf"), float("-inf")):
        raise ParamError(name, f"Expected a finite number, got {raw!r}.")

    if minimum is not None and value < minimum:
        raise ParamError(name, f"Must be {minimum} or more, got {value}.")
    if maximum is not None and value > maximum:
        raise ParamError(name, f"Must be {maximum} or less, got {value}.")
    return value


def _choice(params, *names, choices, default=None):
    name, raw = _raw(params, *names)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered not in choices:
        raise ParamError(
            name,
            f"Expected one of: {', '.join(sorted(choices))}. Got {raw!r}.",
        )
    return lowered


def _text(params, *names, max_length=None):
    name, raw = _raw(params, *names)
    if raw is None:
        return None
    if max_length is not None and len(raw) > max_length:
        raise ParamError(name, f"Must be {max_length} characters or fewer.")
    return raw


def parse_discovery(params):
    """
    The whole /discover query string, settled.

    Returns a dict with every key always present, so the view never reaches
    for a parameter that might not be there:

        latitude, longitude   float or None, and either both or neither
        radius_km             float > 0 or None. KILOMETRES.
        category              "gents"/"ladies"/"unisex", or None for no filter
        search                str or None
        city                  str or None
        sort                  "distance"/"rating" or None for the default
        rating_min            float or None
        is_top_rated          bool
        is_open_now           bool
        hijab_only            bool

    Note that "all" comes back as None. It is not a category — it is the
    absence of the category filter — and turning it into None here means no
    caller downstream has to remember that.
    """
    latitude = _number(params, "latitude", "lat", minimum=-90, maximum=90)
    longitude = _number(params, "longitude", "lng", "lon", minimum=-180, maximum=180)

    # Half a coordinate is not a location. Guessing the other half would put
    # the customer on the prime meridian and sort the whole country by their
    # distance from a point in the Atlantic.
    if (latitude is None) != (longitude is None):
        missing = "longitude" if longitude is None else "latitude"
        raise ParamError(missing, "Send latitude and longitude together.")

    radius_km = _number(params, "radius", "radius_km")
    if radius_km is not None:
        if radius_km <= 0:
            raise ParamError("radius", "Must be greater than 0 (kilometres).")
        if latitude is None:
            raise ParamError(
                "radius", "Needs latitude and longitude to measure from."
            )

    sort = _choice(params, "sort", choices=SORT_CHOICES)
    if sort == "distance" and latitude is None:
        # Silently falling back to rating order is the trap: the list comes
        # back looking sorted, just not by what was asked for, and nothing in
        # the response says so.
        raise ParamError("sort", "Sorting by distance needs latitude and longitude.")

    category = _choice(params, "category", choices=CATEGORY_CHOICES)

    return {
        "latitude": latitude,
        "longitude": longitude,
        "radius_km": radius_km,
        "category": None if category == "all" else category,
        "search": _text(params, "search", "q", max_length=SEARCH_MAX_LENGTH),
        "city": _text(params, "city"),
        "sort": sort,
        "rating_min": _number(params, "rating_min", minimum=0, maximum=5),
        "is_top_rated": _boolean(params, "is_top_rated", "top_rated"),
        "is_open_now": _boolean(params, "is_open_now", "open_now"),
        "hijab_only": _boolean(params, "hijab_mode", "hijab_only"),
    }



def parse_map(params):
    """
    The /discover/map query string, settled.

    radius arrives in METRES here, unlike /discover which takes
    kilometres, because the map sends a viewport size. It is
    converted once, right here, so nothing downstream sees metres.
    """
    latitude = _number(params, "latitude", "lat", minimum=-90, maximum=90)
    longitude = _number(params, "longitude", "lng", "lon", minimum=-180, maximum=180)

    if (latitude is None) != (longitude is None):
        missing = "longitude" if longitude is None else "latitude"
        raise ParamError(missing, "Send latitude and longitude together.")

    radius_m = _number(params, "radius", minimum=100, maximum=50000)
    if radius_m is not None and latitude is None:
        raise ParamError("radius", "Needs latitude and longitude to measure from.")

    category = _choice(params, "category", choices=CATEGORY_CHOICES)

    return {
        "latitude": latitude,
        "longitude": longitude,
        "radius_km": None if radius_m is None else radius_m / 1000.0,
        "latitude_delta": _number(params, "latitudeDelta", "latitude_delta", "lat_delta"),
        "longitude_delta": _number(params, "longitudeDelta", "longitude_delta", "lng_delta"),
        "category": None if category == "all" else category,
        "limit": _number(params, "limit", minimum=1),
    }
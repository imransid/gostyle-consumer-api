import uuid
from datetime import datetime, timedelta


class ParamError(ValueError):
    """One unreadable query parameter, carrying the name to blame.

    `code` is the contract the app branches on, so it stays stable even when
    the wording changes. It defaults to DRF's own "invalid", which is what
    every parameter here meant before any of them needed a code of its own.
    """

    def __init__(self, param, message, code="invalid"):
        self.param = param
        self.message = message
        self.code = code
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

    A few parameters accept more than one spelling (`search` or `q`,
    `hijab_mode` or `hijab_only`). The name comes back alongside the value so
    an error message can blame the parameter the caller actually typed rather
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


# Spellings removed on 2026-09-10. Sending one is a 422 naming its
# replacement rather than a silent no-op: an old build sending `lat` would
# otherwise get the national list with a 200 and a location filter that
# looked applied, which is the failure described at the top of this module.
#
# An EMPTY value still counts as absent, same as everywhere else here: `lat=`
# from a build whose customer denied location asked for no filter, and gets
# none.
RENAMED_COORDINATES = {
    "lat": "latitude",
    "lng": "longitude",
    "lon": "longitude",
}
RENAMED_DISCOVERY = {**RENAMED_COORDINATES, "open_now": "is_open_now"}


def _reject_renamed(params, renamed):
    for old, new in renamed.items():
        if _raw(params, old)[1] is not None:
            raise ParamError(old, f"{old} was renamed to {new}.")


def parse_services(params):
    tenant_id = _uuid(params, "tenant_id")
    branch_id = _uuid(params, "branch_id")
    category_id = _uuid(params, "category_id")   # optional

    if tenant_id is None:
        raise ParamError("tenant_id", "This parameter is required.")
    # if branch_id is None:
    #     raise ParamError("branch_id", "This parameter is required.")

    return {
        "tenant_id": tenant_id,
        "branch_id": branch_id,
        "category_id": category_id,
    }

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
    _reject_renamed(params, RENAMED_DISCOVERY)

    latitude = _number(params, "latitude", minimum=-90, maximum=90)
    longitude = _number(params, "longitude", minimum=-180, maximum=180)

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
        "is_open_now": _boolean(params, "is_open_now"),
        "hijab_only": _boolean(params, "hijab_mode", "hijab_only"),
    }



def parse_map(params):
    """
    The /discover/map query string, settled.

    radius arrives in METRES here, unlike /discover which takes
    kilometres, because the map sends a viewport size. It is
    converted once, right here, so nothing downstream sees metres.
    """
    _reject_renamed(params, RENAMED_COORDINATES)

    latitude = _number(params, "latitude", minimum=-90, maximum=90)
    longitude = _number(params, "longitude", minimum=-180, maximum=180)

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





def _uuid(params, name):
    value = params.get(name)
    if not value or not value.strip():
        return None
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        raise ParamError(name, "Must be a valid UUID.")


def parse_stylists(params):
    tenant_id = _uuid(params, "tenant_id")
    branch_id = _uuid(params, "branch_id")

    if tenant_id is None:
        raise ParamError("tenant_id", "This parameter is required.")
    if branch_id is None:
        raise ParamError("branch_id", "This parameter is required.")

    return {"tenant_id": tenant_id, "branch_id": branch_id}

# One booking cannot plausibly be 50 services long. The cap is not a product
# rule, it is a ceiling on how big a query string can make the skill lookup.
MAX_SERVICE_IDS = 50


def parse_service_ids(params):
    """
    The `service_ids` filter on the stylists endpoint, as a list of UUIDs.

    Liberal on the way in, because three clients spell a list three ways and
    all three are already in the wild:

        service_ids=a,b          what the customer app sends
        service_ids=a&service_ids=b
        service_ids[]=a&service_ids[]=b   axios' default for an array

    All three normalise to the same list. Absent or empty means no filter at
    all — the caller wants the whole roster — and comes back as an empty list,
    which is why `parse_stylists`-style "required" checks are absent here.

    Duplicates collapse and order is kept: the response lists each stylist's
    covered services in the order the customer picked them.
    """
    raw = []
    for name in ("service_ids", "service_ids[]"):
        getlist = getattr(params, "getlist", None)
        values = getlist(name) if getlist else ([params[name]] if name in params else [])
        raw.extend(values or [])

    seen = set()
    service_ids = []
    for value in raw:
        for piece in str(value).split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                parsed = uuid.UUID(piece)
            except ValueError:
                raise ParamError(
                    "service_ids", f"Must be a list of UUIDs. Got {piece!r}."
                ) from None
            if parsed not in seen:
                seen.add(parsed)
                service_ids.append(parsed)

    if len(service_ids) > MAX_SERVICE_IDS:
        raise ParamError(
            "service_ids", f"Send {MAX_SERVICE_IDS} services or fewer."
        )

    return service_ids


def _instant(params, name):
    """
    One ISO 8601 instant from the query string, or None if it was not sent.

    Must carry an offset. A naive "2026-09-20T10:00:00" is not a moment in
    time — it is a moment in an unnamed timezone, and reading it as the
    salon's own would quietly answer a different question than the caller
    asked on the one day of the year that matters.

    Remember that a raw `+` in a query string decodes to a SPACE, so
    `+04:00` has to travel as `%2B04:00`. A caller who forgets sends
    "2026-09-20T10:00:00 04:00"; the space is put back before parsing so that
    mistake reads as the time it obviously means rather than as a 422 nobody
    can decipher from the URL they typed.
    """
    raw = params.get(name)
    if raw is None or not str(raw).strip():
        return None

    raw = str(raw).strip().replace(" ", "+")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise ParamError(
            name,
            f"Must be an ISO 8601 time with an offset, e.g. "
            f"2026-09-20T10:00:00+04:00. Got {raw!r}.",
            code="invalid_window",
        ) from None

    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ParamError(
            name,
            "Must carry a UTC offset, e.g. 2026-09-20T10:00:00+04:00 or a Z suffix.",
            code="invalid_window",
        )
    return moment


def parse_window(params, tz):
    """
    The `from`/`to` window, settled against the salon's own clock.

    Returns (start, end) as aware datetimes. `start` is inclusive, `end`
    exclusive, and both are the instants the caller sent — converting them to
    the salon's offset is the response's job, not the filter's.

    `tz` is the salon's timezone, needed because "the same day" is a question
    about the salon's calendar, not the caller's. A customer in London asking
    a Dubai salon about 22:00-23:00 local is asking about tomorrow morning
    there, and that window is one salon day, not two.
    """
    start = _instant(params, "from")
    end = _instant(params, "to")

    for name, value in (("from", start), ("to", end)):
        if value is None:
            raise ParamError(
                name, "This parameter is required.", code="invalid_window"
            )

    if end <= start:
        raise ParamError(
            "to",
            "The end of the window must come after the start.",
            code="invalid_window",
        )

    # `end` is EXCLUSIVE, so a window closing at exactly midnight closes the
    # day it belongs to rather than opening the next one: 20:00 to 00:00 is
    # one evening, and rejecting it would reject the most obvious "rest of
    # today" request the app can make.
    local_start = start.astimezone(tz).date()
    local_end = (end.astimezone(tz) - timedelta(microseconds=1)).date()
    if local_start != local_end:
        raise ParamError(
            "to",
            "The window must start and end on the same day at the salon.",
            code="invalid_window",
        )

    return start, end


def parse_nearest_available(params, tz):
    """
    Everything `GET /booking/nearest-available/:id` takes.

    `service_ids` says who is qualified to search across, `stylist_id` names
    one person to search. Sending both narrows to that stylist for those
    services; sending neither leaves nothing to search at all, which is the
    one thing this endpoint cannot answer.
    """
    start, end = parse_window(params, tz)
    service_ids = parse_service_ids(params)
    stylist_id = _uuid(params, "stylist_id")

    if not service_ids and stylist_id is None:
        raise ParamError(
            "service_ids",
            "Send service_ids, stylist_id, or both.",
            code="missing_filter",
        )

    return {
        "start": start,
        "end": end,
        "service_ids": service_ids,
        "stylist_id": stylist_id,
    }

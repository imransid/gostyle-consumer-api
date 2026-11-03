"""
The map's Filters drawer (MAP_FILTER_V1, docs/MAP_FILTERS_API.md).

BOTH ENDS LIVE HERE, so they cannot drift. GET /me/map/filters lists the
drawer's sections and their option ids (`drawer`), and GET /discover/map
reads the same ids back (`parse_map_filters`, then `filtered_map_venues`):
the param is the section's key, its value the picked ids
(docs/map-filter-params-fe-contract.md). An id this module offers is
exactly an id it accepts, with no mapping table in between.

THE SERVICE CATEGORIES ARE NAMES. Every salon makes its own categories on the
platform (its own "Hair", with its own id), so one salon's category id means
nothing at another salon. A drawer option is one category NAME across every
public salon, filed the way the salon's Services tab files a service (its
category's parent when it has one: menu.category_chip), and its id is a UUID
made from that name (uuid5), so the same name is the same id on every call.
"""

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.db.models import Exists, OuterRef, Subquery, TextField

from apps.platform_data.models import (
    Branch,
    Service,
    ServiceBranchAvailability,
    Storefront,
    StorefrontVersion,
)

from . import map_availability
from . import verified as verified_rule
from .menu import OTHER_CHIP, category_chip
from .params import ParamError, _number, _raw
from .selectors import (
    BRANCH_AVAILABILITY_ENABLED,
    categories_for_tenants,
    filter_by_radius,
    map_venue_cap,
    map_venue_queryset,
    with_distance,
    with_published_card_fields,
)
from .serializers import SalonCardSerializer

# "No filter" in a single-choice section, and the drawer's own first chip
# there. Sent anyway, it is read as not sent, in any section.
ANY = "any"

# (id, label) in the order the drawer shows them.
RATING_OPTIONS = (("3", "3.0+"), ("4", "4.0+"), ("4.5", "4.5+"), ("5", "5.0"))
# MAP_RATING_FIGMA_V1: the Figma's chips, highest first (audit D7). No "4.5":
# with the switch on, an app that still sends it gets the 400 any id the
# drawer does not offer gets.
RATING_OPTIONS_FIGMA = (("5", "5"), ("4", "4.0+"), ("3", "3.0+"), ("2", "2.0+"), ("1", "1.0+"))
# Every rating id either set offers, for the docs' enum.
RATING_IDS = ("1", "2", "3", "4", "4.5", "5")
AVAILABILITY_OPTIONS = (("today", "Today"), ("tomorrow", "Tomorrow"), ("week", "This week"))
DISTANCE_OPTIONS = (("5", "5 km"), ("10", "10 km"), ("20", "20 km"), ("100", "100 km"))
# "verified" only with MAP_FILTER_VERIFIED_V1 (additional_options): the
# platform has no "verified by Go Style" mark, so it is our staff's
# (verified.py). Off, the chip is left out and `additional=verified` is
# refused like any other id the drawer does not offer (the FE doc's own rule).
ADDITIONAL_OPTIONS = (("open_now", "Open now"),)
# MAP_FILTER_PARKING_V1: the salon's profile lists parking among its
# amenities. "Parking", not "Free parking": the platform's amenity does not
# say whether it is free. Off, the drawer does not offer it and the map
# refuses it, as before.
PARKING_OPTION = ("parking", "Parking")
# The platform's own key in the profile's Amenities row (translate.py).
PARKING_AMENITY = "PARKING"


def additional_options():
    """The Additional section's (id, label) pairs, with the switches in force."""
    options = ADDITIONAL_OPTIONS
    if verified_rule.is_on():
        options = (*options, verified_rule.OPTION)
    if settings.MAP_FILTER_PARKING_V1:
        options = (*options, PARKING_OPTION)
    return options

# Kept when its average rating is at least the pick (the FE doc, section 3):
# "4" keeps 4.0 and up, "5" keeps 5.0 only. The plain average, not the pin's
# rounded one: 3.96 shows "4.0" on its pin and does NOT pass "4". Every id of
# either set: which ones may be sent is the parser's job (rating_options).
RATING_FLOORS = {rating_id: float(rating_id) for rating_id in RATING_IDS}


def rating_options():
    """The Rating section's (id, label) pairs, with the switch in force."""
    if settings.MAP_RATING_FIGMA_V1:
        return RATING_OPTIONS_FIGMA
    return RATING_OPTIONS

# The distance from the customer, beside the map's own `distance_km` (from
# the map's centre, for `radius`).
ORIGIN_DISTANCE = "origin_distance_km"

# FIXED FOREVER: a category option's id is uuid5(this, its name). Changing it
# changes every id, and every drawer the app has loaded stops matching.
CATEGORY_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://api.gostyle.uk/api/v1/me/map/filters/service_categories"
)

# The service categories change when a salon edits its menu, not per call.
CATALOG_CACHE_KEY = "map-filters:categories:v1"
CATALOG_SECONDS = 60


def category_name_key(name):
    """' Hair  Cut ' and 'hair cut' are one category on the map."""
    return " ".join((name or "").split()).casefold()


def category_option_id(name):
    """The drawer's id for a category name: the same name, the same id. None for no name."""
    key = category_name_key(name)
    return str(uuid.uuid5(CATEGORY_NAMESPACE, key)) if key else None


def service_option_id(categories, category_id):
    """
    The drawer's id for a service filed under `category_id`, or None when it
    has no chip of its own (no category, or a deleted one: "Other").

    `categories` is `selectors.salon_categories` (id to row), as for the tab.
    """
    chip = category_chip(categories, category_id)
    if chip["id"] == OTHER_CHIP["id"]:
        return None
    return category_option_id(chip["label"])


def category_catalog():
    """
    Every service category the map can filter on, as
    {option id: {"label", "category_ids", "salons"}}.

    A category is on the map when at least one public salon has a service in
    it on its menu (the Services tab's rules: published, not deleted, open to
    online booking). `category_ids` is every platform category filed under
    that name (the chip and its children, in every salon), `salons` how many
    public salons offer it, `label` the spelling most salons use.
    """
    cached = cache.get(CATALOG_CACHE_KEY)
    if cached is not None:
        return cached

    salons_of = Counter(
        Storefront.objects.filter(visibility="PUBLIC", deleted_at__isnull=True)
        .values_list("tenant_id", flat=True)
    )
    on_menu = (
        Service.objects.filter(
            tenant_id__in=list(salons_of),
            status="PUBLISHED",
            deleted_at__isnull=True,
            online_booking_enabled=True,
            category_0_id__isnull=False,
        )
        .values_list("tenant_id", "category_0_id")
        .distinct()
    )
    catalog = build_catalog(salons_of, categories_for_tenants(salons_of), on_menu)
    cache.set(CATALOG_CACHE_KEY, catalog, CATALOG_SECONDS)
    return catalog


def build_catalog(salons_of, categories, on_menu):
    """
    `category_catalog` from rows already read: `salons_of` is {tenant id:
    public salons}, `categories` the tenants' live categories
    (categories_for_tenants), `on_menu` (tenant id, category id) of each
    category a service on a menu is filed under.
    """
    ids_by_option = defaultdict(set)
    for category_id in categories:
        option_id = service_option_id(categories, category_id)
        if option_id:
            ids_by_option[option_id].add(category_id)

    tenants_by_option = defaultdict(set)
    labels = defaultdict(Counter)
    seen = set()
    for tenant_id, category_id in on_menu:
        option_id = service_option_id(categories, category_id)
        if not option_id:
            continue
        tenants_by_option[option_id].add(tenant_id)
        label = " ".join(category_chip(categories, category_id)["label"].split())
        if (tenant_id, option_id) not in seen:
            seen.add((tenant_id, option_id))
            labels[option_id][label] += 1

    catalog = {}
    for option_id, tenants in tenants_by_option.items():
        # The most used spelling, then the first in the alphabet, so a tie
        # does not swap the label between calls.
        label = min(labels[option_id].items(), key=lambda item: (-item[1], item[0]))[0]
        catalog[option_id] = {
            "label": label,
            "category_ids": frozenset(ids_by_option[option_id]),
            "salons": sum(salons_of[t] for t in tenants),
        }
    return catalog


def _options(pairs, with_any):
    options = [{"id": ANY, "label": "Any"}] if with_any else []
    return options + [{"id": option_id, "label": label} for option_id, label in pairs]


def drawer():
    """GET /me/map/filters: the sections in the drawer's order, each with its options."""
    catalog = category_catalog()
    categories = sorted(
        catalog.items(),
        # The most offered first, then by name; the id settles a tie.
        key=lambda item: (-item[1]["salons"], item[1]["label"].casefold(), item[0]),
    )
    return {
        "sections": [
            {
                "key": "service_categories",
                "title": "Service Categories",
                "multiple": True,
                "default": [],
                "options": [{"id": option_id, "label": entry["label"]} for option_id, entry in categories],
            },
            {
                "key": "rating",
                "title": "Rating",
                "multiple": False,
                "default": [ANY],
                "options": _options(rating_options(), with_any=True),
            },
            {
                "key": "availability",
                "title": "Availability",
                "multiple": False,
                "default": [ANY],
                "options": _options(AVAILABILITY_OPTIONS, with_any=True),
            },
            {
                "key": "distance",
                "title": "Distance",
                "multiple": False,
                "default": [ANY],
                "options": _options(DISTANCE_OPTIONS, with_any=True),
            },
            {
                "key": "additional",
                "title": "Additional",
                "multiple": True,
                # The drawer opens with "Open now" on (the FE doc, section 2).
                "default": ["open_now"],
                "options": _options(additional_options(), with_any=False),
            },
        ]
    }


@dataclass(frozen=True)
class MapFilters:
    """The five params of GET /discover/map, read. Empty: a plain map call."""

    # The picked category ids, as sent (unknown ones are dropped later,
    # against the catalog).
    service_categories: tuple = ()
    rating: str | None = None
    availability: str | None = None
    distance_km: float | None = None
    origin: tuple | None = None
    open_now: bool = False
    # MAP_FILTER_VERIFIED_V1: additional=verified, or verified=true.
    verified: bool = False
    parking: bool = False

    @property
    def active(self):
        return bool(
            self.service_categories
            or self.rating
            or self.availability
            or self.distance_km is not None
            or self.open_now
            or self.verified
            or self.parking
        )


NO_FILTERS = MapFilters()


def _single(params, name, pairs):
    """One option id, or None for not sent (or `any`)."""
    _, raw = _raw(params, name)
    if raw is None or raw.lower() == ANY:
        return None
    ids = [option_id for option_id, _ in pairs]
    if raw.lower() not in ids:
        raise ParamError(
            name,
            f"Expected one of: {', '.join([ANY, *ids])}. Got {raw!r}.",
            code="invalid_choice",
        )
    return raw.lower()


def _multiple(params, name):
    """The ids of a comma separated list, in order, each once, without `any`."""
    getlist = getattr(params, "getlist", None)
    values = getlist(name) if getlist else [params.get(name)]
    picked = []
    for value in values:
        for piece in str(value or "").split(","):
            piece = piece.strip().lower()
            if piece and piece != ANY and piece not in picked:
                picked.append(piece)
    return tuple(picked)


def parse_map_filters(params):
    """
    The five params, read and checked against the drawer's own options.

    A ParamError names the param to blame, as everywhere in params.py. Unknown
    ids are refused in rating, availability, distance and additional, but NOT
    in service_categories: a category may be gone since the app loaded the
    drawer, so an unknown one is dropped later (`filtered_map_venues`), never
    refused.
    """
    rating = _single(params, "rating", rating_options())
    availability = _single(params, "availability", AVAILABILITY_OPTIONS)
    distance = _single(params, "distance", DISTANCE_OPTIONS)

    origin_latitude = _number(params, "origin_latitude", minimum=-90, maximum=90)
    origin_longitude = _number(params, "origin_longitude", minimum=-180, maximum=180)
    origin = (("origin_latitude", origin_latitude), ("origin_longitude", origin_longitude))
    if distance is not None:
        for name, value in origin:
            if value is None:
                raise ParamError(
                    name, "Send origin_latitude and origin_longitude with distance.", code="required"
                )
    else:
        for name, value in origin:
            if value is not None:
                raise ParamError(
                    name, "Send origin_latitude and origin_longitude only with distance."
                )

    additional = _multiple(params, "additional")
    offered = [option_id for option_id, _ in additional_options()]
    for option_id in additional:
        if option_id not in offered:
            raise ParamError(
                "additional",
                f"Expected one or more of: {', '.join(offered)}. Got {option_id!r}.",
                code="invalid_choice",
            )

    return MapFilters(
        service_categories=_multiple(params, "service_categories"),
        rating=rating,
        availability=availability,
        distance_km=float(distance) if distance is not None else None,
        origin=(origin_latitude, origin_longitude) if distance is not None else None,
        open_now="open_now" in additional,
        # The app's map contract sends additional=verified; verified=true is
        # the same ask (MAP_FILTER_VERIFIED_V1; off, never true).
        verified=verified_rule.OPTION[0] in additional or verified_rule.wanted(params),
        parking=PARKING_OPTION[0] in additional,
    )


def _on_menu_in(category_ids):
    """
    EXISTS: the salon has a service on its menu filed under one of
    `category_ids`. The Services tab's rules (selectors.salon_services):
    this business's service, published, not deleted, open to online booking,
    and offered at this branch when BRANCH_AVAILABILITY_ENABLED is on.
    """
    services = Service.objects.filter(
        tenant_id=OuterRef("tenant_id"),
        status="PUBLISHED",
        deleted_at__isnull=True,
        online_booking_enabled=True,
        category_0_id__in=list(category_ids),
    )
    if BRANCH_AVAILABILITY_ENABLED:
        services = services.filter(
            Exists(
                ServiceBranchAvailability.objects.filter(
                    service_id=OuterRef("pk"),
                    branch_id=OuterRef(OuterRef("branch_id")),
                    available=True,
                )
            )
        )
    return Exists(services)


def _has_amenity(key):
    """
    EXISTS: the salon's published profile lists the amenity. The live
    version, the one the profile's Amenities row is read from
    (snapshot.read_snapshot, translate.amenities). A version whose list is
    missing or not a list has no amenity: jsonb's @> is false on it.
    """
    return Exists(
        StorefrontVersion.objects.filter(
            id=OuterRef("live_version_id"),
            snapshot__AMENITIES__items__contains=[key],
        )
    )


def filtered_map_venues(map_params, filters, now):
    """
    GET /discover/map's venues with the drawer's filters on, as (venues,
    count): `venues` a list at most the map's cap long, `count` every salon
    in view that matches, before the cap (the FE doc, section 4).
    `map_params` is params.parse_map's answer, `now` an aware datetime.

    FILTER FIRST, THEN THE CAP. Cheapest first (the FE doc, 5.1): the box,
    the category and the radius (map_venue_queryset), the rating, the
    distance from the customer, parking and the service categories in SQL
    (`filtered_queryset`); then "open now" and the availability, which need
    each salon's own clock, in Python on what is left, availability last
    (`keep_open_and_available`).
    """
    qs, picked = filtered_queryset(map_params, filters)
    cap = map_venue_cap(map_params["limit"])
    if not filters.open_now and filters.availability is None:
        if (
            picked is None
            and filters.rating is None
            and filters.distance_km is None
            and not filters.verified
            and not filters.parking
        ):
            # Every category id unknown and nothing else: as if no param was
            # sent (the FE doc, section 6), so `count` as on a plain call.
            venues = list(qs[:cap])
            return venues, len(venues)
        return list(qs[:cap]), qs.count()

    # What "open now" and the slots are read from: the published hours,
    # today's hand-set state (which needs the branch's timezone) and the
    # branch's timezone itself.
    qs = with_published_card_fields(
        qs.annotate(
            branch_timezone=Subquery(
                Branch.objects.filter(id=OuterRef("branch_id")).values("timezone")[:1],
                output_field=TextField(),
            )
        )
    )
    kept = keep_open_and_available(list(qs), filters, picked, now)
    return kept[:cap], len(kept)


def keep_open_and_available(rows, filters, picked, now):
    """
    The rows open now (when asked) and with a start left (when asked), in
    their order, ALL of them: `count` is every match in view, so the
    availability is worked out for every salon left, in one call (one read of
    who is busy), not only until the cap is full.
    """
    if filters.open_now:
        # /discover's is_open_now, by the card's own rule (SalonCardSerializer.get_open).
        card = SalonCardSerializer()
        rows = [row for row in rows if card.get_open(row)]

    if filters.availability is None or not rows:
        return rows

    keep = map_availability.available_ids(rows, filters.availability, picked, now)
    return [row for row in rows if row.pk in keep]


def filtered_queryset(map_params, filters):
    """
    The SQL half of `filtered_map_venues`: the map's own query (the customer's
    country first, when the view found one: MAP_COUNTRY_V1) narrowed by the
    rating, the distance, parking and the service categories, ordered, not capped.
    Returns (queryset, the picked category ids the catalog knows, or None).
    """
    qs = map_venue_queryset(
        latitude=map_params["latitude"],
        longitude=map_params["longitude"],
        latitude_delta=map_params["latitude_delta"],
        longitude_delta=map_params["longitude_delta"],
        category=map_params["category"],
        radius_km=map_params["radius_km"],
        country=map_params.get("country"),
    )

    if filters.rating is not None:
        # NULL (no review yet) fails `>=`, so an unrated salon is left out.
        qs = qs.filter(avg_rating__gte=RATING_FLOORS[filters.rating])

    if filters.verified:
        # map_venue_queryset carries is_verified while the switch is on; a
        # salon with no flag row is not verified.
        qs = qs.filter(is_verified=True)

    if filters.distance_km is not None:
        origin_latitude, origin_longitude = filters.origin
        qs = with_distance(qs, origin_latitude, origin_longitude, name=ORIGIN_DISTANCE)
        # The map keeps its own order: the FE doc changes nothing in the answer.
        qs = filter_by_radius(
            qs, origin_latitude, origin_longitude, filters.distance_km, name=ORIGIN_DISTANCE
        )

    if filters.parking:
        qs = qs.filter(_has_amenity(PARKING_AMENITY))

    picked = None
    if filters.service_categories:
        catalog = category_catalog()
        picked = [option_id for option_id in filters.service_categories if option_id in catalog]
        if picked:
            category_ids = set().union(*(catalog[option_id]["category_ids"] for option_id in picked))
            qs = qs.filter(_on_menu_in(category_ids))
        else:
            # Every id unknown: as if the param were not sent.
            picked = None

    return qs, picked

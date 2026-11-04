"""
The Search Results screens (SEARCH_RESULTS_V1, docs/SEARCH_RESULTS_API.md):
GET /search/salons (the Salons tab) and GET /search/services (the Services
tab), with every filter at once. The views are search_views.py; this reads
the params and builds the lists.

NOTHING IS COPIED. A salon is the /discover card (discoverable_salons, the
card's fields); a service is the /services search result (service_search);
the filter ids are the map drawer's (map_filters), checked by its own
helpers; availability is the map's (map_availability). So a salon cannot
read one way on the map and another way here.

Two halves, as on the map: what SQL can say (position, gender, rating,
distance, parking, service categories, the text), then what needs each
salon's own clock or its packages, in Python on what is left (open now,
packages, availability last).
"""

from dataclasses import dataclass

from django.conf import settings
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from apps.platform_data.models import Service, ServiceMedia, Storefront, StorefrontMedia, StorefrontVersion

from . import map_availability, map_filters, service_search
from .geo import format_distance
from .map_filters import (
    ADDITIONAL_OPTIONS,
    AVAILABILITY_OPTIONS,
    DISTANCE_OPTIONS,
    RATING_FLOORS,
    _multiple,
    _single,
    rating_options,
)
from .menu import service_price_minor
from .params import (
    CATEGORY_CHOICES,
    SEARCH_MAX_LENGTH,
    SERVICE_SEARCH_PAGE_SIZE,
    ParamError,
    _choice,
    _number,
    _page_size,
    _text,
)
from .selectors import (
    BRANCH_AVAILABILITY_ENABLED,
    BookingCountsUnavailable,
    average_salon_rating,
    branch_service_rows,
    categories_for_tenants,
    discoverable_salons,
    filter_by_category,
    filter_by_radius,
    salon_packages,
    service_booking_counts,
    with_distance,
    with_popular_score,
    with_published_card_fields,
)
from .serializers import SalonCardSerializer

# (id, label) in the order the Smart Search shows them. GET /search/options
# (P3) will serve these labels; the ids are what the two routes take.
TAB_OPTIONS = (("near_me", "Near Me"), ("packages", "Packages"))
# "parking": the profile lists parking, or the salon shows the Free Parking
# badge. "free_parking": only the badge, the one place that says free (D8).
# "packages": the salon's Packages tab lists at least one package (D14).
AMENITY_OPTIONS = (("parking", "Parking"), ("free_parking", "Free Parking"), ("packages", "Packages Use"))

SALON_SORTS = frozenset({"distance", "rating", "popular"})
SERVICE_SORTS = frozenset({"distance", "rating", "popular", "price"})

# The platform's keys in the published profile (storefront_version.snapshot).
PARKING_AMENITY = "PARKING"
FREE_PARKING_BADGE = "FREE_PARKING"

# The params whose ids come from the map drawer's lists (and the Smart
# Search's): a bad one is the map's 400, every other param the usual 422.
DRAWER_PARAMS = frozenset({"service_categories", "rating", "availability", "distance", "additional", "amenities", "tab"})


@dataclass(frozen=True)
class SearchQuery:
    """GET /search/salons' or /search/services' params, read."""

    q: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    tab: str | None = None
    gender: str | None = None
    service_categories: tuple = ()
    rating: str | None = None
    availability: str | None = None
    distance_km: float | None = None
    open_now: bool = False
    amenities: tuple = ()
    sort: str = "rating"
    page_size: int = SERVICE_SEARCH_PAGE_SIZE

    @property
    def has_position(self):
        return self.latitude is not None

    @property
    def wants_packages(self):
        return self.tab == "packages" or "packages" in self.amenities


def _ids(params, name, pairs):
    """A comma separated list of ids from `pairs`, each once; one not offered is refused."""
    picked = _multiple(params, name)
    offered = [option_id for option_id, _ in pairs]
    for option_id in picked:
        if option_id not in offered:
            raise ParamError(
                name,
                f"Expected one or more of: {', '.join(offered)}. Got {option_id!r}.",
                code="invalid_choice",
            )
    return picked


def parse_search(params, sorts, default_sort):
    """
    The query string of either route, checked. `sorts` are the route's
    sort ids; `default_sort` is used without a position (with one, the
    nearest first). Raises ParamError naming the param to blame.
    """
    q = _text(params, "q", max_length=SEARCH_MAX_LENGTH)
    latitude = _number(params, "latitude", minimum=-90, maximum=90)
    longitude = _number(params, "longitude", minimum=-180, maximum=180)
    if (latitude is None) != (longitude is None):
        missing = "longitude" if longitude is None else "latitude"
        raise ParamError(missing, "Send latitude and longitude together.", code="required")

    gender = _choice(params, "gender", choices=CATEGORY_CHOICES)
    tab = _single(params, "tab", TAB_OPTIONS)
    rating = _single(params, "rating", rating_options())
    availability = _single(params, "availability", AVAILABILITY_OPTIONS)
    distance = _single(params, "distance", DISTANCE_OPTIONS)
    additional = _ids(params, "additional", ADDITIONAL_OPTIONS)
    amenities = _ids(params, "amenities", AMENITY_OPTIONS)
    sort = _choice(params, "sort", choices=sorts)
    page_size = _page_size(params, "pageSize", "page_size") or SERVICE_SEARCH_PAGE_SIZE

    # Each of these measures from the customer, so it needs the position.
    if latitude is None:
        for name, asked in (("distance", distance), ("tab", tab == "near_me"), ("sort", sort == "distance")):
            if asked:
                raise ParamError(name, "Needs latitude and longitude to measure from.", code="required")

    if sort is None:
        sort = "distance" if latitude is not None else default_sort

    return SearchQuery(
        q=q,
        latitude=latitude,
        longitude=longitude,
        tab=tab,
        gender=None if gender == "all" else gender,
        service_categories=_multiple(params, "service_categories"),
        rating=rating,
        availability=availability,
        distance_km=float(distance) if distance is not None else None,
        open_now="open_now" in additional,
        amenities=amenities,
        sort=sort,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# The salons: SQL first
# ---------------------------------------------------------------------------


def _on_menu(where):
    """
    EXISTS: the salon has a service on its menu, by the Services tab's rules
    (published, not deleted, open to online booking, and offered at this
    branch when BRANCH_AVAILABILITY_ENABLED is on), that matches `where`.
    """
    services = Service.objects.filter(
        where,
        tenant_id=OuterRef("tenant_id"),
        status="PUBLISHED",
        deleted_at__isnull=True,
        online_booking_enabled=True,
    )
    if BRANCH_AVAILABILITY_ENABLED:
        from apps.platform_data.models import ServiceBranchAvailability

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


def _published(**lookup):
    """EXISTS: the salon's live published profile has `lookup` (a jsonb @>)."""
    return Exists(StorefrontVersion.objects.filter(id=OuterRef("live_version_id"), **lookup))


def public_categories():
    """Every live category of every public salon's business, by id (the chip labels' source)."""
    tenants = Storefront.objects.filter(visibility="PUBLIC", deleted_at__isnull=True).values_list(
        "tenant_id", flat=True
    )
    return categories_for_tenants(set(tenants))


def service_text(q, categories):
    """
    A service the text finds: its name, its chip's label (as GET /services
    across salons) or one of its tags holds `q`, case ignored. `tags` is
    the platform's text[]: matched as text.
    """
    return (
        Q(name__icontains=q)
        | Q(category_0_id__in=list(service_search.label_ids(categories, q)))
        | Q(tags__icontains=q)
    )


def salon_queryset(query, *, with_text=True, categories=None):
    """
    The salons the SQL can narrow down, card fields on, not ordered:
    (queryset, the picked category ids the drawer knows, or None).

    `with_text` False leaves `q` out: the Services tab matches the text on
    each service, not on the salon. `categories` is public_categories(),
    read only when the text needs it.
    """
    qs = with_published_card_fields(discoverable_salons())

    if query.has_position:
        # A salon with no pin has no distance (as /discover with a position).
        qs = with_distance(qs, query.latitude, query.longitude).filter(lat__isnull=False, lng__isnull=False)
        if query.distance_km is not None:
            qs = filter_by_radius(qs, query.latitude, query.longitude, query.distance_km)

    # Strict, as /discover and the map: Gents is gents only (D13).
    qs = filter_by_category(qs, query.gender)

    if query.rating is not None:
        # NULL (no review yet) fails >=: an unrated salon is left out.
        qs = qs.filter(avg_rating__gte=RATING_FLOORS[query.rating])

    if "parking" in query.amenities:
        qs = qs.annotate(
            lists_parking=_published(snapshot__AMENITIES__items__contains=[PARKING_AMENITY]),
            shows_free_parking=_published(snapshot__BADGES__manualBadges__contains=[FREE_PARKING_BADGE]),
        ).filter(Q(lists_parking=True) | Q(shows_free_parking=True))
    if "free_parking" in query.amenities:
        qs = qs.filter(_published(snapshot__BADGES__manualBadges__contains=[FREE_PARKING_BADGE]))

    picked = None
    if query.service_categories:
        catalog = map_filters.category_catalog()
        picked = [option_id for option_id in query.service_categories if option_id in catalog]
        if picked:
            category_ids = set().union(*(catalog[option_id]["category_ids"] for option_id in picked))
            qs = qs.filter(_on_menu(Q(category_0_id__in=list(category_ids))))
        else:
            # Every id unknown (gone since the app loaded the list): no filter.
            picked = None

    if with_text and query.q:
        if categories is None:
            categories = public_categories()
        # D11: the salon's name or city, OR a service on its menu.
        qs = qs.annotate(offers_text=_on_menu(service_text(query.q, categories))).filter(
            Q(published_name__icontains=query.q)
            | Q(branch_name__icontains=query.q)
            | Q(branch_city__icontains=query.q)
            | Q(offers_text=True)
        )

    return qs, picked


# ---------------------------------------------------------------------------
# The salons: Python on what is left
# ---------------------------------------------------------------------------


def tab_has_packages(salon):
    """
    The salon's Packages tab lists at least one package, by the rules that
    tab uses right now (D14): PACKAGES_LIVE_RULES_V1 decides both.
    """
    if settings.PACKAGES_LIVE_RULES_V1:
        from .views import SalonPackagesView

        return bool(SalonPackagesView.live_bundles(salon))
    return salon_packages(salon).exists()


def keep(rows, query, picked, now):
    """
    `rows` (card rows, in any order) that are open now, list packages and
    have a start left, each only when asked. Availability last and in one
    call: it reads who is busy once for every salon left. Raises
    map_availability.AvailabilityUnavailable when that cannot be read.
    """
    if query.open_now:
        card = SalonCardSerializer()
        rows = [row for row in rows if card.get_open(row)]
    if query.wants_packages:
        rows = [row for row in rows if tab_has_packages(row)]
    if query.availability is not None and rows:
        found = map_availability.available_ids(rows, query.availability, picked, now)
        rows = [row for row in rows if row.pk in found]
    return rows


def needs_python(query):
    return query.open_now or query.wants_packages or query.availability is not None


def salon_ordering(query):
    """The ORDER BY of the Salons tab, always ending in `id` so pages never shift."""
    if query.sort == "distance":
        return ("distance_km", "id")
    if query.sort == "popular":
        nearest = ("distance_km",) if query.has_position else ()
        return (F("popular_score").desc(nulls_last=True), "-review_count", *nearest, "id")
    return (F("avg_rating").desc(nulls_last=True), "-review_count", "id")


def salons(query, now=None):
    """
    GET /search/salons' list, ordered, not paged: a queryset of card rows.
    `now` is aware (default: now). Raises AvailabilityUnavailable.
    """
    qs, picked = salon_queryset(query)
    if needs_python(query):
        kept = keep(list(qs), query, picked, now or timezone.now())
        qs = qs.filter(pk__in=[row.pk for row in kept])
    if query.sort == "popular":
        qs = with_popular_score(qs, average_salon_rating())
    return qs.order_by(*salon_ordering(query))


# ---------------------------------------------------------------------------
# The services
# ---------------------------------------------------------------------------

SERVICE_FIELDS = (
    "id",
    "tenant_id",
    "name",
    "description",
    "price_minor",
    "currency",
    "duration_minutes",
    "category_0_id",
    "created_at",
    "tags",
)

# At most this many photos on a result (the Figma's collage).
IMAGES = 4


def menu_rows(tenant_ids, query, categories, category_ids):
    """
    The services of these businesses' menus the Services tab lists, as
    dicts: the Services tab's rules, the text (service_text, plus "Other"
    as GET /services finds it) and the picked categories.
    """
    tenant_ids = list(tenant_ids)
    if not tenant_ids:
        return []
    qs = Service.objects.filter(
        tenant_id__in=tenant_ids,
        status="PUBLISHED",
        deleted_at__isnull=True,
        online_booking_enabled=True,
    )
    if query.q:
        found = service_text(query.q, categories)
        if service_search.finds_other(query.q):
            live = list(categories)
            found |= Q(category_0_id__isnull=True) | ~Q(category_0_id__in=live)
        qs = qs.filter(found)
    if category_ids is not None:
        qs = qs.filter(category_0_id__in=list(category_ids))
    return list(qs.values(*SERVICE_FIELDS))


def _tags(service):
    tags = service.get("tags")
    if isinstance(tags, (list, tuple)):
        return [str(tag) for tag in tags]
    return [str(tags)] if tags else []


def service_matches(row, categories, q):
    """The rule behind menu_rows' narrowing: name, chip label, or a tag holds `q`."""
    wanted = q.lower()
    return service_search.matches(row, categories, q) or any(wanted in tag.lower() for tag in _tags(row.service))


def _salon_dict(salon):
    """A card row as the dict service_search reads, plus the logo and the distance."""
    return {
        "id": salon.pk,
        "tenant_id": salon.tenant_id,
        "branch_id": salon.branch_id,
        "published_name": getattr(salon, "published_name", None),
        "branch_name": salon.branch_name,
        "category": getattr(salon, "category", None),
        "avg_rating": salon.avg_rating,
        "review_count": getattr(salon, "review_count", 0),
        "logo_url": getattr(salon, "logo_url", None),
        "distance_km": getattr(salon, "distance_km", None),
    }


def _ids_of(row):
    return (str(row.salon["id"]), str(row.service["id"]))


def _newest(row):
    created = row.service["created_at"]
    return -created.timestamp() if created is not None else float("inf")


def order_services(rows, query, page_wanted):
    """
    The Services tab's order. distance: the salon's, nearest first. rating:
    the salon's, best first, unrated last. price: the price there, lowest
    first. popular: GET /services' is_popular (most booked in 30 days, at
    most 2 of one salon on a page). Ties: the newest, then the ids.
    """
    if query.sort == "distance":
        return sorted(rows, key=lambda r: (r.salon["distance_km"], _newest(r), *_ids_of(r)))
    if query.sort == "rating":
        def by_rating(r):
            rating = r.salon["avg_rating"]
            return (rating is None, -float(rating) if rating is not None else 0.0, _newest(r), *_ids_of(r))

        return sorted(rows, key=by_rating)
    if query.sort == "price":
        return sorted(
            rows,
            key=lambda r: (
                service_price_minor({**r.service, "branch_price_minor": r.branch_price_minor}),
                _newest(r),
                *_ids_of(r),
            ),
        )
    ordered = service_search.popular_first(rows, booking_counts([r.salon for r in rows]))
    return service_search.spread(ordered, query.page_size, page_wanted)


def booking_counts(salon_rows):
    """The popular order's counts; booking-api's database down: none (the tie-break order)."""
    try:
        return service_booking_counts(
            {salon["branch_id"] for salon in salon_rows},
            service_search.popular_since(timezone.now()),
            service_search.COUNTED_STATUSES,
        )
    except BookingCountsUnavailable:
        return {}


def service_list(query, now=None):
    """
    GET /search/services' rows, ordered, not paged, and the categories
    their chips are read from: (rows, categories). The salons are the
    Salons tab's, every filter but the text; the text and the categories
    pick the services. Raises AvailabilityUnavailable.
    """
    qs, picked = salon_queryset(query, with_text=False)
    salon_rows = list(qs)
    if needs_python(query):
        salon_rows = keep(salon_rows, query, picked, now or timezone.now())
    salons_ = [_salon_dict(salon) for salon in salon_rows]

    tenants = {salon["tenant_id"] for salon in salons_}
    categories = categories_for_tenants(tenants)
    category_ids = None
    if picked:
        catalog = map_filters.category_catalog()
        category_ids = set().union(*(catalog[option_id]["category_ids"] for option_id in picked))

    services = menu_rows(tenants, query, categories, category_ids)
    rows = service_search.listing(
        salons_,
        services,
        branch_service_rows({salon["branch_id"] for salon in salons_}, with_currency=True),
    )
    if query.q:
        rows = [row for row in rows if service_matches(row, categories, query.q)]
    return rows, categories


def images_for(rows):
    """
    {(salon id, service id): up to IMAGES photo URLs} for a page of rows: the
    service's own photos, then this salon's gallery photos tagged with it
    (Service Detail's order, selectors.service_photo_urls), in two queries
    for the whole page.
    """
    if not rows:
        return {}
    service_ids = {row.service["id"] for row in rows}
    salon_ids = {row.salon["id"] for row in rows}

    own = {}
    for service_id, tenant_id, url in (
        ServiceMedia.objects.filter(service_id__in=service_ids, deleted_at__isnull=True)
        .order_by("-is_primary", "sort_order", "created_at", "id")
        .values_list("service_id", "tenant_id", "url")
    ):
        own.setdefault((tenant_id, service_id), []).append(url)

    linked = {}
    for salon_id, service_id, url in (
        StorefrontMedia.objects.filter(
            storefront_id__in=salon_ids,
            linked_service_id__in=service_ids,
            kind="GALLERY",
            is_public=True,
            moderation_status="APPROVED",
            deleted_at__isnull=True,
        )
        .order_by("sort_order", "created_at", "id")
        .values_list("storefront_id", "linked_service_id", "url")
    ):
        linked.setdefault((salon_id, service_id), []).append(url)

    found = {}
    for row in rows:
        salon_id, service_id = row.salon["id"], row.service["id"]
        urls = own.get((row.salon["tenant_id"], service_id), []) + linked.get((salon_id, service_id), [])
        found[(salon_id, service_id)] = urls[:IMAGES]
    return found


def service_result(row, categories, images):
    """
    One Services tab result: GET /services' result with its currency, the
    photos after the durations, and the salon's logo, distance and rating.
    """
    base = service_search.result(row, categories, with_currency=True)
    out = {}
    for name, value in base.items():
        if name == "salon":
            continue
        out[name] = value
        if name == "duration_max":
            out["images"] = images.get((row.salon["id"], row.service["id"]), [])
    salon = row.salon
    rating = salon["avg_rating"]
    out["salon"] = {
        "id": base["salon"]["id"],
        "name": base["salon"]["name"],
        "logo_url": salon.get("logo_url"),
        "category": base["salon"]["category"],
        "distance": format_distance(salon.get("distance_km")),
        "rating": None if rating is None else f"{float(rating):.1f}",
    }
    return out


import json
import logging
from datetime import datetime, time, timedelta, timezone as dt_timezone
from django.db.models import F
from django.http import Http404
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework.exceptions import (
    APIException,
    ErrorDetail,
    UnsupportedMediaType,
    ValidationError,
)
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from .snapshot import field as snap_field
from .params import (
    MAX_SERVICE_IDS,
    ParamError,
    parse_discovery,
    parse_map,
    parse_nearest_available,
    parse_service_ids,
    parse_services,
    parse_stylists,
)

from .booking_api import create_booking, read_booking, patch_booking, BookingApiUnavailable

from rest_framework.permissions import AllowAny, IsAuthenticated
from apps.accounts.models import Favourite

from . import slots, timezones
from .hours import resolve as resolve_hours
from .hours import weekly_row as hours_row
from .money import major
from .selectors import (
    booking_rows,
    discoverable_salons,
    filter_by_category,
    filter_by_radius,
    filter_by_search,
    filter_top_rated,
    map_venues,
    categories_for_tenants,
    salon_categories,
    salon_packages,
    salon_products,
    manual_state_on,
    salon_profile,
    salon_service_ids,
    salon_services,
    salon_stories,
    salon_stylists,
    service_stage_rows,
    services_by_ids,
    service_timing_rows,
    shift_rows,
    stylist_service_coverage,
    tenant_for_salon,
    with_distance,
    with_published_card_fields,
)
from .serializers import (
    FavouriteSerializer,
    MapVenueSerializer,
    SalonCardSerializer,
    SalonProfileSerializer,
    ServiceDetailSerializer,
    StorySalonSerializer,
)
from .snapshot import read_snapshot

logger = logging.getLogger(__name__)


# The project's error envelope, as raw OpenAPI. Shared by every view here
# that documents a refusal of its own, and shaped by apps.accounts.exceptions
# (api_exception_handler) rather than by any serializer — which is why it is
# written out rather than derived.
_OUR_ENVELOPE = {
    "type": "object",
    "properties": {
        "detail": {"type": "string"},
        "code": {"type": "string"},
        "errors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "nullable": True},
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                },
            },
        },
    },
}


class SalonListView(APIView):
    """
    GET /api/v1/salons/ — GONE.

    This read the `salons_salon` table, which is a fixture: three invented
    salons written by the `seed_salons` command. It never touched the platform
    database, so it returned the same demo list on every environment including
    production, which is exactly how it went unnoticed.

    410 rather than deletion, and rather than 404. A client on an old build
    calling this needs to be told the resource is gone for good and where the
    real one is; a 404 reads as a typo or an outage and invites a retry.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        return Response(
            {
                "detail": (
                    "GET /api/v1/salons/ has been removed. It served fixture "
                    "data, never real salons. Use GET /api/v1/discover for "
                    "salon cards or GET /api/v1/discover/map for map markers."
                ),
                "replaced_by": "/api/v1/discover",
            },
            status=status.HTTP_410_GONE,
        )


@extend_schema(
    parameters=[
        OpenApiParameter("latitude", float, description="User latitude, e.g. 25.19. Send with longitude."),
        OpenApiParameter("longitude", float, description="User longitude, e.g. 55.26. Send with latitude."),
        OpenApiParameter("radius", float, description="Only salons within this many KILOMETRES of latitude/longitude."),
        OpenApiParameter("category", str, description="Salon category. 'all' means no filter.", enum=["all", "gents", "ladies", "unisex"]),
        OpenApiParameter("search", str, description="Free text over the salon name and city."),
        OpenApiParameter("is_top_rated", bool, description="Only well-reviewed salons. See TOP_RATED_* in selectors.py."),
        OpenApiParameter("is_open_now", bool, description="Only salons open right now, in their own timezone."),
        OpenApiParameter("hijab_mode", bool, description="Only hijab-certified salons."),
        OpenApiParameter("page_size", int, description="Cards per page, up to 50. Defaults to 15."),
        OpenApiParameter("sort", str, description="Sort order. 'distance' needs latitude/longitude.", enum=["distance", "rating"]),
        OpenApiParameter("city", str, description="Filter by city name, e.g. Dubai. Exact, case-insensitive."),
        OpenApiParameter("rating_min", float, description="Minimum average rating, e.g. 4.5."),
        OpenApiParameter("total_amount", float, description="Booking total used to compute deposit.amount, e.g. 250"),
    ],
    responses=SalonCardSerializer(many=True),
)
class SalonDiscoveryListView(ListAPIView):
    """
    Figma discovery list + map screen. Public platform salons.

    Every query parameter is parsed in one place, by params.parse_discovery,
    and an unreadable one is a 422 naming it rather than a filter that
    silently did nothing. See that module for why.
    """

    serializer_class = SalonCardSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        # with_published_card_fields adds the storefront's PUBLISHED hours and
        # name plus its manual state. Without it the card falls back to null
        # for every time field, because branch.opening_hours is no longer read
        # here: that column is the salon-build value and not what the manager
        # published.
        qs = with_published_card_fields(discoverable_salons())
        params = self._parsed()

        if params["latitude"] is not None:
            qs = with_distance(qs, params["latitude"], params["longitude"])
            # A salon whose branch has no pin cannot have a distance, and
            # showing it in a list sorted by distance would put it anywhere.
            qs = qs.filter(lat__isnull=False, lng__isnull=False)
            if params["radius_km"] is not None:
                qs = filter_by_radius(
                    qs, params["latitude"], params["longitude"], params["radius_km"]
                )

        if params["hijab_only"]:
            qs = qs.filter(hijab_certified=True)
        if params["is_top_rated"]:
            qs = filter_top_rated(qs)
        if params["rating_min"] is not None:
            qs = qs.filter(avg_rating__gte=params["rating_min"])
        if params["city"]:
            qs = qs.filter(branch_city__iexact=params["city"])

        # Both no-op when their parameter is absent, so no `if` here.
        qs = filter_by_category(qs, params["category"])
        qs = filter_by_search(qs, params["search"])

        return qs.order_by(*self._ordering(params))

    def _parsed(self):
        """
        The query string, read once per request and cached.

        get_queryset and filter_queryset both need it, and parsing twice would
        mean two chances to raise two different errors for one request.
        """
        cached = getattr(self, "_discovery_params", None)
        if cached is None:
            try:
                cached = parse_discovery(self.request.query_params)
            except ParamError as exc:
                # Lands in the project's error envelope as a field-level
                # error naming the parameter. See accounts.exceptions.
                raise ValidationError({exc.param: [exc.message]}) from exc
            self._discovery_params = cached
        return cached

    @staticmethod
    def _ordering(params):
        """
        The ORDER BY, and it ends in `id` on purpose.

        WITHOUT A UNIQUE TIEBREAKER, PAGINATION LIES. Postgres is free to
        return equally-ranked rows in any order it likes, and it does not have
        to pick the same order for page 1 and page 2. Two salons on 4.5 stars
        can therefore both appear on page 1, both vanish from page 2, or one
        of each — which reads as cards duplicating and disappearing while the
        customer scrolls, and is close to unreproducible once reported.

        nulls_last is the other half. `-avg_rating` alone emits plain DESC,
        and Postgres sorts NULLs FIRST on a descending column, so every salon
        that nobody has reviewed yet led the discovery list ahead of the
        five-star ones.
        """
        # The DEFAULT stays rating, even when a location was sent. Nearest
        # first is a plausible default for a discovery screen and it is not
        # this one's, so switching it here would change what every existing
        # client sees without anybody asking for it. `sort=distance` opts in.
        if params["sort"] == "distance":
            return ("distance_km", "id")
        return (
            F("avg_rating").desc(nulls_last=True),
            # Among salons on the same score, the one more people rated.
            "-review_count",
            "id",
        )

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        if not self._parsed()["is_open_now"]:
            return queryset

        # is_open_now cannot be a SQL filter: whether a salon is open depends on
        # its own timezone and on a JSONB grid, so it is computed in Python
        # and fed back as an id list. That evaluates the queryset once extra,
        # which is why it only happens when the flag is set — and why it runs
        # HERE, after category, search and radius have already cut the set
        # down in SQL, rather than over every public salon in the country.
        #
        # Note that ?page_size= does not bound this. Pagination slices what
        # comes back from this method, so the Python pass has already visited
        # every matching row whatever page the customer asked for.
        serializer = SalonCardSerializer()
        open_ids = [obj.pk for obj in queryset if serializer.get_open(obj)]
        return queryset.filter(pk__in=open_ids)


@extend_schema(
    parameters=[
        OpenApiParameter("total_amount", float, description="Booking total used to compute deposit.amount, e.g. 250"),
    ],
    responses=SalonCardSerializer,
)
class SalonDiscoveryDetailView(RetrieveAPIView):
    """Single salon card by id (map pin tap / card tap)."""

    serializer_class = SalonCardSerializer
    permission_classes = [AllowAny]
    lookup_field = "pk"

    def get_queryset(self):
        return with_published_card_fields(discoverable_salons())


class SalonProfileView(APIView):

    permission_classes = [AllowAny]

    def get(self, request, salon_id):
        salon = salon_profile(salon_id, user=request.user)
        if salon is None:
            raise Http404("Salon not found")

        snapshot = read_snapshot(salon)

        tz = timezones.resolve(salon.branch_timezone, salon.id)
        now = datetime.now(tz)

        hours = resolve_hours(
            weekly=snapshot["HOURS"].get("weekly"),
            # No storefront_status_exception table in this schema version, so
            # a dated exception cannot be read yet. Re-check against staging
            # before release.
            exception=None,
            state=salon.manual_state,
            weekday_index=now.weekday(),
            now_hhmm=now.strftime("%H:%M"),
        )

        data = SalonProfileSerializer(
            salon,
            context={
                "snapshot": snapshot,
                "hours": hours,
                "cancel_window_hours": snapshot["POLICY"].get("cancelWindowHours"),
            },
        ).data
        return Response(data)


class SalonServicesView(APIView):

    permission_classes = [AllowAny]

    def get(self, request, salon_id):
        salon = salon_profile(salon_id)
        if salon is None:
            raise Http404("Salon not found")

        categories = salon_categories(salon.tenant_id)
        services = salon_services(salon)

        # category_0_id, not category_id: the service table has a legacy text
        # column already named `category`, so inspectdb renamed the real
        # foreign key rather than colliding with it.
        grouped = {}
        for svc in services:
            grouped.setdefault(svc.category_0_id, []).append(svc)

        chips = {}
        groups = []

        for cat_id, svcs in grouped.items():
            cat = categories.get(cat_id)
            # A service with no category, or one pointing at a deleted row,
            # still has to appear: dropping it would silently hide a bookable
            # service from the menu.
            if cat is None:
                cat = {"id": None, "name_en": "Other", "icon": None, "parent_id": None}

            parent = categories.get(cat["parent_id"]) if cat["parent_id"] else None
            chip = parent or cat

            chips[chip["id"]] = {
                "id": str(chip["id"]) if chip["id"] else "other",
                "label": chip["name_en"],
                "icon": chip.get("icon"),
            }

            groups.append({
                "id": str(cat["id"]) if cat["id"] else "other",
                "category_id": str(chip["id"]) if chip["id"] else "other",
                "name": cat["name_en"],
                "services": [self._service(s) for s in svcs],
            })

        return Response({
            "service_categories": [{"id": "all", "label": "All"}] + list(chips.values()),
            "service_groups": groups,
        })

    @staticmethod
    def _service(svc) -> dict:
        # duration_min and duration_max are the SAME number. A real range needs
        # service_variant rows with differing durations; until the app reads
        # those, sending one value twice is honest and lets the app collapse
        # "20 - 20 mins" to "20 mins" itself.
        price_minor = getattr(svc, "branch_price_minor", None) or svc.price_minor
        return {
            "id": str(svc.id),
            "name": svc.name,
            "description": svc.description,
            "price": major(price_minor),
            "duration_min": svc.duration_minutes,
            "duration_max": svc.duration_minutes,
        }

def stylist_rows(salon, service_ids=None, stages=None):
    """
    Every stylist of one salon in the mobile app's shape.

    With `service_ids`, only the staff who can perform at least one of those
    services, each carrying the ones they cover. Without it, the whole roster
    and no `service_ids` key at all — the Stylists tab asks a question about
    people, not about a basket, and an empty list there would read as "this
    person can do nothing".
    """
    staff = list(salon_stylists(salon))

    coverage = {}
    if service_ids:
        coverage = stylist_service_coverage(
            salon, service_ids, [s.id for s in staff], stages=stages
        )
        # Someone who covers none of the picked services is left out.
        staff = [s for s in staff if s.id in coverage]

    rows = [
        _stylist_row(s, coverage[s.id] if service_ids else None) for s in staff
    ]

    # Rating first, then name, so the list does not reshuffle between
    # refreshes. Sorted here rather than in SQL because rating is not a column
    # yet (see _stylist_row) and NULLS LAST would have to be spelled out for it
    # the moment it becomes one.
    rows.sort(key=_stylist_order)
    return rows


def _stylist_row(s, covered=None):
    row = {
        "id": str(s.id),
        "tenant_id": str(s.tenant_id),
        "branch_id": str(s.branch_id) if s.branch_id else None,
        "name": " ".join(filter(None, [s.first_name, s.last_name])) or None,
        # Two different columns, not one string split in two. `title` is the
        # job title on the staff record (staff_profile.position — "Master
        # Barber"); `role` is the expertise line on the person's own account
        # (user_account.job_title — "Haircut & Styling Expert"). The app joins
        # them as "title · role", so neither is pre-joined here and neither
        # falls back to the other: printing the same words on both sides of the
        # dot is worse than leaving one side empty.
        "title": s.position or None,
        "role": s.job_title or None,
        "avatar_url": s.avatar_url,
        "rating": None,
        "review_count": None,
        "years_experience": None,
        "day_off": None,
    }

    # Only when the caller asked about services. `covered` is a list, possibly
    # of one; an empty one cannot reach here, because a stylist covering
    # nothing is not in the response at all.
    if covered is not None:
        row["service_ids"] = [str(service_id) for service_id in covered]
    return row


def _stylist_order(row):
    # Rating descending with nulls last, then name. Every rating is null today,
    # so this is name order in practice; it stops being so the day reviews
    # learn which stylist they belong to.
    rating = row.get("rating")
    return (0 if rating is not None else 1, -(rating or 0), row.get("name") or "")


def _invalid(exc):
    """A ParamError as the project's field-level 422, code and all."""
    return ValidationError({exc.param: [ErrorDetail(exc.message, code=exc.code)]})


def _service_ids_or_422(request):
    try:
        return parse_service_ids(request.query_params)
    except ParamError as exc:
        raise _invalid(exc) from exc


def _reject_unbookable(salon, service_ids):
    """
    422 for the two things a caller can get wrong about a service id.

    Nothing else here is an error. A service nobody is qualified for is a 200
    with an empty list (BOOKING_EXPERT_API.md §2): the customer picked a real
    service and the
    honest answer is that this salon has no one for it, which is a screen the
    app draws, not an error it apologises for.
    """
    offered = salon_service_ids(salon, service_ids)
    if any(sid not in offered for sid in service_ids):
        raise ValidationError({
            "service_ids": [
                ErrorDetail(
                    "One or more selected services are not offered by this salon.",
                    code="unknown_service",
                )
            ]
        })

    stages = service_stage_rows(service_ids)
    staged = {row["service_id"] for row in stages}
    if any(sid not in staged for sid in service_ids):
        # A service with no stages requires no skill, so EVERY stylist would
        # trivially qualify. That is a catalogue gap on the platform side, and
        # answering it with the whole roster would book the customer with
        # someone who cannot do the job.
        raise ValidationError({
            "service_ids": [
                ErrorDetail(
                    "One or more selected services are not bookable yet.",
                    code="service_without_skill",
                )
            ]
        })
    return stages


@extend_schema(
    parameters=[
        OpenApiParameter(
            "service_ids",
            str,
            required=False,
            description=(
                "Comma-separated service UUIDs. Filters the list to the staff "
                "who can perform at least one of them and adds `service_ids` "
                "to every stylist. Omitted or empty returns the full roster."
            ),
        ),
    ],
)
class SalonStylistsView(APIView):
    """GET /api/v1/salon/<uuid>/stylists[?service_ids=a,b]"""

    permission_classes = [AllowAny]

    def get(self, request, salon_id):
        salon = salon_profile(salon_id)
        if salon is None:
            raise Http404("Salon not found")

        service_ids = _service_ids_or_422(request)
        if not service_ids:
            return Response({"stylists": stylist_rows(salon)})

        stages = _reject_unbookable(salon, service_ids)
        return Response({
            "stylists": stylist_rows(salon, service_ids, stages=stages),
        })


@extend_schema(
    parameters=[
        OpenApiParameter("tenant_id", str, required=True, description="Tenant UUID"),
        OpenApiParameter("branch_id", str, required=False, description="Branch UUID"),
    ],
)
class StylistListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            params = parse_stylists(request.query_params)
        except ParamError as exc:
            raise ValidationError({exc.param: [exc.message]}) from exc

        salon = discoverable_salons().filter(
            tenant_id=params["tenant_id"],
        ).first()
        if salon is None:
            raise Http404("Salon not found")

        return Response({"stylists": stylist_rows(salon)})


        
@extend_schema(
    parameters=[
        OpenApiParameter("tenant_id", str, required=True),
        OpenApiParameter("branch_id", str, required=False),
        OpenApiParameter("category_id", str, required=False),
    ],
)
class ServiceListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            params = parse_services(request.query_params)
        except ParamError as exc:
            raise ValidationError({exc.param: [exc.message]}) from exc

        salon = discoverable_salons().filter(
            tenant_id=params["tenant_id"],
        ).first()
        if salon is None:
            raise Http404("Salon not found")

        data = SalonServicesView().get(request, salon.id).data

        if params["category_id"]:
            wanted = str(params["category_id"])
            data["service_groups"] = [
                g for g in data["service_groups"]
                if wanted in (g["id"], g["category_id"])
            ]

        return Response(data)


@extend_schema(
    summary="Resolve service ids into their details",
    description=(
        "Resolve a set of service ids into full details, in one call — what "
        "the app needs to describe a basket, a saved booking, or a deep link "
        "it holds ids for.\n\n"
        "The array comes back in the order the ids were sent, duplicates "
        "collapse, and an id that resolves to nothing is LEFT OUT rather "
        "than erroring — so the array can be shorter than the request, or "
        "empty. The caller compares lengths to notice.\n\n"
        "Retired services still resolve, carrying `is_active: false`, so an "
        "old booking can still be described instead of showing a gap.\n\n"
        "Prices are current, not quoted: this is a lookup, and booking-api "
        "recomputes every figure itself (BOOKING_CREATE_API.md). Ids may "
        "span salons; each entry names its own `salon_id`. No pagination — "
        "the caller asks for a known, small set."
    ),
    parameters=[
        OpenApiParameter(
            "service_ids",
            str,
            required=True,
            description=(
                "Service UUIDs, comma separated. The repeated "
                "(`service_ids=a&service_ids=b`) and bracket "
                f"(`service_ids[]=a`) spellings are accepted too. Up to "
                f"{MAX_SERVICE_IDS} ids."
            ),
        ),
    ],
    responses={
        200: ServiceDetailSerializer(many=True),
        422: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description=(
                "`service_ids` missing or empty (`missing_filter`), or not a "
                "list of UUIDs (`invalid`). Nothing else here is an error: "
                "ids that resolve to nothing are a 200 with a shorter array."
            ),
            examples=[
                OpenApiExample(
                    "Missing filter",
                    value={
                        "detail": "Please correct the highlighted fields.",
                        "code": "validation_error",
                        "errors": [
                            {
                                "field": "service_ids",
                                "code": "missing_filter",
                                "message": "Provide at least one service id.",
                            }
                        ],
                    },
                )
            ],
        ),
    },
)
class ServiceDetailsView(APIView):
    """
    GET /api/v1/services-details?service_ids=<uuid>,<uuid>

    The one endpoint here that is keyed by service rather than by salon. The
    app reaches it holding ids it stored earlier — a basket it kept across a
    restart, a booking it is rendering, a link someone shared — and needs the
    names, prices and pictures back for them, possibly across two salons.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        service_ids = _service_ids_or_422(request)
        if not service_ids:
            # REQUIRED here, unlike the same parameter on the stylists
            # endpoint, where absent means "the whole roster". There is no
            # whole roster to fall back on: a lookup with nothing to look up
            # is a caller bug, and answering [] would hide it.
            raise _invalid(
                ParamError(
                    "service_ids",
                    "Provide at least one service id.",
                    code="missing_filter",
                )
            )

        rows = {svc.id: svc for svc in services_by_ids(service_ids)}
        categories = categories_for_tenants({svc.tenant_id for svc in rows.values()})

        # The REQUESTED order, not the database's: the app renders a basket in
        # the order the customer built it. `parse_service_ids` already
        # collapsed duplicates and kept that order; anything that did not
        # resolve simply is not here.
        found = [rows[sid] for sid in service_ids if sid in rows]

        return Response(
            ServiceDetailSerializer(
                found, many=True, context={"categories": categories}
            ).data
        )


class SalonPackagesView(APIView):
    """
    GET /api/v1/salon/<uuid>/packages

    my_packages is ALWAYS empty, and not because the user is logged out. The
    platform can SELL a package (sale_line.package_id records it) but nothing
    anywhere tracks sessions used against sessions bought: there is no
    redemption table. Returning the key with an empty list lets the app ship
    the tab now; filling it needs schema work on the platform side.
    """

    permission_classes = [AllowAny]

    def get(self, request, salon_id):
        salon = salon_profile(salon_id)
        if salon is None:
            raise Http404("Salon not found")

        bundles = []
        for pkg in salon_packages(salon):
            sum_minor = pkg.sum_minor or 0
            save_minor = max(sum_minor - pkg.price_minor, 0)

            bundles.append({
                "id": str(pkg.id),
                "name": pkg.name,
                "description": pkg.description,
                # highlights is text[] in Postgres but TextField in the stale
                # model, so guard the type rather than trusting either.
                "features": list(pkg.highlights) if isinstance(pkg.highlights, (list, tuple)) else [],
                "duration_minutes": pkg.total_minutes,
                "price": major(pkg.price_minor),
                # Only shown when the bundle actually saves something. A
                # package priced at or above its parts is not a deal, and
                # printing "save 0" on the card would look like a bug.
                "price_before": major(sum_minor) if save_minor else None,
                "save_amount": major(save_minor) if save_minor else None,
                "theme": pkg.color_theme,
            })

        return Response({"my_packages": [], "bundles": bundles})


class SalonProductsView(APIView):
    """
    GET /api/v1/salon/<uuid>/products

    The shop tab. Retail products only, priced from each product's default
    variant. The list is tenant-wide rather than branch-specific because
    product has no branch column; see the selector for why that is a schema
    fact and not a shortcut.
    """

    permission_classes = [AllowAny]

    def get(self, request, salon_id):
        salon = salon_profile(salon_id)
        if salon is None:
            raise Http404("Salon not found")

        products = [
            {
                "id": str(p.id),
                "name": p.name,
                "price": major(p.price_minor),
                "image_url": p.image_url,
            }
            for p in salon_products(salon)
        ]

        return Response({"products": products})


class SalonStoriesView(APIView):
    """
    GET /api/v1/salon/<uuid>/stories

    What the story ring opens. Public, because the card already tells every
    caller whether a salon has one.

    Lifecycle only: not deleted, not expired — the SAME rule as the has_story
    flag on the card. If the two ever diverge, a ring appears over an empty
    viewer, which is the one failure mode worth designing against here.

    Deliberately NOT filtered on media moderation. Nothing on the platform
    sets APPROVED yet, so the check would empty every ring on the app. That
    gap is a platform ticket and the fix belongs there, not as a second
    opinion in this file.

    A story whose media row is missing is skipped rather than sent with a
    null url: the app would otherwise render a blank frame the customer
    cannot dismiss.
    """

    permission_classes = [AllowAny]

    def get(self, request, salon_id):
        salon = salon_profile(salon_id)
        if salon is None:
            raise Http404("Salon not found")

        # The PUBLISHED name, read the same way the profile screen reads it.
        # published_name is not available here: that annotation comes from
        # with_published_card_fields, which salon_profile() does not apply, so
        # reading it would silently fall through to the branch name and this
        # screen would call the salon something the card never calls it.
        snapshot = read_snapshot(salon)

        # The two timestamps are stored WITHOUT a zone by the platform, and
        # they are UTC: checked against now() on the server, a story created
        # minutes earlier reads minutes earlier. Labelling them here is what
        # stops the app reading them in the phone's own zone and showing a
        # story as expired hours early.
        stories = [
            {
                "id": str(s.id),
                "media_url": s.media_url,
                "caption": s.caption_en,
                "link_url": s.link_url,
                "publish_time": s.created_at.replace(tzinfo=dt_timezone.utc).isoformat(),
                "expires_at": s.expires_at.replace(tzinfo=dt_timezone.utc).isoformat(),
            }
            for s in salon_stories(salon.id)
            if s.media_url
        ]

        return Response({
            "name": snap_field(snapshot, "IDENTITY", "nameEn") or salon.branch_name,
            "logo_url": salon.logo_url,
            "stories": stories,
        })


class DiscoverMapView(APIView):
    """Map viewport endpoint, returns lightweight venue markers.

    ``GET /api/v1/discover/map?latitude=…&longitude=…&latitudeDelta=…&longitudeDelta=…[&category=…&radius=…]``

    All parameters are optional. Bounding-box filtering replaces pagination.
    A hard ``LIMIT`` inside the selector acts as a safety valve.

    NOTE ON UNITS: ``radius`` is in METRES on this endpoint, because the map
    sends a viewport size in metres. ``/discover`` takes kilometres. The
    conversion happens once, inside ``parse_map``, so everything below this
    view speaks kilometres only.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        parameters=[
            OpenApiParameter("latitude", float, required=False,
                             description="Center latitude"),
            OpenApiParameter("longitude", float, required=False,
                             description="Center longitude"),
            OpenApiParameter("latitudeDelta", float, required=False,
                             description="Latitude delta span"),
            OpenApiParameter("longitudeDelta", float, required=False,
                             description="Longitude delta span"),
            OpenApiParameter("radius", float, required=False,
                             description="Search radius in METRES (100 to 50000)"),
            OpenApiParameter("limit", int, required=False,
                             description="Max venues to return, capped by the selector"),
            OpenApiParameter("zoom", int, required=False,
                             description="Current map zoom level"),
            OpenApiParameter("category", str, required=False,
                             description="Venue category filter",
                             enum=["all", "gents", "ladies", "unisex"]),
        ],
        responses={200: MapVenueSerializer(many=True)},
    )
    def get(self, request):
        try:
            params = parse_map(request.query_params)
        except ParamError as exc:
            # Raised, not returned. The project's exception handler turns this
            # into the same field-level envelope /discover produces, so one
            # client-side error reader works for both endpoints.
            raise ValidationError({exc.param: [exc.message]}) from exc

        venues_qs = map_venues(
            latitude=params["latitude"],
            longitude=params["longitude"],
            latitude_delta=params["latitude_delta"],
            longitude_delta=params["longitude_delta"],
            category=params["category"],
            radius_km=params["radius_km"],
            limit=params["limit"],
        )

        serializer = MapVenueSerializer(venues_qs, many=True)
        venues = serializer.data

        return Response({
            "mode": "markers",
            "count": len(venues),
            "venues": venues,
        })


class DiscoverStoryListView(ListAPIView):
    """
    GET /api/v1/discover/story

    The story rail: salons with a live story right now, paginated.

    Filters on the SAME has_story flag the card carries, so the rail and the
    ring can never disagree about which salons have something to show.
    """

    serializer_class = StorySalonSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        return (
            with_published_card_fields(discoverable_salons())
            .filter(has_story=True)
            .order_by("id")
        )




@extend_schema_view(
    get=extend_schema(
        parameters=[
            OpenApiParameter("category", str, description="Salon category. 'all' means no filter.", enum=["all", "gents", "ladies", "unisex"]),
            OpenApiParameter("is_top_rated", bool, description="Only well-reviewed salons."),
            OpenApiParameter("is_open_now", bool, description="Only salons open right now, in their own timezone."),
            OpenApiParameter("search", str, description="Free text over the salon name and city."),
            OpenApiParameter("page", int, description="Page number."),
            OpenApiParameter("page_size", int, description="Cards per page, up to 50. Defaults to 15."),
        ],
        responses=FavouriteSerializer(many=True),
    ),
    post=extend_schema(
        parameters=[],
        request={
            "application/json": {
                "type": "object",
                "properties": {"salon_id": {"type": "string", "format": "uuid"}},
                "required": ["salon_id"],
            }
        },
        responses={200: None},
    ),
)
class FavouriteListView(ListAPIView):
    """
    GET  /api/v1/favourite  the customer's saved salons, paginated
    POST /api/v1/favourite  the heart, a toggle, body {"salon_id": "..."}

    NOT inheriting SalonDiscoveryListView, although it started that way and
    the filtering below is close to a copy of it. Inheriting also inherits
    that view's CLASS-LEVEL @extend_schema, which cannot be overridden per
    method: the POST, which reads nothing but a request body, was documented
    as taking a dozen query filters, and a frontend reading the docs sent the
    wrong thing. Repeating four filter calls is the cheaper mistake.

    Same URL for both methods because it is one resource: GET reads your
    favourites, POST changes them.
    """

    serializer_class = FavouriteSerializer
    permission_classes = [IsAuthenticated]

    def _parsed(self):
        """The query string, read once per request and cached."""
        cached = getattr(self, "_favourite_params", None)
        if cached is None:
            try:
                cached = parse_discovery(self.request.query_params)
            except ParamError as exc:
                raise ValidationError({exc.param: [exc.message]}) from exc
            self._favourite_params = cached
        return cached

    def get_queryset(self):
        # The saved rows first, keyed by salon id: the serializer needs the
        # favourite's own id beside the salon, and holding them here avoids a
        # second query per card to find it again.
        saved = Favourite.objects.filter(account=self.request.user)
        self._favourites = {str(f.storefront_id): f for f in saved}

        qs = with_published_card_fields(discoverable_salons()).filter(
            id__in=self._favourites.keys()
        )

        params = self._parsed()
        if params["is_top_rated"]:
            qs = filter_top_rated(qs)

        # Both no-op when their parameter is absent.
        qs = filter_by_category(qs, params["category"])
        qs = filter_by_search(qs, params["search"])

        # Ordered by id, not by when it was saved. Newest-first would be
        # nicer, but the ordering has to be on the SALON queryset and the
        # save time lives on the favourite row. A unique tiebreaker is the
        # part that actually matters: without one, pagination can repeat a
        # card between pages.
        return qs.order_by("id")

    def filter_queryset(self, queryset):
        if not self._parsed()["is_open_now"]:
            return queryset

        # Same reasoning as the discovery list: open/closed depends on the
        # salon's own timezone and a JSONB grid, so it cannot be SQL. Runs
        # last, over the already-narrowed favourites, which is a small set by
        # definition here.
        card = SalonCardSerializer()
        open_ids = [obj.pk for obj in queryset if card.get_open(obj)]
        return queryset.filter(pk__in=open_ids)

    def get_serializer(self, *args, **kwargs):
        # The queryset yields SALONS; the contract wants {id, salon}. Wrapping
        # happens here rather than in the queryset so every filter above still
        # applies to the salon rows themselves.
        if args and hasattr(args[0], "__iter__"):
            wrapped = []
            for salon in args[0]:
                favourite = self._favourites[str(salon.id)]
                favourite.salon = salon
                wrapped.append(favourite)
            args = (wrapped,) + args[1:]
        return super().get_serializer(*args, **kwargs)

    def post(self, request, *args, **kwargs):
        """
        Tap the heart. Already saved means remove, otherwise add.

        A TOGGLE rather than separate add and delete endpoints, because that
        is what the heart button is. The response says which way it went, so
        the app sets the icon from the answer instead of guessing.

        The salon is not checked for existence: that is one extra query on
        every tap to catch an id the app got from this same API.
        """
        salon_id = request.data.get("salon_id")
        if not salon_id:
            raise ValidationError({"salon_id": ["This field is required."]})

        deleted, _ = Favourite.objects.filter(
            account=request.user,
            storefront_id=salon_id,
        ).delete()

        if deleted:
            return Response({"is_favorite": False})

        Favourite.objects.create(account=request.user, storefront_id=salon_id)
        return Response({"is_favorite": True})


@extend_schema(
    parameters=[
        OpenApiParameter(
            "from", str, required=True,
            description="Window start, inclusive. ISO 8601 with an offset — "
                        "encode the + as %2B, e.g. 2026-09-19T10:00:00%2B04:00.",
        ),
        OpenApiParameter(
            "to", str, required=True,
            description="Window end, exclusive. Same salon-local day as `from`.",
        ),
        OpenApiParameter(
            "service_ids", str, required=False,
            description="Comma-separated service UUIDs. Required unless "
                        "`stylist_id` is sent; both together narrow to that "
                        "stylist for those services.",
        ),
        OpenApiParameter(
            "stylist_id", str, required=False,
            description="One stylist. Required unless `service_ids` is sent.",
        ),
    ],
)
class NearestAvailableView(APIView):
    """GET /api/v1/booking/nearest-available/<uuid> — bookable starts in a window."""

    permission_classes = [IsAuthenticated]

    def get(self, request, salon_id):
        salon = salon_profile(salon_id)
        if salon is None:
            raise Http404("Salon not found")

        tz = timezones.resolve(salon.branch_timezone, salon.id)
        try:
            params = parse_nearest_available(request.query_params, tz)
        except ParamError as exc:
            raise _invalid(exc) from exc

        timing = service_timing_rows(salon, params["service_ids"])
        duration = _appointment_minutes(params["service_ids"], timing)

        return Response({
            "duration_min": duration,
            "offers": _offers(
                salon, tz, params, duration,
                lead_minutes=max([row["lead_time_minutes"] or 0 for row in timing] or [0]),
                now=datetime.now(tz),
            ),
        })


def _appointment_minutes(service_ids, timing):
    """
    How long the whole visit takes, padding included.

    Per service the platform's own figure wins: the sum across its stages of
    pre-buffer, work, impact and post-buffer, which is what has to fit in the
    diary. A service with no stages falls back to the catalogue duration the
    services tab already shows the customer, so the number is never zero for a
    service that exists.

    With no services named there is nothing to measure, and the answer is the
    browsing default — reported back as `duration_min` so the app never has to
    guess which number drew the grid.
    """
    if not service_ids:
        return slots.DEFAULT_APPOINTMENT_MINUTES

    return sum(
        row["stage_minutes"] or row["duration_minutes"] or 0 for row in timing
    )


def _qualified_stylists(salon, service_ids, stylist_id):
    """
    Who may take this whole visit.

    ONE stylist for the whole visit, so "qualified" is stricter here than on
    the Expert step: covering some of the basket is covering none of it. A
    split visit would be a different response shape entirely, not a flag on
    this one.

    Anything that simply matches nobody — an unknown stylist, a stylist who
    does not work here, an id from another salon's menu — comes back as an
    empty list, which the caller turns into `"offers": []` rather than an
    error.
    """
    staff = list(salon_stylists(salon))
    if stylist_id is not None:
        staff = [s for s in staff if s.id == stylist_id]

    if not staff or not service_ids:
        return staff

    coverage = stylist_service_coverage(salon, service_ids, [s.id for s in staff])
    # Deduped ids, so "covers all of them" is a length check.
    return [s for s in staff if len(coverage.get(s.id, ())) == len(service_ids)]


def _open_span(salon, tz, day, day_start):
    """
    The hours the salon itself is open on that date, or None if it is shut.

    A stylist rostered on a day the salon is closed is not bookable, so this
    bounds every shift below. Two ways to be shut: the published weekly grid
    says so, or someone shut the day by hand in the platform.

    Dated opening-hours exceptions are still not readable (see
    SalonProfileView), so a salon open on its normal hours over a public
    holiday will offer starts it should not. That gap is the profile's, not
    this endpoint's, and it closes in one place when the table arrives.
    """
    if manual_state_on(salon.id, day) == "CLOSED":
        return None

    row = hours_row(read_snapshot(salon)["HOURS"].get("weekly"), day.weekday())
    if not row or row.get("closed"):
        return None

    return slots.span(day_start, row.get("open"), row.get("close"))


def _offers(salon, tz, params, duration, lead_minutes, now):
    day = params["start"].astimezone(tz).date()
    day_start = datetime.combine(day, time.min, tzinfo=tz)

    open_span = _open_span(salon, tz, day, day_start)
    if open_span is None:
        return []

    staff = _qualified_stylists(salon, params["service_ids"], params["stylist_id"])
    if not staff:
        return []

    staff_ids = [s.id for s in staff]
    shifts = {
        row["staff_member_id"]: row
        for row in shift_rows(salon.tenant_id, salon.branch_id, staff_ids, day)
    }

    # One booking query for both jobs: the blocks that stop a start, and the
    # count of what each stylist already holds that day. The span runs to the
    # day after next so an overnight shift's tail is included, since a salon
    # open past midnight books past midnight too.
    booked = {}
    day_end = day_start + timedelta(days=2)
    for row in booking_rows(salon.tenant_id, staff_ids, day_start, day_end):
        booked.setdefault(row["staff_id"], []).append(
            (row["start_at"].astimezone(tz), row["end_at"].astimezone(tz))
        )

    earliest = now + timedelta(minutes=lead_minutes)

    offers = []
    for member in staff:
        shift = shifts.get(member.id)
        if shift is None:
            continue  # not rostered here that day

        working = slots.span(day_start, shift["start_time"], shift["end_time"])
        if working is None:
            continue

        # The salon's hours bound the stylist's: a shift starting before the
        # doors open is not bookable time.
        on_duty = (max(working[0], open_span[0]), min(working[1], open_span[1]))
        if on_duty[0] >= on_duty[1]:
            continue

        blocks = list(booked.get(member.id, ()))
        unpaid_break = slots.break_span(day_start, shift["break_time"])
        if unpaid_break is not None:
            blocks.append(unpaid_break)

        free = slots.subtract([on_duty], blocks)
        bookings_today = sum(
            1 for start, _ in booked.get(member.id, ()) if start.date() == day
        )

        for start in slots.starts(
            free, duration, params["start"], params["end"], earliest, day_start
        ):
            offers.append({
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=duration)).isoformat(),
                "bookings_today": bookings_today,
                "stylist": {
                    "id": str(member.id),
                    "name": " ".join(
                        filter(None, [member.first_name, member.last_name])
                    ) or None,
                    # The same `role` the stylists endpoint sends, from the same
                    # column, so one stylist reads the same on every screen.
                    "role": member.job_title or None,
                    "avatar_url": member.avatar_url,
                },
                # Sort keys, dropped before the response goes out. Sorting on
                # the ISO strings would work for `start` and not for the rest.
                "_order": (start, bookings_today,
                           " ".join(filter(None, [member.first_name, member.last_name]))),
            })

    # The soonest start first; then whoever is least busy that day, which
    # spreads the work rather than filling one diary; then by name so two
    # equally free stylists do not swap places between refreshes.
    offers.sort(key=lambda offer: offer["_order"])
    for offer in offers:
        del offer["_order"]
    return offers


def _tenant_for_booking(request):
    """
    The `X-Tenant-Id` to forward: the caller's own, else the salon's.

    THE CALLER'S WINS, verbatim and unexamined. An app that knows its tenant
    is the better source, and second-guessing it here would make this service
    the thing that decides which salon's books a booking lands in.

    Only when the app sent none is one derived, and DERIVED IS NOT INVENTED:
    it is the tenant that the storefront named in the payload actually belongs
    to, read from the same table `/salon/<id>` serves. booking-api cannot do
    this for itself — its TenantMiddleware runs before the guard and reads the
    header or nothing (tenant.middleware.ts) — and with no tenant it resolves
    no platform services and refuses the booking with `unknown_service`.

    Anything unreadable comes back as None and NO header is sent, which leaves
    the refusal exactly where it was. The body is read here, never rewritten:
    what crosses the network is still `request.body`, byte for byte, because
    booking-api hashes those bytes to recognise a retry.
    """
    sent = request.META.get("HTTP_X_TENANT_ID")
    if sent:
        return sent

    try:
        payload = json.loads(request.body or b"")
    except ValueError:
        # A malformed body is booking-api's 422 to give, not ours to pre-empt.
        return None

    if not isinstance(payload, dict):
        return None

    tenant_id = tenant_for_salon(payload.get("salon_id"))
    if tenant_id is None:
        logger.warning(
            "No X-Tenant-Id sent and salon_id %r resolved to no tenant; "
            "forwarding the booking without one, which booking-api will "
            "refuse with unknown_service.",
            payload.get("salon_id"),
        )
        return None

    return str(tenant_id)


class BookingApiDown(APIException):
    """503 when gostyle-booking-api cannot be reached.

    Deliberately NOT how a 409 or a 422 from that service arrives: those are
    answers, and they are forwarded with their own status and body. This is
    the network failing, which is the one case the customer app cannot act on.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Booking is temporarily unavailable. Please try again."
    default_code = "booking_api_unavailable"


# The schema below is written by hand as raw OpenAPI, and there is no
# serializer behind it ON PURPOSE. booking-api owns this payload; a serializer
# here would validate it a second time, and two copies of one contract drift.
# It would also have to re-serialise the body to produce `request.data`, and
# the bytes must cross untouched — booking-api hashes them to recognise a
# retry (see booking_api.create_booking). So this documents the body without
# standing in its way. It is a description, not a gate: what actually refuses
# a bad payload is booking-api's 422, and only that.
_MONEY = {
    "type": "number",
    "format": "double",
    "minimum": 0,
    "description": "Decimal AED, at most two places. Verified upstream, never trusted.",
}

_LINE = {
    "type": "object",
    "required": ["id", "amount"],
    "properties": {
        "id": {"type": "string", "example": "svc_fade"},
        "amount": {**_MONEY, "example": 120},
    },
}

_BOOKING_REQUEST = {
    "type": "object",
    "required": [
        "salon_id",
        "services",
        "stylists",
        "date",
        "start_time",
        "end_time",
        "amount_without_tax",
        "tax_amount",
        "discount",
        "total",
        "advance_paid_amount",
        "due_amount",
        "payment_status",
        "status",
        "booking_type",
    ],
    "properties": {
        "salon_id": {"type": "string", "example": "marina-walk"},
        "services": {
            "type": "array",
            "minItems": 1,
            "items": _LINE,
            "description": "At least one. An empty basket is refused upstream.",
        },
        "products": {
            "type": "array",
            "items": _LINE,
            "description": (
                "Refused with 422 products_not_supported when non-empty: there "
                "is no product catalogue to price a line against, and a product "
                "silently dropped from a basket is money the salon does not take."
            ),
        },
        "stylists": {
            "type": "array",
            "items": {"type": "string"},
            "example": ["maya"],
            "description": (
                "One for the whole visit, or one per service in the same order. "
                "An empty array is refused: the staff directory publishes no "
                "skills, so \"the salon picks a qualified one\" cannot be done "
                "honestly."
            ),
        },
        "date": {
            "type": "string",
            "format": "date",
            "example": "2026-09-20",
            "description": "Branch-local trading day. Must agree with start_time.",
        },
        "start_time": {
            "type": "string",
            "format": "date-time",
            "example": "2026-09-20T20:00:00+04:00",
        },
        "end_time": {
            "type": "string",
            "format": "date-time",
            "example": "2026-09-20T20:45:00+04:00",
        },
        "amount_without_tax": {**_MONEY, "example": 225},
        "tax_amount": {**_MONEY, "example": 11.25},
        "discount": {**_MONEY, "example": 20},
        "promo_code": {
            "type": "string",
            "nullable": True,
            "example": "GOSTYLE20",
        },
        "total": {**_MONEY, "example": 216.25},
        "advance_paid_amount": {**_MONEY, "example": 0, "description": "Always 0 on create."},
        "due_amount": {**_MONEY, "example": 216.25},
        "payment_status": {
            "type": "string",
            "enum": ["DRAFT"],
            "description": "Only DRAFT on create.",
        },
        "status": {
            "type": "string",
            "enum": ["BOOKED"],
            "description": "Only BOOKED on create.",
        },
        "booking_type": {
            "type": "string",
            "enum": ["SINGLE", "ROUTINE"],
            "description": (
                "ROUTINE is refused with 422 routine_not_supported: this payload "
                "carries no recurrence rule, and a recurring booking that creates "
                "one visit is a customer expecting twelve."
            ),
        },
    },
    "description": (
        "Who is booking is NOT in here. booking-api resolves the customer from "
        "the bearer token, and this service neither reads nor overrides it."
    ),
}


@extend_schema(
    summary="Create a booking",
    description=(
        "Create a booking. The body is forwarded to gostyle-booking-api "
        "unchanged — raw bytes, never parsed and re-serialised, because that "
        "service hashes the body to recognise a retry — and its answer is "
        "returned unchanged, including 409 slot_taken and 422 validation "
        "errors, which carry that service's error shape rather than this "
        "one's. Send `Idempotency-Key` to make a retry safe.\n\n"
        "The body below is booking-api's contract, documented here but not "
        "validated here: nothing in this service checks it, and its 422 is "
        "the only answer about the payload. See docs/BOOKING_CREATE_API.md."
    ),
    parameters=[
        OpenApiParameter(
            name="Idempotency-Key",
            type=str,
            location=OpenApiParameter.HEADER,
            required=False,
            description=(
                "A UUID per booking attempt, reused for that attempt's retries. "
                "booking-api stores it with a hash of the body and returns the "
                "original response to a repeat, so a customer who taps twice "
                "gets one booking. Forwarded only when sent — never invented "
                "here, which would make every retry a second booking."
            ),
        ),
        OpenApiParameter(
            name="X-Tenant-Id",
            type=str,
            location=OpenApiParameter.HEADER,
            required=False,
            description=(
                "The tenant whose books the booking lands in. Optional: when "
                "it is not sent, the tenant that owns the payload's "
                "`salon_id` is looked up and sent instead, because "
                "booking-api reads this header and nothing else to resolve "
                "the services. A header the caller does send is forwarded "
                "verbatim and the salon is not consulted. Nothing is guessed "
                "— a salon that cannot be resolved sends no tenant at all."
            ),
        ),
    ],
    request={"application/json": _BOOKING_REQUEST},
    responses={
        201: OpenApiResponse(
            response={
                "type": "object",
                "description": "booking-api's created booking, forwarded verbatim.",
            },
            description="Created, at payment_status DRAFT. booking-api's body, untouched.",
            examples=[
                OpenApiExample(
                    "Created",
                    value={
                        "id": "bkg_01J8Z",
                        "salon_id": "marina-walk",
                        "status": "BOOKED",
                        "status_detail": "PENDING_PAYMENT",
                        "date": "2026-09-20",
                        "start_time": "2026-09-20T20:00:00+04:00",
                        "end_time": "2026-09-20T20:45:00+04:00",
                        "services": [
                            {"id": "svc_fade", "name": "Skin fade", "amount": 120}
                        ],
                        "products": [],
                        "stylists": [
                            {"id": "maya", "name": "Maya", "avatar_url": None}
                        ],
                        "amount_without_tax": 225,
                        "tax_amount": 11.25,
                        "discount": 20,
                        "total": 216.25,
                        "promo_code": "GOSTYLE20",
                        "advance_paid_amount": 0,
                        "due_amount": 216.25,
                        "payment_status": "DRAFT",
                        "payment_status_detail": "UNPAID",
                        "payment_method": None,
                        "pass_qr_code": "GS-BKG-1",
                        "expires_at": "2026-09-18T18:15:00+04:00",
                        "created_at": "2026-09-18T18:00:00+04:00",
                    },
                )
            ],
        ),
        401: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description="No bearer token. Refused here, before the network.",
            examples=[
                OpenApiExample(
                    "Not authenticated",
                    value={
                        "detail": "Authentication credentials were not provided.",
                        "code": "not_authenticated",
                        "errors": [
                            {
                                "field": None,
                                "code": "not_authenticated",
                                "message": "Authentication credentials were not provided.",
                            }
                        ],
                    },
                )
            ],
        ),
        409: OpenApiResponse(
            response={"type": "object"},
            description=(
                "booking-api's own shape. The slot went to someone else between "
                "picking it and pressing Book — a race, not a mistake, so the "
                "app sends the customer back to the picker."
            ),
            examples=[
                OpenApiExample(
                    "Slot taken",
                    value={"code": "slot_taken", "message": "That time just went."},
                )
            ],
        ),
        415: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description=(
                "Not `Content-Type: application/json`. Refused before dialling "
                "out, so the app hears about the header rather than getting "
                "booking-api's answer about the payload after a round trip."
            ),
            examples=[
                OpenApiExample(
                    "Unsupported media type",
                    value={
                        "detail": 'Unsupported media type "application/x-www-form-urlencoded" in request.',
                        "code": "unsupported_media_type",
                        "errors": [
                            {
                                "field": None,
                                "code": "unsupported_media_type",
                                "message": 'Unsupported media type "application/x-www-form-urlencoded" in request.',
                            }
                        ],
                    },
                )
            ],
        ),
        422: OpenApiResponse(
            response={"type": "object"},
            description=(
                "booking-api's own shape, NOT this project's envelope. It owns "
                "payload validation, including the money check: a figure that "
                "disagrees with the server's quote comes back with the correct "
                "one so the app can show the customer what changed."
            ),
            examples=[
                OpenApiExample(
                    "Validation error",
                    value={
                        "detail": "The booking could not be created.",
                        "code": "validation_error",
                        "errors": [
                            {
                                "field": "total",
                                "code": "amount_mismatch",
                                "message": "The price changed.",
                                "expected": 226.25,
                            }
                        ],
                    },
                )
            ],
        ),
        503: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description=(
                "booking-api was unreachable, timed out, or answered with "
                "non-JSON. `booking_api_unavailable` is the one to branch on: "
                "the request never arrived, so nothing was created and a retry "
                "is safe. A 5xx that came FROM booking-api is forwarded as "
                "itself and carries no such promise."
            ),
            examples=[
                OpenApiExample(
                    "Booking service unavailable",
                    value={
                        "detail": "Booking is temporarily unavailable. Please try again.",
                        "code": "service_unavailable",
                        "errors": [
                            {
                                "field": None,
                                "code": "booking_api_unavailable",
                                "message": "Booking is temporarily unavailable. Please try again.",
                            }
                        ],
                    },
                )
            ],
        ),
    },
)
class BookingCreateView(APIView):
    """POST /api/v1/booking — forwards to gostyle-booking-api."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        # Guard the content type before dialling out: booking-api would
        # answer 422 for a form post, after a round trip, with a message
        # about the payload rather than about the header.
        media_type = (request.content_type or "").split(";")[0].strip().lower()
        if media_type != "application/json":
            raise UnsupportedMediaType(media_type or "none")

        try:
            upstream_status, body = create_booking(
                # request.body, NOT request.data: the bytes go across exactly
                # as they arrived. See booking_api.create_booking.
                request.body,
                authorization=request.META.get("HTTP_AUTHORIZATION", ""),
                # Never invented: forwarded only if the caller sent one,
                # because a made-up key would turn a retry into a second
                # booking.
                idempotency_key=request.META.get("HTTP_IDEMPOTENCY_KEY"),
                # The caller's own, or the tenant the payload's salon belongs
                # to. Never a guess — see _tenant_for_booking.
                tenant_id=_tenant_for_booking(request),
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc

        # Returned, not raised, so api_exception_handler never sees it and the
        # body reaches the app in booking-api's own shape.
        return Response(body, status=upstream_status)


class BookingDetailView(APIView):
    """GET and PATCH one booking. Forwards to booking-api unchanged."""

    permission_classes = [IsAuthenticated]

    def get(self, request, booking_id):
        try:
            upstream_status, body = read_booking(
                booking_id,
                authorization=request.META.get("HTTP_AUTHORIZATION", ""),
                tenant_id=request.META.get("HTTP_X_TENANT_ID"),
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        return Response(body, status=upstream_status)

    def patch(self, request, booking_id):
        try:
            upstream_status, body = patch_booking(
                booking_id,
                request.body,
                authorization=request.META.get("HTTP_AUTHORIZATION", ""),
                idempotency_key=request.META.get("HTTP_IDEMPOTENCY_KEY"),
                tenant_id=request.META.get("HTTP_X_TENANT_ID"),
            )
        except BookingApiUnavailable as exc:
            raise BookingApiDown() from exc
        return Response(body, status=upstream_status)
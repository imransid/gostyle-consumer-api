import zoneinfo
from datetime import datetime

from django.http import Http404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .hours import resolve as resolve_hours
from .money import major
from .selectors import (
    discoverable_salons,
    filter_by_category,
    map_venues,
    salon_categories,
    salon_packages,
    salon_products,
    salon_profile,
    salon_services,
    salon_stylists,
    with_distance,
    with_published_hours,
)
from .serializers import (
    MapVenueSerializer,
    SalonCardSerializer,
    SalonProfileSerializer,
)
from .snapshot import read_snapshot


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
        OpenApiParameter("lat", float, description="User latitude, e.g. 25.19"),
        OpenApiParameter("lng", float, description="User longitude, e.g. 55.26"),
        OpenApiParameter("sort", str, description="Sort order", enum=["distance", "rating"]),
        OpenApiParameter("rating_min", float, description="Minimum average rating, e.g. 4.5"),
        OpenApiParameter("city", str, description="Filter by city name, e.g. Dubai"),
        OpenApiParameter("category", str, description="Filter by salon category", enum=["gents", "ladies", "unisex"]),
        OpenApiParameter("hijab_mode", str, description="1 to show only hijab-certified salons", enum=["1"]),
        OpenApiParameter("open_now", str, description="1 to show only currently open salons", enum=["1"]),
        OpenApiParameter("total_amount", float, description="Booking total used to compute deposit.amount, e.g. 250"),
    ],
    responses=SalonCardSerializer(many=True),
)
class SalonDiscoveryListView(ListAPIView):
    """Figma discovery list + map screen. Public platform salons."""

    serializer_class = SalonCardSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        # with_published_hours adds the storefront's PUBLISHED hours and its
        # manual state. Without it the card falls back to null for every time
        # field, because branch.opening_hours is no longer read here: that
        # column is the salon-build value and not what the manager published.
        qs = with_published_hours(discoverable_salons())

        lat = self.request.query_params.get("lat")
        lng = self.request.query_params.get("lng")
        if lat and lng:
            try:
                qs = with_distance(qs, float(lat), float(lng))
                qs = qs.filter(lat__isnull=False, lng__isnull=False)
            except ValueError:
                pass

        rating_min = self.request.query_params.get("rating_min")
        hijab_mode = self.request.query_params.get("hijab_mode")
        if hijab_mode in ("1", "true"):
            qs = qs.filter(hijab_certified=True)

        self._filter_open_now = self.request.query_params.get("open_now") in ("1", "true")

        if rating_min:
            qs = qs.filter(avg_rating__gte=float(rating_min))

        city = self.request.query_params.get("city")
        if city:
            qs = qs.filter(branch_city__iexact=city)

        category = self.request.query_params.get("category")
        if category in ("gents", "ladies", "unisex"):
            qs = filter_by_category(qs, category)

        sort = self.request.query_params.get("sort")
        if sort == "distance" and lat and lng:
            return qs.order_by("distance_km")
        return qs.order_by("-avg_rating")

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        if getattr(self, "_filter_open_now", False):
            # open_now cannot be a SQL filter: whether a salon is open depends
            # on its own timezone and on a JSONB grid, so it is computed in
            # Python and fed back as an id list. That evaluates the queryset
            # once extra, which is why it only happens when the flag is set.
            serializer = SalonCardSerializer()
            open_ids = [
                obj.pk for obj in queryset
                if serializer.get_is_open_now(obj)
            ]
            return queryset.filter(pk__in=open_ids)
        return queryset


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
        return with_published_hours(discoverable_salons())


class SalonProfileView(APIView):
    """
    GET /api/v1/salon/<uuid>

    APIView, not RetrieveAPIView, because the response is an object and the
    project's default pagination class would otherwise wrap list endpoints in
    an envelope the app does not expect.

    AllowAny explicitly: the project default is IsAuthenticated. With AllowAny
    the JWT still populates request.user when a token is sent, which is what
    the user-specific fields will need, and does not 401 when it is absent.
    """

    permission_classes = [AllowAny]

    def get(self, request, salon_id):
        salon = salon_profile(salon_id)
        if salon is None:
            raise Http404("Salon not found")

        snapshot = read_snapshot(salon)

        # The clock lives HERE, at the edge, and nowhere else. hours.resolve
        # is pure and takes the local time it should reason about, the same
        # discipline the platform's domain services use.
        tz = zoneinfo.ZoneInfo(salon.branch_timezone or "Asia/Dubai")
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
    """
    GET /api/v1/salon/<uuid>/services

    Two levels out of a one-level table. A category with a parent becomes a
    GROUP under that parent's chip; a category without one is its own chip and
    its own group. Tenants do not populate parent_id today, so this reads as a
    flat list now and becomes a real tree the moment they do, with no change
    to the response shape or to the app.
    """

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
        price_minor = svc.branch_price_minor or svc.price_minor
        return {
            "id": str(svc.id),
            "name": svc.name,
            "description": svc.description,
            "price": major(price_minor),
            "duration_min": svc.duration_minutes,
            "duration_max": svc.duration_minutes,
        }


class SalonStylistsView(APIView):
    """
    GET /api/v1/salon/<uuid>/stylists

    Four fields the mobile contract asks for have no source in this schema and
    are sent as null: rating and review_count (storefront_review carries no
    staff_id, so a review is of the salon and not the person), years_experience
    (no column anywhere) and day_off (derivable from shift_roster later, but
    never stored as a fact). Null means hide the element.
    """

    permission_classes = [AllowAny]

    def get(self, request, salon_id):
        salon = salon_profile(salon_id)
        if salon is None:
            raise Http404("Salon not found")

        stylists = [
            {
                "id": str(s.id),
                # position is the salon's own label for the seat; job_title is
                # what the person calls themselves. Prefer position, since it
                # is the one a manager sets deliberately per branch.
                "name": " ".join(filter(None, [s.first_name, s.last_name])) or None,
                "role": s.position or s.job_title,
                "avatar_url": s.avatar_url,
                "rating": None,
                "review_count": None,
                "years_experience": None,
                "day_off": None,
            }
            for s in salon_stylists(salon)
        ]

        return Response({"stylists": stylists})


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


class DiscoverMapView(APIView):
    """Map viewport endpoint — returns lightweight venue markers.

    ``GET /api/v1/discover/map?latitude=…&longitude=…&latitudeDelta=…&longitudeDelta=…[&category=…]``

    All parameters are optional. Bounding-box filtering replaces pagination.
    A hard ``LIMIT`` inside the selector acts as a safety valve.
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
            OpenApiParameter("zoom", int, required=False,
                             description="Current map zoom level"),
            OpenApiParameter("category", str, required=False,
                             description="Venue category filter",
                             enum=["all", "gents", "ladies", "unisex"]),
        ],
        responses={200: MapVenueSerializer(many=True)},
    )
    def get(self, request):
        params = request.query_params

        def parse_float(param_name: str) -> float | None:
            val = params.get(param_name)
            if val is not None and val != "":
                try:
                    return float(val)
                except ValueError:
                    pass
            return None

        latitude = parse_float("latitude") or parse_float("lat")
        longitude = parse_float("longitude") or parse_float("lng")
        latitude_delta = (
            parse_float("latitudeDelta")
            or parse_float("latitude_delta")
            or parse_float("lat_delta")
        )
        longitude_delta = (
            parse_float("longitudeDelta")
            or parse_float("longitude_delta")
            or parse_float("lng_delta")
        )

        category = params.get("category", "all")

        venues_qs = map_venues(
            latitude=latitude,
            longitude=longitude,
            latitude_delta=latitude_delta,
            longitude_delta=longitude_delta,
            category=category,
        )

        serializer = MapVenueSerializer(venues_qs, many=True)
        venues = serializer.data

        return Response({
            "mode": "markers",
            "count": len(venues),
            "venues": venues,
        })

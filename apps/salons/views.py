from datetime import datetime, timezone as dt_timezone
from django.db.models import F
from django.http import Http404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from .snapshot import field as snap_field

from rest_framework.permissions import AllowAny, IsAuthenticated
from apps.accounts.models import Favourite
from .serializers import FavouriteSerializer

from . import timezones
from .hours import resolve as resolve_hours
from .money import major
from .params import ParamError, parse_discovery, parse_map
from .selectors import (
    discoverable_salons,
    filter_by_category,
    filter_by_radius,
    filter_by_search,
    filter_top_rated,
    map_venues,
    salon_categories,
    salon_packages,
    salon_products,
    salon_profile,
    salon_services,
    salon_stories,
    salon_stylists,
    with_distance,
    with_published_card_fields,
)
from .serializers import (
    MapVenueSerializer,
    SalonCardSerializer,
    SalonProfileSerializer,
    StorySalonSerializer,
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

class FavouriteListView(SalonDiscoveryListView):
    """
    GET /api/v1/favourite

    The customer's saved salons, with the same filters as /discover.

    INHERITS the discovery view rather than repeating it. Category, search,
    is_top_rated, is_open_now, ordering and pagination all already work there,
    and a second copy would drift the first time one of them changes.

    The only difference is the queryset: narrowed to the ids this customer
    saved, newest save first.
    """

    serializer_class = FavouriteSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        saved = Favourite.objects.filter(account=self.request.user)
        by_salon = {str(f.storefront_id): f for f in saved}

        qs = super().get_queryset().filter(id__in=by_salon.keys())

        # Each salon carries its favourite row, so the serializer can send the
        # favourite's own id alongside the salon without a second query.
        self._favourites = by_salon
        return qs

    def get_serializer(self, *args, **kwargs):
        # The list endpoint serializes salons; the contract wants favourites.
        # Wrap each salon in its favourite row here rather than changing the
        # queryset, which would lose every filter the parent applies.
        if args and hasattr(args[0], "__iter__"):
            wrapped = []
            for salon in args[0]:
                fav = self._favourites[str(salon.id)]
                fav.salon = salon
                wrapped.append(fav)
            args = (wrapped,) + args[1:]
        return super().get_serializer(*args, **kwargs)
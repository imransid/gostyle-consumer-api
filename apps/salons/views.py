import math
from rest_framework.views import APIView
from rest_framework.response import Response
from .models import Salon
from .serializers import SalonSerializer
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated

from .selectors import discoverable_salons, map_venues, with_distance
from .serializers import MapVenueSerializer, SalonCardSerializer
from drf_spectacular.utils import OpenApiParameter, extend_schema

from .money import major
from .selectors import salon_categories, salon_services


import zoneinfo
from datetime import datetime

from django.http import Http404
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .selectors import salon_profile
from .serializers import SalonProfileSerializer
from .snapshot import items as snap_items
from .snapshot import read_snapshot
from .hours import resolve as resolve_hours



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

        # Group services under their own category id.
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

        exception = None
        auto_rule=False

        hours = resolve_hours(
            weekly=snapshot["HOURS"].get("weekly"),
            exception=exception,
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


def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371  # earth radius km
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


class SalonListView(APIView):
    serializer_class = SalonSerializer

    def get(self, request):
        salons = list(Salon.objects.all())

        lat = request.query_params.get("lat")
        lng = request.query_params.get("lng")
        category = request.query_params.get("category")

        if category in ("gents", "ladies", "unisex"):
            salons = [s for s in salons if s.category == category]

        if lat and lng:
            lat, lng = float(lat), float(lng)
            for s in salons:
                s.distance_km = round(haversine_km(lat, lng, s.latitude, s.longitude), 2)
            salons.sort(key=lambda s: s.distance_km)

        return Response(SalonSerializer(salons, many=True).data)


@extend_schema(
    parameters=[
        OpenApiParameter("lat", float, description="User latitude, e.g. 25.19"),
        OpenApiParameter("lng", float, description="User longitude, e.g. 55.26"),
        OpenApiParameter("sort", str, description="Sort order", enum=["distance", "rating"]),
        OpenApiParameter("rating_min", float, description="Minimum average rating, e.g. 4.5"),
        OpenApiParameter("city", str, description="Filter by city name, e.g. Dubai"),
        OpenApiParameter("hijab_mode", str, description="1 to show only hijab-certified salons", enum=["1"]),
        OpenApiParameter("open_now", str, description="1 to show only currently open salons", enum=["1"]),
    ],
    responses=SalonCardSerializer(many=True),
)
class SalonDiscoveryListView(ListAPIView):
    """Figma discovery list + map screen. Public platform salons."""

    serializer_class = SalonCardSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):          # ← purano get_queryset er JAYGAY ei notun ta
        qs = discoverable_salons()
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
        sort = self.request.query_params.get("sort")
        if sort == "distance" and lat and lng:
            return qs.order_by("distance_km")
        return qs.order_by("-avg_rating")

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        if getattr(self, "_filter_open_now", False):
            serializer = SalonCardSerializer()
            open_ids = [
                obj.pk for obj in queryset
                if serializer.get_is_open_now(obj)
            ]
            return queryset.filter(pk__in=open_ids)
        return queryset


@extend_schema(responses=SalonCardSerializer)
class SalonDiscoveryDetailView(RetrieveAPIView):
    """Single salon card by id (map pin tap / card tap)."""

    serializer_class = SalonCardSerializer
    permission_classes = [AllowAny]
    lookup_field = "pk"

    def get_queryset(self):
        return discoverable_salons()


class DiscoverMapView(APIView):
    """Map viewport endpoint — returns lightweight venue markers.

    ``GET /api/v1/discover/map?sw_lat=…&sw_lng=…&ne_lat=…&ne_lng=…&zoom=…[&category=…]``

    All parameters are optional. Bounding-box filtering replaces pagination.
    A hard ``LIMIT`` inside the selector acts as a safety valve.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter("sw_lat", float, required=False,
                             description="Bounding-box south-west latitude"),
            OpenApiParameter("sw_lng", float, required=False,
                             description="Bounding-box south-west longitude"),
            OpenApiParameter("ne_lat", float, required=False,
                             description="Bounding-box north-east latitude"),
            OpenApiParameter("ne_lng", float, required=False,
                             description="Bounding-box north-east longitude"),
            OpenApiParameter("zoom", int, required=False,
                             description="Current map zoom level"),
            OpenApiParameter("category", str, required=False,
                             description="Venue category filter",
                             enum=["all", "gents", "ladies"]),
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

        sw_lat = parse_float("sw_lat")
        sw_lng = parse_float("sw_lng")
        ne_lat = parse_float("ne_lat")
        ne_lng = parse_float("ne_lng")

        category = params.get("category", "all")

        venues_qs = map_venues(
            sw_lat=sw_lat,
            sw_lng=sw_lng,
            ne_lat=ne_lat,
            ne_lng=ne_lng,
            category=category,
        )

        serializer = MapVenueSerializer(venues_qs, many=True)
        venues = serializer.data

        return Response({
            "mode": "markers",
            "count": len(venues),
            "venues": venues,
        })
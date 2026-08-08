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

def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371  # earth radius km
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


class SalonListView(APIView):
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
    ]
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


class SalonDiscoveryDetailView(RetrieveAPIView):
    """Single salon card by id (map pin tap / card tap)."""

    serializer_class = SalonCardSerializer
    permission_classes = [AllowAny]
    lookup_field = "pk"

    def get_queryset(self):
        return discoverable_salons()


class DiscoverMapView(APIView):
    """Map viewport endpoint — returns lightweight venue markers.

    ``GET /api/v1/discover/map?latitude=…&longitude=…&latitudeDelta=…&longitudeDelta=…[&category=…]``

    All parameters are optional. Bounding-box filtering replaces pagination.
    A hard ``LIMIT`` inside the selector acts as a safety valve.
    """

    permission_classes = [IsAuthenticated]

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


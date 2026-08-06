import math
from rest_framework.views import APIView
from rest_framework.response import Response
from .models import Salon
from .serializers import SalonSerializer
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import AllowAny

from .selectors import discoverable_salons
from .serializers import SalonCardSerializer


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

class SalonDiscoveryListView(ListAPIView):
    """Figma discovery list + map screen. Public platform salons."""

    serializer_class = SalonCardSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = discoverable_salons()

        rating_min = self.request.query_params.get("rating_min")
        if rating_min:
            qs = qs.filter(avg_rating__gte=float(rating_min))

        city = self.request.query_params.get("city")
        if city:
            qs = qs.filter(branch_city__iexact=city)

        return qs.order_by("-avg_rating")


class SalonDiscoveryDetailView(RetrieveAPIView):
    """Single salon card by id (map pin tap / card tap)."""

    serializer_class = SalonCardSerializer
    permission_classes = [AllowAny]
    lookup_field = "pk"

    def get_queryset(self):
        return discoverable_salons()
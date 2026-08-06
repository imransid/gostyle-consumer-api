from rest_framework import serializers
from .models import Salon


class SalonSerializer(serializers.ModelSerializer):
    open = serializers.BooleanField(source="open_now")
    coordinate = serializers.SerializerMethodField()
    distance_km = serializers.FloatField(read_only=True, required=False)

    class Meta:
        model = Salon
        fields = ["id", "name", "category", "rating", "reviews",
                  "open", "hours", "logo", "coordinate", "distance_km"]

    def get_coordinate(self, obj):
        return {"latitude": obj.latitude, "longitude": obj.longitude}

class SalonCardSerializer(serializers.Serializer):
    """Read-only card shape for the discovery list and map (Figma salon card)."""

    id = serializers.UUIDField()
    slug = serializers.CharField()
    name = serializers.CharField(source="branch_name")
    city = serializers.CharField(source="branch_city", allow_null=True)
    rating = serializers.SerializerMethodField()
    review_count = serializers.IntegerField(allow_null=True)
    coordinate = serializers.SerializerMethodField()

    def get_rating(self, obj):
        if obj.avg_rating is None:
            return None
        return round(float(obj.avg_rating), 1)

    def get_coordinate(self, obj):
        if obj.lat is None or obj.lng is None:
            return None
        return {"latitude": obj.lat, "longitude": obj.lng}
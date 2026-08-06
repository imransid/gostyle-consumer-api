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
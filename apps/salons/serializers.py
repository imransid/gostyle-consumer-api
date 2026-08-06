from rest_framework import serializers
from .models import Salon
import zoneinfo
from datetime import datetime

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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
    is_open_now = serializers.SerializerMethodField()
    hours_today = serializers.SerializerMethodField()
    photo_url = serializers.CharField(allow_null=True)

    def get_rating(self, obj):
        if obj.avg_rating is None:
            return None
        return round(float(obj.avg_rating), 1)

    def get_coordinate(self, obj):
        if obj.lat is None or obj.lng is None:
            return None
        return {"latitude": obj.lat, "longitude": obj.lng}

    def _today_window(self, obj):
        hours = obj.opening_hours
        if not hours:
            return None
        tz = zoneinfo.ZoneInfo(obj.branch_timezone or "Asia/Dubai")
        now = datetime.now(tz)
        day = DAY_KEYS[now.weekday()]
        window = hours.get(day)
        if not window or not window.get("open") or not window.get("close"):
            return None
        return now, window

    def get_is_open_now(self, obj):
        result = self._today_window(obj)
        if result is None:
            return None
        now, window = result
        current = now.strftime("%H:%M")
        return window["open"] <= current < window["close"]

    def get_hours_today(self, obj):
        result = self._today_window(obj)
        if result is None:
            return None
        _, window = result
        return f'{window["open"]} - {window["close"]}'
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
    hijab_certified = serializers.BooleanField()
    deposit = serializers.SerializerMethodField()
    closes_at = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()

    def get_distance_km(self, obj):
        d = getattr(obj, "distance_km", None)
        if d is None:
            return None
        return round(d, 1)

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

    def get_deposit(self, obj):
        mode = getattr(obj, "deposit_mode", None)
        if mode is None:
            return None
        if mode == "NONE":
            return {"required": False, "label": "No Deposit"}
        pct = (obj.deposit_bps or 0) // 100
        return {"required": True, "label": f"{pct}% Deposit", "percent": pct}

    def get_closes_at(self, obj):
        result = self._today_window(obj)
        if result is None:
            return None
        now, window = result
        close = window["close"]  # "22:00"
        hh, mm = int(close[:2]), int(close[3:])
        suffix = "PM" if hh >= 12 else "AM"
        hh12 = hh % 12 or 12
        if self.get_is_open_now(obj):
            return f"Closes at {hh12}:{mm:02d} {suffix}"
        opens = window["open"]
        ohh, omm = int(opens[:2]), int(opens[3:])
        osuffix = "PM" if ohh >= 12 else "AM"
        ohh12 = ohh % 12 or 12
        return f"Opens at {ohh12}:{omm:02d} {osuffix}"


class MapVenueSerializer(serializers.Serializer):
    """Minimal marker shape for the map viewport endpoint."""

    id = serializers.UUIDField()
    lat = serializers.FloatField()
    lng = serializers.FloatField()
    rating = serializers.SerializerMethodField()
    salon_profile_image = serializers.CharField(source="photo_url", allow_null=True)

    def get_rating(self, obj) -> float | None:
        if obj.avg_rating is None:
            return None
        return round(float(obj.avg_rating), 1)
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
    cover_url = serializers.CharField(allow_null=True)
    logo_url = serializers.CharField(allow_null=True)
    hijab_certified = serializers.BooleanField()
    deposit = serializers.SerializerMethodField()
    closes_at = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()
    gallery_urls = serializers.ListField(
        child=serializers.CharField(),
        allow_null=True,
        required=False,
    )

    def get_distance_km(self, obj):
        d = getattr(obj, "distance_km", None)
        if d is None:
            return None
        return round(d, 1)

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

    def get_deposit(self, obj):
        mode = getattr(obj, "deposit_mode", None)
        if mode is None:
            return None
        if mode == "NONE":
            return {"required": False, "label": "No Deposit"}
        pct = (obj.deposit_bps or 0) // 100
        return {"required": True, "label": f"{pct}% Deposit", "percent": pct}

    def get_closes_at(self, obj):
        result = self._today_window(obj)
        if result is None:
            return None
        now, window = result
        close = window["close"]  # "22:00"
        hh, mm = int(close[:2]), int(close[3:])
        suffix = "PM" if hh >= 12 else "AM"
        hh12 = hh % 12 or 12
        if self.get_is_open_now(obj):
            return f"Closes at {hh12}:{mm:02d} {suffix}"
        opens = window["open"]
        ohh, omm = int(opens[:2]), int(opens[3:])
        osuffix = "PM" if ohh >= 12 else "AM"
        ohh12 = ohh % 12 or 12
        return f"Opens at {ohh12}:{omm:02d} {osuffix}"

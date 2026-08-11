from rest_framework import serializers
from .models import Salon
import zoneinfo
from datetime import datetime

from . import translate
from .hours import resolve as resolve_hours
from .snapshot import field as snap_field
from .snapshot import items as snap_items

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

class SalonProfileSerializer(serializers.Serializer):
    """
    The salon profile screen's header, info card and check-in card.

    Most of what this returns is NOT in columns: it comes from the published
    snapshot, which the view reads once and passes in through context. Doing
    that in the view rather than here keeps the number of queries visible at
    the call site instead of hidden behind a serializer method.
    """

    id = serializers.UUIDField()
    slug = serializers.CharField()
    cover_url = serializers.CharField(allow_null=True)
    logo_url = serializers.CharField(allow_null=True)
    rating = serializers.SerializerMethodField()
    review_count = serializers.IntegerField(allow_null=True)
    currency = serializers.CharField(allow_null=True)

    gallery = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    tagline = serializers.SerializerMethodField()
    bio = serializers.SerializerMethodField()
    category = serializers.SerializerMethodField()
    price_level = serializers.SerializerMethodField()
    amenities = serializers.SerializerMethodField()
    social_links = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    booking_policy = serializers.SerializerMethodField()

    is_open = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    hours_today = serializers.SerializerMethodField()

    # Agreed as not-yet-available. Present so the app's shape is stable and
    # null means "hide this element". See the platform tickets for the two
    # that need schema work.
    active_booking = serializers.SerializerMethodField()

    # ── helpers ──────────────────────────────────────────────────────
    @property
    def _snap(self):
        return self.context["snapshot"]

    @property
    def _hours(self):
        return self.context["hours"]

    # ── fields ───────────────────────────────────────────────────────
    def get_rating(self, obj) -> float | None:
        if obj.avg_rating is None:
            return None
        return round(float(obj.avg_rating), 1)

    def get_gallery(self, obj) -> list:
        return obj.gallery_urls or []

    def get_name(self, obj) -> str | None:
        # Falls back to the branch name: a salon that published without
        # filling IDENTITY still has to render with a name on the card.
        return snap_field(self._snap, "IDENTITY", "nameEn") or obj.branch_name

    def get_tagline(self, obj) -> str | None:
        return snap_field(self._snap, "IDENTITY", "tagEn")

    def get_bio(self, obj) -> str | None:
        return snap_field(self._snap, "IDENTITY", "aboutEn")

    def get_category(self, obj) -> str | None:
        return translate.category(snap_field(self._snap, "AUDIENCE", "mode"))

    def get_price_level(self, obj) -> int | None:
        return translate.price_level(snap_field(self._snap, "BADGES", "priceTier"))

    def get_amenities(self, obj) -> list:
        return translate.amenities(snap_items(self._snap, "AMENITIES", "items"))

    def get_social_links(self, obj) -> list:
        return translate.social_links(self._snap.get("SOCIALS"))

    def get_location(self, obj) -> dict:
        # MAP overrides the branch address for PRESENTATION; the branch value
        # is the fallback, not a competing truth.
        lat = snap_field(self._snap, "MAP", "lat", obj.lat)
        lng = snap_field(self._snap, "MAP", "lng", obj.lng)
        return {
            "address": snap_field(self._snap, "MAP", "address"),
            "latitude": lat,
            "longitude": lng,
            "map_url": f"https://maps.google.com/?q={lat},{lng}" if lat and lng else None,
        }

    def get_booking_policy(self, obj) -> dict:
        return translate.booking_policy(
            getattr(obj, "deposit_mode", None),
            getattr(obj, "deposit_bps", None),
            self.context.get("cancel_window_hours"),
        )

    def get_is_open(self, obj) -> bool | None:
        return self._hours["is_open"]

    def get_status(self, obj) -> str | None:
        return self._hours["status"]

    def get_hours_today(self, obj) -> str | None:
        return self._hours["hours_today"]

    def get_active_booking(self, obj):
        # Needs the consumer-to-customer link that does not exist yet.
        return None
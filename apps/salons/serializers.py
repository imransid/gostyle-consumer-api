from datetime import datetime
import zoneinfo

from rest_framework import serializers

from . import translate
from .hours import resolve as resolve_hours
from .snapshot import field as snap_field
from .snapshot import items as snap_items

# Normalize platform salon.gender values to the app's category vocabulary.
CATEGORY_ALIASES = {
    "male": "gents",
    "men": "gents",
    "female": "ladies",
    "women": "ladies",
    "both": "unisex",
    "mixed": "unisex",
    "all": "unisex",
}


class SalonCardSerializer(serializers.Serializer):
    """
    Read-only card shape for the discovery list and map (Figma salon card).

    HOURS COME FROM THE PUBLISHED SNAPSHOT, not branch.opening_hours. That
    column is written once by nest-build at salon provisioning and never
    touched again; the hours a customer should see are the ones the manager
    published on their storefront card. The two drift the moment a salon edits
    its hours, and this card showed the onboarding value until that was fixed.

    The time arithmetic itself lives in hours.py, shared with the profile
    endpoint. Two endpoints describing the same salon must not be able to
    disagree about whether it is open, and a second implementation here is
    exactly how they would.
    """

    id = serializers.UUIDField()
    slug = serializers.CharField()
    name = serializers.CharField(source="branch_name")
    city = serializers.CharField(source="branch_city", allow_null=True)
    category = serializers.SerializerMethodField()
    rating = serializers.SerializerMethodField()
    review_count = serializers.IntegerField(allow_null=True)
    coordinate = serializers.SerializerMethodField()
    is_open_now = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    hours_today = serializers.SerializerMethodField()
    cover_url = serializers.CharField(allow_null=True)
    logo_url = serializers.CharField(allow_null=True)
    hijab_certified = serializers.BooleanField()
    deposit = serializers.SerializerMethodField()
    free_cancellation = serializers.SerializerMethodField()
    closes_at = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()
    gallery_urls = serializers.ListField(
        child=serializers.CharField(),
        allow_null=True,
        required=False,
    )

    def _hours(self, obj):
        """
        Resolve once per salon and cache the answer on the instance.

        Four fields ask the same question. resolve() is pure Python over data
        already fetched, so it is cheap, but running it four times per card on
        a fifteen-row page is sixty needless calls and four chances for the
        fields to disagree if the clock ticks between them.
        """
        cached = getattr(obj, "_resolved_hours", None)
        if cached is None:
            tz = zoneinfo.ZoneInfo(getattr(obj, "branch_timezone", None) or "Asia/Dubai")
            now = datetime.now(tz)
            published = getattr(obj, "published_hours", None) or {}
            cached = resolve_hours(
                weekly=published.get("weekly"),
                # Dated exceptions are not read on the list: the table does
                # not exist in this schema version. The profile endpoint has
                # the same gap, and both are re-checked before release.
                exception=None,
                state=getattr(obj, "manual_state", None),
                weekday_index=now.weekday(),
                now_hhmm=now.strftime("%H:%M"),
            )
            obj._resolved_hours = cached
        return cached

    def get_distance_km(self, obj):
        d = getattr(obj, "distance_km", None)
        if d is None:
            return None
        return round(d, 1)

    def get_category(self, obj):
        raw = getattr(obj, "category", None)
        if not raw:
            return None
        value = raw.lower()
        return CATEGORY_ALIASES.get(value, value)

    def get_rating(self, obj):
        if obj.avg_rating is None:
            return None
        return round(float(obj.avg_rating), 1)

    def get_coordinate(self, obj):
        if obj.lat is None or obj.lng is None:
            return None
        return {"latitude": obj.lat, "longitude": obj.lng}

    def get_is_open_now(self, obj):
        return self._hours(obj)["is_open"]

    def get_status(self, obj):
        # OPEN, BUSY, WALK_INS, SPECIAL_HOURS or CLOSED. is_open_now cannot
        # carry "Busy", which is a state no grid can compute and the reason
        # the salon set it by hand.
        return self._hours(obj)["status"]

    def get_hours_today(self, obj):
        return self._hours(obj)["hours_today"]

    def get_closes_at(self, obj):
        return self._hours(obj)["closes_at"]

    def _total_amount(self):
        """Optional ?total_amount= query param (e.g. selected services total)."""
        request = self.context.get("request")
        if request is None:
            return None
        raw = request.query_params.get("total_amount")
        if raw in (None, ""):
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def get_deposit(self, obj):
        mode = getattr(obj, "deposit_mode", None)
        if mode is None:
            return None
        if mode == "NONE":
            return {
                "required": False,
                "label": "No Deposit",
                "percentage": None,
                "amount": None,
            }
        pct = (obj.deposit_bps or 0) // 100
        total = self._total_amount()
        amount = round(total * pct / 100) if total is not None else None
        return {
            "required": True,
            "label": f"{pct}% Deposit",
            "percentage": pct,
            "amount": amount,
        }

    def get_free_cancellation(self, obj):
        hours = getattr(obj, "cancel_window_hours", None)
        return hours is not None and hours > 0


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

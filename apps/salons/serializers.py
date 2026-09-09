from datetime import datetime

from rest_framework import serializers

from . import timezones, translate
from .geo import format_distance
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

    # ── the mobile card contract ─────────────────────────────────────
    id = serializers.UUIDField()
    name = serializers.SerializerMethodField()
    category = serializers.SerializerMethodField()
    logo_url = serializers.CharField(allow_null=True)
    rating = serializers.SerializerMethodField()
    review_count = serializers.SerializerMethodField()
    distance = serializers.SerializerMethodField()
    open = serializers.SerializerMethodField()
    opens_at = serializers.SerializerMethodField()
    closes_at = serializers.SerializerMethodField()
    has_story = serializers.BooleanField()
    gallery = serializers.SerializerMethodField()

    # ── everything else the card and its neighbours render ───────────
    slug = serializers.CharField()
    city = serializers.CharField(source="branch_city", allow_null=True)
    coordinate = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    hours_today = serializers.SerializerMethodField()
    status_line = serializers.SerializerMethodField()
    cover_url = serializers.CharField(allow_null=True)
    hijab_certified = serializers.BooleanField()
    deposit = serializers.SerializerMethodField()
    free_cancellation = serializers.SerializerMethodField()

    # ── DEPRECATED: the older spelling of three fields above ─────────
    # Kept for one release so a client on the current build does not break the
    # day this ships. Delete them, and this block, once the app is on `open`,
    # `distance` and `gallery`. See docs/DISCOVERY_API.md.
    is_open_now = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()
    gallery_urls = serializers.ListField(
        child=serializers.CharField(),
        allow_null=True,
        required=False,
    )

    def _hours(self, obj):
        """
        Resolve once per salon and cache the answer on the instance.

        SEVEN fields ask the same question. resolve() is pure Python over data
        already fetched, so it is cheap, but running it seven times per card on
        a fifteen-row page is a hundred needless calls and seven chances for
        the fields to disagree if the clock ticks between them — which is how
        a card ends up reading "Open" above "Opens at 9:00 AM".
        """
        cached = getattr(obj, "_resolved_hours", None)
        if cached is None:
            tz = timezones.resolve(getattr(obj, "branch_timezone", None), obj.pk)
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

    def get_name(self, obj) -> str | None:
        """
        The name the salon PUBLISHED, falling back to the branch's own.

        Same order the profile screen uses, and that is the whole point. A
        salon that published "Iron Razor" over a branch recorded as "Al Quoz
        Branch 2" used to appear under one name on the card and the other one
        tap later. A salon that never filled IDENTITY still has to render with
        something, which is what the fallback is for.
        """
        return getattr(obj, "published_name", None) or obj.branch_name

    def get_distance(self, obj) -> str | None:
        """
        "2.0 km", "850 m", or null when the caller sent no location.

        NULL and not "0 km". Without a latitude and longitude there is no
        distance to report, and printing zero would put every salon on the
        customer's doorstep.
        """
        return format_distance(getattr(obj, "distance_km", None))

    def get_open(self, obj) -> bool | None:
        """
        True, False, or NULL when the salon has published no hours at all.

        Null rather than False, and the difference matters: False is "this
        salon is shut right now", null is "nobody has told us when this salon
        opens". Collapsing the second into the first would have the app print
        "Closed" under a salon that may well be serving customers.
        """
        return self._hours(obj)["is_open"]

    def get_opens_at(self, obj) -> str | None:
        return self._hours(obj)["opens_at"]

    def get_status_line(self, obj) -> str | None:
        """
        "Closes at 10:00 PM" / "Opens at 9:00 AM".

        This is what `closes_at` used to hold. It moved here when `closes_at`
        became an actual closing time, because one key cannot mean both a time
        and a sentence about a different time.
        """
        return self._hours(obj)["status_line"]

    def get_gallery(self, obj) -> list[str]:
        """
        Always a list. "No gallery photos" is a fact, and it is `[]`.

        Null would be the app writing a guard around every map() for a case
        that is ordinary rather than exceptional.
        """
        return getattr(obj, "gallery_urls", None) or []

    def get_distance_km(self, obj) -> float | None:
        """DEPRECATED. The numeric form of `distance`."""
        d = getattr(obj, "distance_km", None)
        if d is None:
            return None
        return round(d, 1)

    def get_category(self, obj) -> str | None:
        """
        "gents", "ladies", "unisex", or null.

        THREE VALUES, not two. The `category` FILTER offers gents/ladies/all,
        but a unisex salon is a real thing in the data and it comes back under
        `all`, so the field has to be able to say so. Forcing it into gents or
        ladies would be reporting something the salon never said about itself.

        Null when the platform's salon.gender is empty, which is its own
        answer: this salon has not declared one.
        """
        raw = getattr(obj, "category", None)
        if not raw:
            return None
        value = raw.lower()
        return CATEGORY_ALIASES.get(value, value)

    def get_rating(self, obj) -> str | None:
        """
        "4.5", or NULL when nobody has reviewed this salon.

        A STRING, matching the mobile contract, which is the one place this
        serializer formats a number it could have sent raw.

        Never "0.0". A salon with no reviews has no average — the question has
        no answer yet — and answering it with zero renders a brand-new salon
        as though the market had judged it and put it at the bottom of the
        scale. `review_count` says "0" beside it, which is the real fact.
        """
        if obj.avg_rating is None:
            return None
        return f"{float(obj.avg_rating):.1f}"

    def get_review_count(self, obj) -> str:
        """
        "0", "1", "128". A string, per the contract, and never null.

        Coalesced to zero in SQL, because unlike the rating this one always
        has an answer: nobody having reviewed a salon yet IS a count.
        """
        return str(getattr(obj, "review_count", 0) or 0)

    def get_coordinate(self, obj) -> dict | None:
        if obj.lat is None or obj.lng is None:
            return None
        return {"latitude": obj.lat, "longitude": obj.lng}

    def get_is_open_now(self, obj) -> bool | None:
        """DEPRECATED. The older spelling of `open`."""
        return self._hours(obj)["is_open"]

    def get_status(self, obj) -> str | None:
        # OPEN, BUSY, WALK_INS, SPECIAL_HOURS or CLOSED. is_open_now cannot
        # carry "Busy", which is a state no grid can compute and the reason
        # the salon set it by hand.
        return self._hours(obj)["status"]

    def get_hours_today(self, obj) -> str | None:
        return self._hours(obj)["hours_today"]

    def get_closes_at(self, obj) -> str | None:
        """
        Today's closing time, "10:00 PM".

        CHANGED. This used to hold the sentence "Closes at 10:00 PM", or
        "Opens at 9:00 AM" when the salon was shut — one key doing two jobs,
        which is why there was nowhere to put an opening time. The sentence
        now lives in `status_line`.
        """
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

    def get_deposit(self, obj) -> dict | None:
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

    def get_free_cancellation(self, obj) -> bool:
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

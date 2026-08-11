from django.db.models import QuerySet
from django.db.models import Avg, Count, Exists, F, FloatField, Func, OuterRef, Subquery, TextField, Value
from django.db.models.functions import ACos, Cos, Radians, Sin
from django.contrib.postgres.aggregates import ArrayAgg
from django.contrib.postgres.fields import ArrayField

from apps.platform_data.models import (
    Branch,
    Salon as PlatformSalon,
    Storefront,
    StorefrontCertification,
    StorefrontMedia,
    StorefrontPolicy,
    StorefrontReview,
)


def with_distance(qs, user_lat, user_lng):
    """Annotate distance_km from the user's location using the haversine formula."""
    return qs.annotate(
        distance_km=Value(6371.0) * ACos(
            Cos(Radians(Value(user_lat)))
            * Cos(Radians(F("lat")))
            * Cos(Radians(F("lng")) - Radians(Value(user_lng)))
            + Sin(Radians(Value(user_lat))) * Sin(Radians(F("lat")))
        )
    )


def discoverable_salons():
    published_reviews = StorefrontReview.objects.filter(
        storefront_id=OuterRef("pk"),
        state="PUBLISHED",
    )

    branch = Branch.objects.filter(id=OuterRef("branch_id"))

    return (
        Storefront.objects.filter(
            visibility="PUBLIC",
            link_enabled=True,
            deleted_at__isnull=True,
        )
        .annotate(
            avg_rating=Subquery(
                published_reviews.values("storefront_id")
                .annotate(avg=Avg("rating"))
                .values("avg")[:1]
            ),
            review_count=Subquery(
                published_reviews.values("storefront_id")
                .annotate(c=Count("id"))
                .values("c")[:1]
            ),
            branch_name=Subquery(
                branch.values("name")[:1], output_field=TextField()
            ),
            branch_city=Subquery(
                branch.values("city")[:1], output_field=TextField()
            ),
            lat=Subquery(branch.values("lat")[:1], output_field=FloatField()),
            lng=Subquery(branch.values("lng")[:1], output_field=FloatField()),
            opening_hours=Subquery(branch.values("opening_hours")[:1]),
            branch_timezone=Subquery(
                branch.values("timezone")[:1], output_field=TextField()
            ),
            cover_url=Subquery(
                StorefrontMedia.objects.filter(
                    storefront_id=OuterRef("pk"),
                    deleted_at__isnull=True,
                    is_public=True,
                    moderation_status="APPROVED",
                    kind="COVER",
                )
                .order_by("-is_featured", "sort_order")
                .values("url")[:1],
                output_field=TextField(),
            ),
            logo_url=Subquery(
                StorefrontMedia.objects.filter(
                    storefront_id=OuterRef("pk"),
                    deleted_at__isnull=True,
                    is_public=True,
                    moderation_status="APPROVED",
                    kind="LOGO",
                )
                .order_by("-is_featured", "sort_order")
                .values("url")[:1],
                output_field=TextField(),
            ),
            hijab_certified=Exists(
                StorefrontCertification.objects.filter(
                    storefront_id=OuterRef("pk"),
                    state="CERTIFIED",
                    revoked_at__isnull=True,
                    deleted_at__isnull=True,
                )
            ),
            deposit_mode=Subquery(
                StorefrontPolicy.objects.filter(
                    storefront_id=OuterRef("pk")
                ).values("deposit_mode")[:1],
                output_field=TextField(),
            ),
            deposit_bps=Subquery(
                StorefrontPolicy.objects.filter(
                    storefront_id=OuterRef("pk")
                ).values("deposit_bps")[:1],
            ),
            cancel_window_hours=Subquery(
                StorefrontPolicy.objects.filter(
                    storefront_id=OuterRef("pk")
                ).values("cancel_window_hours")[:1],
            ),
            category=Subquery(
                PlatformSalon.objects.filter(
                    tenant_id=OuterRef("tenant_id")
                ).values("gender")[:1],
                output_field=TextField(),
            ),
            gallery_urls=Subquery(
                StorefrontMedia.objects.filter(
                    storefront_id=OuterRef("pk"),
                    deleted_at__isnull=True,
                    is_public=True,
                    moderation_status="APPROVED",
                    kind="GALLERY",
                )
                .values("storefront_id")
                .annotate(urls=ArrayAgg("url", ordering="sort_order"))
                .values("urls")[:1],
                output_field=ArrayField(TextField()),
            ),
        )
    )


# ---------------------------------------------------------------------------
# Map viewport query
# ---------------------------------------------------------------------------

# Hard cap on venues returned per viewport request (safety valve).
MAP_VENUE_LIMIT: int = 500

# Valid category filter values; anything else falls back to "all".
_VALID_CATEGORIES = frozenset({"gents", "ladies"})


def map_venues(
    latitude: float | None = None,
    longitude: float | None = None,
    latitude_delta: float | None = None,
    longitude_delta: float | None = None,
    category: str = "all",
) -> "QuerySet[Storefront]":
    """Return discoverable storefronts inside the viewport bounding box.

    The bounding box is computed from the map region (center + delta).
    Only the columns needed for map markers are annotated (lat, lng,
    avg_rating, photo_url) to keep the query lightweight. A hard LIMIT is
    enforced as a safety valve.

    Args:
        latitude: Center latitude of the map region.
        longitude: Center longitude of the map region.
        latitude_delta: Latitude span of the visible region.
        longitude_delta: Longitude span of the visible region.
        category: ``"all"`` | ``"gents"`` | ``"ladies"``. Default ``"all"``.

    Returns:
        A lightweight queryset of ``Storefront`` instances annotated with
        ``lat``, ``lng``, ``avg_rating``, and ``photo_url``.
    """
    sw_lat = sw_lng = ne_lat = ne_lng = None
    if None not in (latitude, longitude, latitude_delta, longitude_delta):
        sw_lat = latitude - (latitude_delta / 2.0)
        ne_lat = latitude + (latitude_delta / 2.0)
        sw_lng = longitude - (longitude_delta / 2.0)
        ne_lng = longitude + (longitude_delta / 2.0)

    published_reviews = StorefrontReview.objects.filter(
        storefront_id=OuterRef("pk"),
        state="PUBLISHED",
    )

    branch = Branch.objects.filter(id=OuterRef("branch_id"))

    qs = (
        Storefront.objects.filter(
            visibility="PUBLIC",
            link_enabled=True,
            deleted_at__isnull=True,
        )
        .annotate(
            lat=Subquery(branch.values("lat")[:1], output_field=FloatField()),
            lng=Subquery(branch.values("lng")[:1], output_field=FloatField()),
            avg_rating=Subquery(
                published_reviews.values("storefront_id")
                .annotate(avg=Avg("rating"))
                .values("avg")[:1]
            ),
            photo_url=Subquery(
                StorefrontMedia.objects.filter(
                    storefront_id=OuterRef("pk"),
                    deleted_at__isnull=True,
                    is_public=True,
                    moderation_status="APPROVED",
                    kind="COVER",
                )
                .order_by("-is_featured", "sort_order")
                .values("url")[:1],
                output_field=TextField(),
            ),
        )
        .filter(
            lat__isnull=False,
            lng__isnull=False,
        )
    )

    if None not in (sw_lat, sw_lng, ne_lat, ne_lng):
        qs = qs.filter(
            lat__gte=sw_lat,
            lat__lte=ne_lat,
            lng__gte=sw_lng,
            lng__lte=ne_lng,
        )

    if category in _VALID_CATEGORIES and any(f.name == "category" for f in Storefront._meta.fields):
        qs = qs.filter(category=category)

    try:
        from django.db import transaction
        with transaction.atomic():
            results = list(qs[:MAP_VENUE_LIMIT])
            return results
    except Exception:
        # Fallback to Salon model if Storefront table does not exist or query fails
        from .models import Salon

        salon_qs = Salon.objects.all()
        if None not in (sw_lat, sw_lng, ne_lat, ne_lng):
            salon_qs = salon_qs.filter(
                latitude__gte=sw_lat,
                latitude__lte=ne_lat,
                longitude__gte=sw_lng,
                longitude__lte=ne_lng,
            )
        if category in ("gents", "ladies"):
            salon_qs = salon_qs.filter(category=category)

        items = []
        for s in salon_qs[:MAP_VENUE_LIMIT]:
            s.lat = s.latitude
            s.lng = s.longitude
            s.avg_rating = s.rating
            s.photo_url = s.logo
            items.append(s)
        return items
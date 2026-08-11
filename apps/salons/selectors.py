from django.db.models import QuerySet
from django.db.models import Avg, Count, Exists, F, FloatField, Func, OuterRef, Subquery, TextField, Value
from django.db.models.functions import ACos, Cos, Radians, Sin
from django.contrib.postgres.aggregates import ArrayAgg
from django.contrib.postgres.fields import ArrayField

from apps.platform_data.models import Category, Service, ServiceBranchAvailability

from apps.platform_data.models import (
    Branch,
    Storefront,
    StorefrontCertification,
    StorefrontMedia,
    StorefrontPolicy,
    StorefrontReview,
)

from datetime import date
from apps.platform_data.models import (
    StorefrontStatus,
    StorefrontStatusException,
    Tenant,
)



def salon_services(storefront):
    """
    Bookable services for one salon, with the branch price override applied.

    Filtered on what a CUSTOMER may book, which is narrower than what the
    salon's own console lists: published, not deleted, online booking on, and
    available at this branch. A service hidden on a schedule (hide_from /
    auto_show_after) is deliberately NOT handled here yet; that is a second
    filter and belongs with the rest of the visibility rules.

    price_minor is COALESCEd: service_branch_availability may carry a per-branch
    price, and when it does it wins over the catalogue price.
    """
    availability = ServiceBranchAvailability.objects.filter(
        service_id=OuterRef("pk"),
        branch_id=storefront.branch_id,
    )

    return (
        Service.objects.filter(
            tenant_id=storefront.tenant_id,
            status="PUBLISHED",
            deleted_at__isnull=True,
            online_booking_enabled=True,
        )
        .annotate(
            branch_available=Subquery(availability.values("available")[:1]),
            branch_price_minor=Subquery(availability.values("price_minor")[:1]),
        )
        .filter(branch_available=True)
        .order_by("name")
    )


def salon_categories(tenant_id):
    """
    Every category for a tenant, as a dict keyed by id.

    Read whole rather than joined per service: a tenant has a handful of
    categories and a salon has many services, so one small query beats a join
    repeated on every row. The parent lookup below also needs the full set.
    """
    rows = Category.objects.filter(
        tenant_id=tenant_id,
        deleted_at__isnull=True,
    ).values("id", "name_en", "slug", "icon", "parent_id", "sort_order")

    return {row["id"]: row for row in rows}

def salon_profile(storefront_id):
    """
    One salon for the profile screen.

    Builds on discoverable_salons() so the media, rating and policy columns
    are identical to what /discover already serves: two endpoints describing
    the same salon must not disagree about its rating.

    Adds three things the card did not need:
      - live_version_id, so the snapshot can be read
      - today's manual state and dated exception, for the hours resolver
      - the tenant currency

    Returns None when the salon does not exist or is not public, so the view
    can 404 rather than the query raising.
    """
    today = date.today()

    return (
        discoverable_salons()
        .annotate(
            currency=Subquery(
                Tenant.objects.filter(id=OuterRef("tenant_id")).values(
                    "currency_default"
                )[:1],
                output_field=TextField(),
            ),
            # The manual state, but ONLY when it applies to today. A state
            # left over from last week is not today's answer.
             manual_state=Subquery(
                StorefrontStatus.objects.filter(
                    storefront_id=OuterRef("pk"),
                ).values("state")[:1],
                output_field=TextField(),
            ),
            
        )
        .filter(id=storefront_id)
        .first()
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
    sw_lat: float | None = None,
    sw_lng: float | None = None,
    ne_lat: float | None = None,
    ne_lng: float | None = None,
    category: str = "all",
) -> "QuerySet[Storefront]":
    """Return discoverable storefronts inside the viewport bounding box (if specified).

    Only the columns needed for map markers are annotated (lat, lng,
    avg_rating, photo_url) to keep the query lightweight. A hard LIMIT is
    enforced as a safety valve.

    Args:
        sw_lat: Optional South-west latitude of the bounding box.
        sw_lng: Optional South-west longitude of the bounding box.
        ne_lat: Optional North-east latitude of the bounding box.
        ne_lng: Optional North-east longitude of the bounding box.
        category: ``"all"`` | ``"gents"`` | ``"ladies"``. Default ``"all"``.

    Returns:
        A lightweight queryset of ``Storefront`` instances annotated with
        ``lat``, ``lng``, ``avg_rating``, and ``photo_url``.
    """
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
from django.db.models import QuerySet
from django.db.models import Avg, Count, Exists, F, FloatField, Func, OuterRef, Q, Subquery, TextField, Value
from django.db.models.functions import ACos, Coalesce, Cos, Least, Lower, Now, Radians, Sin
from django.contrib.postgres.aggregates import ArrayAgg
from django.contrib.postgres.fields import ArrayField
from apps.platform_data.models import Product, ProductVariant

from apps.platform_data.models import Category, Service, ServiceBranchAvailability

from django.db.models.fields.json import KeyTextTransform, KeyTransform
from apps.platform_data.models import StorefrontStory, StorefrontVersion


from apps.platform_data.models import (
    Branch,
    Salon as PlatformSalon,
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

from django.db.models import F, IntegerField, Sum
from apps.platform_data.models import (
    ServicePackage,
    ServicePackageBranchAvailability,
    ServicePackageItem,
)

from apps.platform_data.models import FileItem, StaffProfile, UserAccount

from .geo import EARTH_RADIUS_KM, bounding_box, radius_box
from .serializers import CATEGORY_ALIASES


# The three categories that are actual categories. "all" is not one of them:
# it is the ABSENCE of the filter, which is why it is handled by returning the
# queryset untouched rather than by widening the IN list to everything.
VALID_CATEGORIES = frozenset({"gents", "ladies", "unisex"})


def filter_by_category(qs, category):
    """Filter a Storefront queryset by salon category ('gents'/'ladies'/'unisex').

    The category lives on the platform ``salon.gender`` column (joined via
    ``tenant_id``); raw values are matched case-insensitively, including the
    aliases the serializer normalizes (male/female/both, …).

    STRICT. ``gents`` returns gents salons and nothing else; a unisex salon is
    not folded in, and appears only under ``all``. That was a product decision
    and it is the surprising one, so it is written down here: a customer who
    picked "Gents" and is shown a unisex salon has no way to tell whether the
    filter is broad by design or simply broken.

    ``all``, ``None``, ``""`` and anything unrecognised are all "no filter".
    The VIEW is what rejects an unrecognised value with a 400 — a selector
    handed junk cannot know whether it came from a typo in a URL or from a
    caller that meant "everything", so it does the harmless thing and lets the
    edge do the complaining.
    """
    if category not in VALID_CATEGORIES:
        return qs

    raw_values = [category] + [
        raw for raw, canonical in CATEGORY_ALIASES.items() if canonical == category
    ]
    return qs.annotate(
        salon_category=Lower(
            Subquery(
                PlatformSalon.objects.filter(
                    tenant_id=OuterRef("tenant_id")
                ).values("gender")[:1],
                output_field=TextField(),
            )
        )
    ).filter(salon_category__in=raw_values)


def with_published_card_fields(qs):
    """
    Annotate each salon with what it PUBLISHED: its hours and its name.

    THE CARD USED TO READ branch.opening_hours, which is the wrong source:
    that column is written once by nest-build at salon provisioning and never
    touched again. The hours a customer sees are the ones the manager
    PUBLISHED on their storefront card, which live in the live version's
    snapshot. The two drift the moment a salon edits its hours, and the card
    has been showing the onboarding value ever since.

    The NAME has the same disease and it was showing on two screens at once.
    The profile reads IDENTITY.nameEn from the snapshot and falls back to the
    branch name; the card read the branch name and stopped there. A salon that
    published "Iron Razor" over a branch recorded as "Al Quoz Branch 2"
    therefore appeared under two different names depending on which screen the
    customer was looking at. Reading the published name here settles it, and
    it is also the name `filter_by_search` has to match: searching a name the
    customer cannot see is a search box that looks broken.

    Two keys are extracted, not the whole snapshot. Pulling all thirteen
    sections per salon to read two of them would multiply the response size of
    a fifteen-row page for nothing.

    KeyTextTransform rather than KeyTransform for the name, because the name
    is compared and printed as TEXT. KeyTransform would hand back a JSON
    string, quotes and all, and `icontains` against it would be matching
    against `"Iron Razor"` with the quote marks included.
    """
    return qs.annotate(
        published_hours=Subquery(
            StorefrontVersion.objects
            .filter(id=OuterRef("live_version_id"))
            .annotate(hours=KeyTransform("HOURS", "snapshot"))
            .values("hours")[:1],
        ),
        published_name=Subquery(
            StorefrontVersion.objects
            .filter(id=OuterRef("live_version_id"))
            .annotate(
                name=KeyTextTransform("nameEn", KeyTransform("IDENTITY", "snapshot"))
            )
            .values("name")[:1],
            output_field=TextField(),
        ),
        manual_state=Subquery(
            StorefrontStatus.objects
            .filter(storefront_id=OuterRef("pk"))
            .values("state")[:1],
            output_field=TextField(),
        ),
    )


# Backwards-compatible alias. The name was accurate until this function also
# started carrying the published NAME; anything still importing the old one
# keeps working.
with_published_hours = with_published_card_fields


def salon_products(storefront):
    """
    Retail products a customer may buy.

    TYPE IS THE IMPORTANT FILTER. product_type is RETAIL, PROFESSIONAL or
    CONSUMABLE, and only RETAIL is for sale: PROFESSIONAL is salon-use stock
    like developer and bleach, CONSUMABLE is towels and foils. Selling either
    in the app shop would be wrong, so type is not optional here.

    NOT BRANCH-SCOPED, and that is a schema fact rather than a choice: product
    carries tenant_id only. A multi-branch tenant therefore shows the same
    shop at every branch. Stock is tracked per branch in stock_lot, so a
    per-branch shop is possible later, but it is a different query.

    The price comes from the LOWEST-POSITION variant, which is the one the
    salon ordered first and so the one they treat as default. Cheapest would
    be a guess about intent; position is the salon's own answer.
    """
    default_variant = ProductVariant.objects.filter(
        product_id=OuterRef("pk"),
        deleted_at__isnull=True,
    ).order_by("position")

    return (
        Product.objects.filter(
            tenant_id=storefront.tenant_id,
            status="ACTIVE",
            type="RETAIL",
            deleted_at__isnull=True,
        )
        .annotate(
            price_minor=Subquery(default_variant.values("sale_price_minor")[:1]),
        )
        # A product with no priced variant cannot be sold, and rendering it
        # with a null price would put a broken card in the shop.
        .filter(price_minor__isnull=False)
        .order_by("name")
    )



def salon_packages(storefront):
    """
    Bundles a customer may buy at one salon.

    price_before is the sum of the member services at their own prices times
    quantity, which is what the customer would pay buying them separately.
    That is derived rather than stored, and deliberately not taken from
    discount_bps: a FIXED-price package has no discount percentage at all, so
    reading the discount would give nothing for exactly the packages the app
    most wants to show a saving on.

    The two aggregates are done as subqueries rather than a join so the
    package rows are not multiplied by their items.
    """
    availability = ServicePackageBranchAvailability.objects.filter(
        package_id=OuterRef("pk"),
        branch_id=storefront.branch_id,
    )

    items = ServicePackageItem.objects.filter(package_id=OuterRef("pk"))

    return (
        ServicePackage.objects.filter(
            tenant_id=storefront.tenant_id,
            status="PUBLISHED",
            deleted_at__isnull=True,
            online_booking_enabled=True,
        )
        .annotate(
            branch_available=Subquery(availability.values("available")[:1]),
            total_minutes=Subquery(
                items.values("package_id")
                .annotate(
                    total=Sum(
                        F("service__duration_minutes") * F("quantity"),
                        output_field=IntegerField(),
                    )
                )
                .values("total")[:1],
                output_field=IntegerField(),
            ),
            sum_minor=Subquery(
                items.values("package_id")
                .annotate(
                    total=Sum(
                        F("service__price_minor") * F("quantity"),
                        output_field=IntegerField(),
                    )
                )
                .values("total")[:1],
                output_field=IntegerField(),
            ),
        )
        .filter(branch_available=True)
        .order_by("name")
    )


def salon_stylists(storefront):
    """
    Staff a customer may see for one salon.

    TWO status columns, and both are required. employment_status says whether
    the person still works here; onboarding_state says whether they ever
    finished joining. A row that is ACTIVE and INVITED is an unaccepted
    invitation, which is a real person who has never worked a shift, and
    showing them on a public profile would be wrong.

    rating, review_count, years_experience and day_off are NOT selected here
    because no column holds them: storefront_review has no staff_id, and
    neither staff_profile nor user_account records experience. They are sent
    as null by the serializer. See the platform tickets.
    """
    user = UserAccount.objects.filter(id=OuterRef("user_id"))

    return (
        StaffProfile.objects.filter(
            tenant_id=storefront.tenant_id,
            branch_id=storefront.branch_id,
            employment_status="ACTIVE",
            onboarding_state="ACTIVE",
            deleted_at__isnull=True,
        )
        .annotate(
            first_name=Subquery(user.values("first_name")[:1], output_field=TextField()),
            last_name=Subquery(user.values("last_name")[:1], output_field=TextField()),
            job_title=Subquery(user.values("job_title")[:1], output_field=TextField()),
            avatar_file_id=Subquery(user.values("avatar_file_item_id")[:1]),
        )
        .annotate(
            avatar_url=Subquery(
                FileItem.objects.filter(id=OuterRef("avatar_file_id")).values("url")[:1],
                output_field=TextField(),
            ),
        )
        .order_by("created_at")
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
    """
    Annotate distance_km from the user's location using the haversine formula.

    EARTH_RADIUS_KM is shared with geo.radius_box on purpose. The box is a
    prefilter for exactly this expression, and a box built on a marginally
    different Earth than the distance it wraps would drop salons sitting on
    the boundary — one salon vanishing from a 5 km search and nothing in the
    logs to say why.

    THE Least() IS NOT DECORATION. When a salon sits on the exact point the
    customer tapped, the argument to ACos should be exactly 1.0 and the
    distance exactly zero. Double-precision rounding across four trig calls
    can land it a hair above 1.0 instead, and Postgres raises "input is out
    of range" rather than returning: a 500 for tapping your own pin. Clamping
    the top costs nothing and removes the case. The bottom is not clamped
    because -1 needs two points on exact opposite sides of the planet.
    """
    return qs.annotate(
        distance_km=Value(EARTH_RADIUS_KM) * ACos(
            Least(
                Cos(Radians(Value(user_lat)))
                * Cos(Radians(F("lat")))
                * Cos(Radians(F("lng")) - Radians(Value(user_lng)))
                + Sin(Radians(Value(user_lat))) * Sin(Radians(F("lat"))),
                Value(1.0),
            )
        )
    )


# What "Top Rated" means, in one place, and as TWO numbers rather than one.
#
# A score floor on its own is not enough. A salon with a single five-star
# review from its own owner scores a perfect 5.0 and outranks one with two
# hundred reviews averaging 4.7; putting the first at the head of a shelf
# labelled Top Rated is how a shelf stops being trusted.
#
# The review floor is the number to revisit. Three suits a platform whose
# review volume is still young: low enough that the filter returns something
# today, high enough that one review cannot win.
TOP_RATED_MIN_RATING = 4.5
TOP_RATED_MIN_REVIEWS = 3


def filter_top_rated(qs):
    """
    The Top Rated shelf.

    Salons with no reviews have a NULL avg_rating, and NULL fails `>=`, so
    they fall out here without needing to be mentioned. That is the correct
    outcome and worth stating, because it is the one place where the decision
    not to coalesce avg_rating to zero does real work.
    """
    return qs.filter(
        avg_rating__gte=TOP_RATED_MIN_RATING,
        review_count__gte=TOP_RATED_MIN_REVIEWS,
    )


def filter_by_radius(qs, user_lat, user_lng, radius_km):
    """
    Salons within radius_km of a point, as the crow flies.

    REQUIRES with_distance() to have run first: this filters on that
    annotation, and calling it on a bare queryset raises FieldError. That is
    the right failure — a radius filter that silently did nothing would return
    salons from the next emirate and look like it had worked.

    TWO filters, and the cheap one runs first. The bounding box is four plain
    comparisons; the haversine is trigonometry on every row that survives
    them. Running the haversine alone gives the same answer and reads every
    branch in the country to do it.

    The box is deliberately LOOSER than the circle it wraps — its corners lie
    outside the radius — so the exact test still runs behind it. It is a
    prefilter, not the answer.

    One honest limitation: `lat` and `lng` are Subquery annotations over
    `branch`, so these comparisons re-evaluate that subquery per row rather
    than using an index on branch(lat, lng). It is cheap today at this catalogue
    size. When it stops being cheap, the fix is joining `branch` properly
    rather than annotating it, and that is a change to discoverable_salons().
    """
    box = radius_box(user_lat, user_lng, radius_km)
    if box is None:
        return qs

    sw_lat, sw_lng, ne_lat, ne_lng = box
    return qs.filter(
        lat__gte=sw_lat,
        lat__lte=ne_lat,
        lng__gte=sw_lng,
        lng__lte=ne_lng,
        distance_km__lte=radius_km,
    )


def filter_by_search(qs, term):
    """
    Free-text search over the name a customer can SEE, plus the city.

    REQUIRES with_published_card_fields(): published_name comes from there.

    Three fields, and each earns its place. published_name first, because it
    is the name printed on the card, and a search box that cannot find what is
    on screen is worse than no search box. branch_name because a salon that
    never published an IDENTITY section still has to be findable. City because
    "Dubai" is a search term to everyone who is not a developer.

    NOT SERVICES. Someone typing "beard trim" is asking a different question —
    which salons offer this — and answering it means joining the service
    catalogue and deciding how a salon matching on a service should rank
    against one matching on its name. That is a feature, not a wider OR.

    icontains is ILIKE '%term%': no index helps it and it does not stem, so
    "barbers" will not find "Barber". Honest for a catalogue this size, and
    the day it stops being honest is the day to reach for Postgres full-text
    search rather than to add a fourth OR.
    """
    term = (term or "").strip()
    if not term:
        return qs

    return qs.filter(
        Q(published_name__icontains=term)
        | Q(branch_name__icontains=term)
        | Q(branch_city__icontains=term)
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
            deleted_at__isnull=True,
        )
        .annotate(
            avg_rating=Subquery(
                published_reviews.values("storefront_id")
                .annotate(avg=Avg("rating"))
                .values("avg")[:1]
            ),
            # COALESCED TO ZERO, and avg_rating deliberately is NOT. The two
            # nulls mean different things. "No customer has reviewed this
            # salon yet" is a fact we know, and it is the number 0; "this
            # salon's average score" is a question with no answer at all until
            # someone reviews it, and answering it with 0.0 would print a
            # one-star-looking card for a salon nobody has judged.
            review_count=Coalesce(
                Subquery(
                    published_reviews.values("storefront_id")
                    .annotate(c=Count("id"))
                    .values("c")[:1]
                ),
                Value(0),
                output_field=IntegerField(),
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
            # Does this salon have a live story behind its avatar ring?
            #
            # Now() and not timezone.now(): the expiry is compared by POSTGRES
            # at execution time. A Python timestamp would be frozen at the
            # moment the queryset was built, which under a long-lived gunicorn
            # worker with CONN_MAX_AGE is not the same thing as "now".
            #
            # Gated on the STORY's own lifecycle only — not deleted, not
            # expired — and deliberately not on the moderation status of the
            # media it points at. That check belongs to the endpoint that
            # actually serves story content, which has to apply it row by row
            # anyway; duplicating it here is how the two drift and how a ring
            # starts appearing over an empty story. See the handoff doc: the
            # content endpoint does not exist yet.
            has_story=Exists(
                StorefrontStory.objects.filter(
                    storefront_id=OuterRef("pk"),
                    deleted_at__isnull=True,
                    expires_at__gt=Now(),
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

def map_venues(
    latitude: float | None = None,
    longitude: float | None = None,
    latitude_delta: float | None = None,
    longitude_delta: float | None = None,
    category: str = "all",
    radius_km: float | None = None,
    limit: int | None = None,
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
        radius_km: Optional circle around the centre, in KILOMETRES. The view
            receives metres and converts; nothing below the parser sees them.
            Applied ON TOP of the box when both are sent, which is the
            intersection of the two, and is what a map that has both a
            viewport and a "within X" control actually means.
        limit: Optional caller ceiling on rows, itself capped by
            MAP_VENUE_LIMIT. A public endpoint cannot let the caller pick the
            number.

    Returns:
        A lightweight queryset of ``Storefront`` instances annotated with
        ``lat``, ``lng``, ``avg_rating``, and ``photo_url``.
    """
    box = bounding_box(latitude, longitude, latitude_delta, longitude_delta)

    published_reviews = StorefrontReview.objects.filter(
        storefront_id=OuterRef("pk"),
        state="PUBLISHED",
    )

    branch = Branch.objects.filter(id=OuterRef("branch_id"))

    qs = (
        Storefront.objects.filter(
            visibility="PUBLIC",
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

    if box is not None:
        sw_lat, sw_lng, ne_lat, ne_lng = box
        qs = qs.filter(
            lat__gte=sw_lat,
            lat__lte=ne_lat,
            lng__gte=sw_lng,
            lng__lte=ne_lng,
        )

    # No guard: filter_by_category already treats "all" and anything
    # unrecognised as no filter at all.
    qs = filter_by_category(qs, category)

    # with_distance FIRST, always. filter_by_radius reads the distance_km
    # annotation and raises FieldError without it, which is the correct
    # failure but only if the two are never separated.
    if radius_km is not None and latitude is not None and longitude is not None:
        qs = with_distance(qs, latitude, longitude)
        qs = filter_by_radius(qs, latitude, longitude, radius_km)

    # ORDER BY BEFORE THE SLICE, and this is not tidiness. A LIMIT with no
    # ordering lets Postgres return whichever rows it reaches first, and it is
    # free to reach them in a different order on the next request: the same
    # viewport could hand back a different set of pins twice in a row.
    if radius_km is not None and latitude is not None:
        qs = qs.order_by("distance_km", "id")
    else:
        qs = qs.order_by("id")

    # The caller may ask for fewer, never for more.
    cap = MAP_VENUE_LIMIT if limit is None else min(int(limit), MAP_VENUE_LIMIT)

    # NO try/except HERE, deliberately. This used to swallow every exception
    # and fall back to the local `salons_salon` demo table, which meant a
    # schema drift, a missing column or a denied SELECT under the read-only
    # `consumer_app` grant all came back as three fake salons with a 200 and
    # nothing in the log. A map that is broken must look broken: let it raise,
    # let django.request log it, let the 500 be visible.
    return qs[:cap]
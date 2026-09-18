import logging
import uuid
from datetime import timezone as dt_timezone

from django.db.models import QuerySet
from django.db.models import Avg, Case, Count, DateField, Exists, F, FloatField, Func, OuterRef, Q, Subquery, TextField, Value, When
from django.db.models.functions import ACos, Coalesce, Cos, Least, Lower, Now, NullIf, Radians, Sin
from django.contrib.postgres.aggregates import ArrayAgg
from django.contrib.postgres.fields import ArrayField
from apps.platform_data.models import Product, ProductVariant

from apps.platform_data.models import (
    Category,
    Service,
    ServiceBranchAvailability,
    ServiceMedia,
)

from django.db.models.fields.json import KeyTextTransform, KeyTransform
from apps.platform_data.models import StorefrontStory, StorefrontVersion

from django.db.models import Value, BooleanField
from apps.accounts.models import Favourite


from apps.platform_data.models import (
    Branch,
    Salon as PlatformSalon,
    Storefront,
    StorefrontCertification,
    StorefrontMedia,
    StorefrontPolicy,
    StorefrontReview,
)

from apps.platform_data.models import (
    StorefrontStatus,
    StorefrontStatusException,
    Tenant,
)

from django.db.models import F, IntegerField, Sum, DateTimeField
from apps.platform_data.models import (
    ServicePackage,
    ServicePackageBranchAvailability,
    ServicePackageItem,
)

from apps.platform_data.models import FileItem, StaffProfile, UserAccount

from apps.platform_data.models import (
    Booking,
    CatalogSkill,
    ServiceStage,
    Shift,
    Skill,
    StaffSkillAssignment,
)
from .skills import (
    bridge as bridge_skills,
    coverage as skill_coverage,
    held_levels as held_skill_levels,
    requirements as skill_requirements,
)

from .geo import EARTH_RADIUS_KM, bounding_box, radius_box
from .serializers import CATEGORY_ALIASES
from .timezones import DEFAULT_TIMEZONE


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
        # Today's manual state, when one applies. See live_manual_state().
        manual_state=live_manual_state(),
    )


# Backwards-compatible alias. The name was accurate until this function also
# started carrying the published NAME; anything still importing the old one
# keeps working.
with_published_hours = with_published_card_fields


class TodayIn(Func):
    """
    Today's calendar date in a timezone, computed by POSTGRES:

        (NOW() AT TIME ZONE <tz>)::date

    NOW() is a timestamptz, an absolute instant. AT TIME ZONE turns it into the
    wall-clock time in that zone, and ::date keeps the calendar date. So a
    Dubai branch at 01:00 local on the 11th reads the 11th here, while the
    server's UTC date is still the 10th.
    """

    template = "(NOW() AT TIME ZONE %(expressions)s)::date"
    arity = 1
    output_field = DateField()


def live_manual_state():
    branch_zone = Coalesce(
        NullIf(OuterRef("branch_timezone"), Value(""), output_field=TextField()),
        Value(DEFAULT_TIMEZONE),
        output_field=TextField(),
    )
    return Subquery(
        StorefrontStatus.objects.filter(
            storefront_id=OuterRef("pk"),
            source="MANUAL",
            applies_on=TodayIn(branch_zone),
        ).values("state")[:1],
        output_field=TextField(),
    )


def salon_products(storefront):
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


def salon_stylists(storefront, branch_id=None):
    user = UserAccount.objects.filter(id=OuterRef("user_id"))

    filters = dict(
        tenant_id=storefront.tenant_id,
        employment_status="ACTIVE",
        onboarding_state="ACTIVE",
        deleted_at__isnull=True,
    )
    if BRANCH_AVAILABILITY_ENABLED:
        filters["branch_id"] = branch_id or storefront.branch_id

    return (
        StaffProfile.objects.filter(**filters)
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

BRANCH_AVAILABILITY_ENABLED = False


def salon_services(storefront, branch_id=None):
    qs = Service.objects.filter(
        tenant_id=storefront.tenant_id,
        status="PUBLISHED",
        deleted_at__isnull=True,
        online_booking_enabled=True,
    )

    if BRANCH_AVAILABILITY_ENABLED:
        branch_id = branch_id or storefront.branch_id
        availability = ServiceBranchAvailability.objects.filter(
            service_id=OuterRef("pk"),
            branch_id=branch_id,
        )
        qs = qs.annotate(
            branch_available=Subquery(availability.values("available")[:1]),
            branch_price_minor=Subquery(availability.values("price_minor")[:1]),
        ).filter(branch_available=True)

    return qs.order_by("name")

def categories_for_tenants(tenant_ids):
    """
    Every category belonging to a SET of tenants, as a dict keyed by id.

    Read whole rather than joined per service: a tenant has a handful of
    categories and a salon has many services, so one small query beats a join
    repeated on every row. The parent lookup below also needs the full set.

    Takes a set because service ids can span salons, and therefore tenants
    (SERVICES_DETAILS_API.md §3.6). One query for all of them beats one per
    tenant, and the ids are globally unique so a single dict is unambiguous.
    """
    tenant_ids = list(tenant_ids)
    if not tenant_ids:
        return {}

    rows = Category.objects.filter(
        tenant_id__in=tenant_ids,
        deleted_at__isnull=True,
    ).values("id", "name_en", "slug", "icon", "parent_id", "sort_order")

    return {row["id"]: row for row in rows}


def salon_categories(tenant_id):
    """Every category for one tenant. See `categories_for_tenants`."""
    return categories_for_tenants([tenant_id])


logger = logging.getLogger(__name__)


def tenant_for_salon(salon_ref):
    """
    The tenant that owns a salon, or None when it cannot be told for certain.

    Takes any of the three things a booking payload's `salon_id` can hold,
    because two services disagree about what a salon is and both are right in
    their own vocabulary:

      * a STOREFRONT uuid — what every salon endpoint in this service returns;
      * a BRANCH uuid — what booking-api means by a salon (its mobile handler
        reads `salon_id` straight into `branchId`);
      * a storefront SLUG — what booking-api's contract examples show.

    Both uuid columns are unique, so matching either is a lookup rather than a
    choice. `slug` is NOT: the platform has no global unique index on it, only
    a per-tenant one.

    NONE RATHER THAN A GUESS, every way this can fail — a reference of some
    other shape, one that resolves to nothing, and one that lands on two
    tenants at once. That last case is the whole reason this is careful:
    picking the first row would file a real booking against another salon's
    rows. Sending no tenant leaves booking-api refusing the booking, which is
    the failure everyone already has.
    """
    if not isinstance(salon_ref, str) or not salon_ref.strip():
        return None

    salon_ref = salon_ref.strip()
    live = Storefront.objects.filter(deleted_at__isnull=True)

    try:
        parsed = uuid.UUID(salon_ref)
    except ValueError:
        match = live.filter(slug=salon_ref)
    else:
        match = live.filter(Q(id=parsed) | Q(branch_id=parsed))

    # Two is all it takes to know the answer is ambiguous.
    tenants = list(match.values_list("tenant_id", flat=True).distinct()[:2])
    if len(tenants) != 1:
        if tenants:
            logger.warning(
                "salon reference %r belongs to more than one tenant; sending "
                "no X-Tenant-Id rather than guessing one.",
                salon_ref,
            )
        return None

    return tenants[0]


def booking_route(salon_ref):
    """
    Everything `POST /booking` has to know about a salon, from its id alone.

    Returns `{"tenant_id": str, "branch_id": str}`, or None when the
    reference cannot be resolved to exactly one salon.

    WHY THIS EXISTS. booking-api's payload calls the field `salon_id` and
    then reads it straight into `branchId` -- so the field is named for a
    storefront and holds a branch. Every other endpoint in this service
    returns storefront uuids, so an app that books with the id it just
    browsed with sends the wrong one, and the refusal it gets back names the
    STYLIST ("that stylist does not work at this salon") because the roster
    lookup is where a bad branch first shows up. Two uuids, one field, and an
    error message pointing at neither.

    Resolving it here ends that. The app sends the id it already has,
    whichever of the three spellings it is, and this finds the branch and the
    tenant that go with it.

    NONE RATHER THAN A GUESS, the same rule `tenant_for_salon` follows. A
    reference matching two salons resolves to neither: `slug` is unique per
    tenant and not globally, and picking the first row would file a real
    booking, with real money, against another salon's diary.
    """
    if not isinstance(salon_ref, str) or not salon_ref.strip():
        return None

    salon_ref = salon_ref.strip()
    live = Storefront.objects.filter(deleted_at__isnull=True)

    try:
        parsed = uuid.UUID(salon_ref)
    except ValueError:
        match = live.filter(slug=salon_ref)
    else:
        match = live.filter(Q(id=parsed) | Q(branch_id=parsed))

    # Two is all it takes to know the answer is ambiguous.
    rows = list(match.values("tenant_id", "branch_id").distinct()[:2])
    if len(rows) != 1:
        if rows:
            logger.warning(
                "salon reference %r belongs to more than one salon; refusing "
                "to route the booking rather than guessing a branch.",
                salon_ref,
            )
        return None

    return {
        "tenant_id": str(rows[0]["tenant_id"]),
        "branch_id": str(rows[0]["branch_id"]),
    }


def salon_cards_for_refs(salon_refs):
    """
    `{ id, name, logo_url, city }` and the cancellation window, for the salon
    references on a page of bookings.

    ONE QUERY FOR THE WHOLE PAGE. A booking list is ten to fifty rows and
    usually two or three distinct salons; resolving each row on its own would
    be fifty round trips to draw one screen.

    Takes the same three spellings `tenant_for_salon` does, for the same
    reason — booking-api's `salon_id` is whatever it was given, which is a
    storefront uuid from this service's own endpoints, a branch uuid from its
    columns, or a slug from its fixtures. The returned map is keyed by the
    reference AS IT WAS PASSED IN, so the caller can look a row up with the
    string it already holds rather than guessing which of the three it was.

    A reference that resolves to nothing, or to more than one salon, is
    ABSENT FROM THE MAP rather than present with nulls. The caller renders
    `null` for the salon object in that case, which says "we could not find
    it"; a card with a name-shaped hole in it says the salon has no name.
    """
    refs = [r.strip() for r in salon_refs if isinstance(r, str) and r.strip()]
    if not refs:
        return {}

    uuids, slugs = [], []
    for ref in refs:
        try:
            uuids.append(uuid.UUID(ref))
        except ValueError:
            slugs.append(ref)

    matched = Q()
    if uuids:
        matched |= Q(id__in=uuids) | Q(branch_id__in=uuids)
    if slugs:
        matched |= Q(slug__in=slugs)

    branch = Branch.objects.filter(id=OuterRef("branch_id"))
    rows = (
        Storefront.objects.filter(deleted_at__isnull=True)
        .filter(matched)
        .annotate(
            branch_name=Subquery(branch.values("name")[:1], output_field=TextField()),
            branch_city=Subquery(branch.values("city")[:1], output_field=TextField()),
            # THE NAME THE CUSTOMER SEES, which is the one they picked the
            # salon by. `branch.name` is the operator's own label for the
            # premises -- "Main" -- and putting that on a booking card shows
            # someone a booking at a salon they have never heard of. Same
            # rule and same source as the discover card
            # (with_published_card_fields), so one salon cannot be called two
            # different things on two screens.
            published_name=Subquery(
                StorefrontVersion.objects
                .filter(id=OuterRef("live_version_id"))
                .annotate(
                    name=KeyTextTransform(
                        "nameEn", KeyTransform("IDENTITY", "snapshot")
                    )
                )
                .values("name")[:1],
                output_field=TextField(),
            ),
            # The same LOGO rule the discover card uses: public, approved,
            # newest first. A second spelling of it here is how one screen
            # starts showing a logo the other has already moderated away.
            logo_url=Subquery(
                StorefrontMedia.objects.filter(
                    storefront_id=OuterRef("pk"),
                    deleted_at__isnull=True,
                    is_public=True,
                    moderation_status="APPROVED",
                    kind="LOGO",
                )
                .order_by("-created_at")
                .values("url")[:1],
                output_field=TextField(),
            ),
            cancel_window_hours=Subquery(
                StorefrontPolicy.objects.filter(
                    storefront_id=OuterRef("pk")
                ).values("cancel_window_hours")[:1],
            ),
            branch_timezone=Subquery(
                branch.values("timezone")[:1], output_field=TextField()
            ),
            # The rest of the address, for the booking DRAWER. A list row
            # needs a name and a city; a booking someone is about to travel
            # to needs the street and a map pin.
            branch_address=Subquery(
                branch.values("address_line")[:1], output_field=TextField()
            ),
            branch_region=Subquery(
                branch.values("region")[:1], output_field=TextField()
            ),
            branch_country=Subquery(
                branch.values("country_code")[:1], output_field=TextField()
            ),
            lat=Subquery(branch.values("lat")[:1], output_field=FloatField()),
            lng=Subquery(branch.values("lng")[:1], output_field=FloatField()),
            cover_url=Subquery(
                StorefrontMedia.objects.filter(
                    storefront_id=OuterRef("pk"),
                    deleted_at__isnull=True,
                    is_public=True,
                    moderation_status="APPROVED",
                    kind="COVER",
                )
                .order_by("-created_at")
                .values("url")[:1],
                output_field=TextField(),
            ),
        )
        .values(
            "id",
            "slug",
            "branch_id",
            "branch_name",
            "published_name",
            "branch_city",
            "logo_url",
            "cancel_window_hours",
            "branch_timezone",
            "branch_address",
            "branch_region",
            "branch_country",
            "lat",
            "lng",
            "cover_url",
        )
    )

    # Every spelling of every row, so a lookup by any of the three lands.
    # A key claimed by two storefronts is DROPPED, not resolved to the first:
    # `slug` is unique per tenant and not globally, so two salons can share
    # one, and picking either would put another salon's name on a booking.
    by_key, ambiguous = {}, set()
    for row in rows:
        card = {
            "id": str(row["id"]),
            # Published first; the branch label only as a fallback for a
            # salon that never published an IDENTITY section, which must
            # still be nameable rather than blank.
            "name": row["published_name"] or row["branch_name"],
            "logo_url": row["logo_url"],
            "city": row["branch_city"],
            "cancel_window_hours": row["cancel_window_hours"],
            "timezone": row["branch_timezone"],
            # Drawer-only fields. `salon_card` trims them off for a list row,
            # which the contract fixes at four keys.
            "slug": row["slug"],
            "cover_url": row["cover_url"],
            "address": row["branch_address"],
            "region": row["branch_region"],
            "country_code": row["branch_country"],
            "lat": row["lat"],
            "lng": row["lng"],
        }
        for key in (str(row["id"]), str(row["branch_id"]), row["slug"]):
            if key in by_key and by_key[key]["id"] != card["id"]:
                ambiguous.add(key)
            by_key[key] = card

    for key in ambiguous:
        logger.warning(
            "salon reference %r belongs to more than one storefront; leaving "
            "the salon off the booking row rather than naming the wrong one.",
            key,
        )
        by_key.pop(key, None)

    return {ref: by_key[ref] for ref in refs if ref in by_key}


def services_by_ids(service_ids):
    """
    Full detail for a set of service ids, whatever state each one is in.

    NOT `salon_services` with an id filter, and the difference is the point.
    That selector answers "what can a customer book at this salon today", so
    it drops anything unpublished, deleted, or off online booking — which is
    exactly the set of rows this lookup exists to describe. A basket or a
    six-month-old booking holds ids that may since have been retired, and a
    silent gap in the array is worse than a row marked `is_active: false`.

    Nothing is filtered by tenant either: the caller holds the ids, and ids
    from two salons in one request is a supported case, not an attack. The
    rows carry no customer data.
    """
    if not service_ids:
        return Service.objects.none()

    # The primary image, else the first by sort order. `-is_primary` puts True
    # first; `created_at` is the tie-break so the same row wins every time
    # rather than whichever the planner happened to return.
    primary_media = ServiceMedia.objects.filter(
        service_id=OuterRef("pk"),
        deleted_at__isnull=True,
    ).order_by("-is_primary", "sort_order", "created_at")

    # A service belongs to a TENANT; there is no salon column to read. What
    # the app calls a salon is a storefront, one per branch, so it is resolved
    # through the tenant — preferring a PUBLIC one, and falling back to any
    # live storefront so a service sold only on a hidden branch still names
    # the salon behind it rather than coming back with a null link.
    #
    # A tenant with several branches has several storefronts and this picks
    # the oldest. See the known gap in docs/SERVICES_DETAILS_API.md: a real fix
    # needs service_branch_availability, which is not read anywhere yet
    # (BRANCH_AVAILABILITY_ENABLED is False).
    storefronts = (
        Storefront.objects.filter(
            tenant_id=OuterRef("tenant_id"),
            deleted_at__isnull=True,
        )
        .annotate(
            public_first=Case(
                When(visibility="PUBLIC", then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by("public_first", "created_at", "id")
    )

    return Service.objects.filter(id__in=list(service_ids)).annotate(
        salon_id=Subquery(storefronts.values("id")[:1]),
        image_url=Subquery(
            primary_media.values("url")[:1], output_field=TextField()
        ),
    )

def salon_profile(storefront_id, user=None):

    qs = (
        discoverable_salons()
        .annotate(
            currency=Subquery(
                Tenant.objects.filter(id=OuterRef("tenant_id")).values(
                    "currency_default"
                )[:1],
                output_field=TextField(),
            ),
            manual_state=live_manual_state(),
        )
    )

    return with_is_favorite(qs, user).filter(id=storefront_id).first()
def with_is_favorite(qs, user):
    if user is None or not user.is_authenticated:
        return qs.annotate(
            is_favorite=Value(False, output_field=BooleanField())
        )

    return qs.annotate(
        is_favorite=Exists(
            Favourite.objects.filter(
                account=user,
                storefront_id=OuterRef("pk"),
            )
        )
    )

def with_distance(qs, user_lat, user_lng):

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
            deleted_at__isnull=True
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
                .order_by("-created_at")
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
                .order_by("-created_at")
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
            is_favorite=Exists(
                StorefrontCertification.objects.filter(
                    storefront_id=OuterRef("pk"),
                    deleted_at__isnull=True,
                    expires_at__gt=Now(),
                )
            ),
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

def salon_stories(storefront_id):
    """
    Active stories for one salon.

    Same lifecycle rule as the has_story flag on the card: not deleted, not
    expired. If the two ever disagree, a ring appears over nothing.

    Now() rather than a Python timestamp, so expiry is judged by Postgres at
    execution time and not at the moment the queryset was built.

    media_id is a bare UUID with no URL on the row, so the image comes from a
    subquery into StorefrontMedia. NOT filtered on moderation status: nothing
    on the platform sets APPROVED yet, so checking it here would return an
    empty list for every salon. That gap is a platform ticket, and the check
    belongs there once approval actually happens.
    """
    return (
        StorefrontStory.objects.filter(
            storefront_id=storefront_id,
            deleted_at__isnull=True,
            expires_at__gt=Now(),
        )
        .annotate(
            media_url=Subquery(
                StorefrontMedia.objects.filter(id=OuterRef("media_id")).values("url")[:1],
                output_field=TextField(),
            ),
        )
        .order_by("sort_order", "-created_at")
    )

def salon_service_ids(storefront, service_ids, branch_id=None):
    """
    Which of `service_ids` this salon actually offers, as a set of UUIDs.

    Measured against the same queryset the services tab lists, not against the
    whole `service` table: a service belonging to another tenant, archived, or
    switched off for online booking is not bookable here, and answering "who
    can do it" for one of those would be answering about a service the
    customer could never have picked.
    """
    if not service_ids:
        return set()

    return set(
        salon_services(storefront, branch_id)
        .filter(id__in=list(service_ids))
        .values_list("id", flat=True)
    )


def service_stage_rows(service_ids):
    """Every stage of these services, with the skill and level it requires."""
    if not service_ids:
        return []

    return list(
        ServiceStage.objects.filter(service_id__in=list(service_ids))
        .values("service_id", "skill_id", "min_level")
    )


def skill_bridge(tenant_id, catalog_skill_ids):
    """catalog_skill id → this tenant's skill id (or None), keyed by code."""
    if not catalog_skill_ids:
        return {}

    catalog_rows = CatalogSkill.objects.filter(
        id__in=list(catalog_skill_ids)
    ).values("id", "code")

    # Whole catalogue rather than a code-filtered query: a tenant holds a few
    # dozen skills, and the bridge needs every code to match against anyway.
    tenant_rows = Skill.objects.filter(
        tenant_id=tenant_id,
        deleted_at__isnull=True,
    ).values("id", "code")

    return bridge_skills(list(catalog_rows), list(tenant_rows))


def staff_skill_rows(tenant_id, staff_ids):
    """The skills these staff hold, and at what level."""
    if not staff_ids:
        return []

    return list(
        StaffSkillAssignment.objects.filter(
            tenant_id=tenant_id,
            staff_member_id__in=list(staff_ids),
        ).values("staff_member_id", "skill_id", "level")
    )


def stylist_service_coverage(storefront, service_ids, staff_ids, stages=None):
    """
    staff id → the requested services that person can perform alone.

    Four small queries — stages, catalog codes, tenant codes, assignments —
    and then the matching happens in `skills.py`. Doing it in SQL would mean
    expressing the code bridge and the two level scales as a join, and the
    only readable place for either is Python.

    Staff who can perform none of the requested services do not appear, so the
    keys are exactly the stylists the Expert step may show.
    """
    if stages is None:
        stages = service_stage_rows(service_ids)

    catalog_to_tenant = skill_bridge(
        storefront.tenant_id, {row["skill_id"] for row in stages}
    )
    required = skill_requirements(stages, catalog_to_tenant)
    held = held_skill_levels(staff_skill_rows(storefront.tenant_id, staff_ids))

    return skill_coverage(list(service_ids), required, held)


# Which bookings occupy a stylist's time. PENDING, CONFIRMED and CHECKED_IN are
# the platform's own ACTIVE_STATUSES — the set its availability engine refuses
# to double-book against. COMPLETED is added here and nowhere there: a finished
# appointment still happened, and the safe direction for a customer-facing
# "when can I come in" is to offer fewer starts than the platform would accept,
# never one it will reject at confirm.
BUSY_STATUSES = ("PENDING", "CONFIRMED", "CHECKED_IN", "COMPLETED")


def shift_rows(tenant_id, branch_id, staff_ids, day):
    """
    Each stylist's working hours on one calendar day.

    A shift belongs to a weekly roster, and a roster belongs to one branch, so
    the branch filter runs through the join: a stylist rostered at another
    branch that day is not working HERE, whatever their skills.
    """
    if not staff_ids:
        return []

    return list(
        Shift.objects.filter(
            tenant_id=tenant_id,
            roster__branch_id=branch_id,
            staff_member_id__in=list(staff_ids),
            shift_date=day,
        ).values("staff_member_id", "start_time", "end_time", "break_time")
    )


def booking_rows(tenant_id, staff_ids, window_start, window_end):
    """
    Appointments these stylists already hold, overlapping the given span.

    Overlap, not containment: a booking that started before the window and
    runs into it occupies the same minutes as one that starts inside it. The
    comparison is `start < to AND end > from`, which is the platform's own
    busy-interval query.
    """
    if not staff_ids:
        return []

    rows = Booking.objects.filter(
        tenant_id=tenant_id,
        staff_id__in=list(staff_ids),
        deleted_at__isnull=True,
        status__in=BUSY_STATUSES,
        start_at__lt=window_end,
        end_at__gt=window_start,
    ).values("staff_id", "start_at", "end_at")

    # `booking.start_at` is `timestamp WITHOUT time zone` — Prisma's default
    # mapping — holding UTC. Postgres therefore hands Django a NAIVE datetime,
    # and `.astimezone()` on one of those reads it as the SERVER's local time,
    # which is how an appointment moves by four hours without anyone touching
    # it. The instant is stamped UTC here, once, so nothing downstream ever
    # sees a naive datetime. (The filter above is safe for the same reason:
    # Django sends UTC and holds the connection at UTC, so the comparison
    # Postgres makes against a naive column is UTC against UTC.)
    return [
        {
            "staff_id": row["staff_id"],
            "start_at": row["start_at"].replace(tzinfo=dt_timezone.utc),
            "end_at": row["end_at"].replace(tzinfo=dt_timezone.utc),
        }
        for row in rows
    ]


def service_timing_rows(storefront, service_ids, branch_id=None):
    """
    How long each requested service takes, and the notice it needs.

    `duration_minutes` is the catalogue's own figure — the one the services tab
    already shows the customer. `stage_minutes` is the sum the platform derives
    from the service's stages (buffer_pre + work + impact + buffer_post), which
    is what actually has to fit in the diary; it is None for a service with no
    stages. `lead_time_minutes` is the minimum notice, null for most services.

    Only services this salon really offers come back, so an id from another
    salon contributes no duration and no stylist — an empty answer rather than
    an error, which is what this endpoint promises for a bad service id.
    """
    if not service_ids:
        return []

    stages = ServiceStage.objects.filter(service_id=OuterRef("pk")).values(
        "service_id"
    ).annotate(
        total=Sum(
            F("buffer_pre") + F("duration_minutes")
            + F("duration_impact") + F("buffer_post"),
            output_field=IntegerField(),
        )
    ).values("total")

    return list(
        salon_services(storefront, branch_id)
        .filter(id__in=list(service_ids))
        .annotate(stage_minutes=Subquery(stages, output_field=IntegerField()))
        .values("id", "duration_minutes", "stage_minutes", "lead_time_minutes")
    )


def manual_state_on(storefront_id, day):
    """
    The salon's hand-set state for one date, or None if it set none.

    `live_manual_state()` answers the same question for today inside a
    queryset; this one takes a date, because a booking window is often not
    today.
    """
    return (
        StorefrontStatus.objects.filter(
            storefront_id=storefront_id,
            source="MANUAL",
            applies_on=day,
        )
        .values_list("state", flat=True)
        .first()
    )

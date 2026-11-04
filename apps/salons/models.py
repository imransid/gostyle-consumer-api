import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Salon(models.Model):
    class Category(models.TextChoices):
        GENTS = "gents"
        LADIES = "ladies"
        UNISEX = "unisex"

    id = models.SlugField(primary_key=True, max_length=64)  # "velvet-co"
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=10, choices=Category.choices)
    rating = models.DecimalField(max_digits=2, decimal_places=1)
    reviews = models.PositiveIntegerField(default=0)
    open_now = models.BooleanField(default=True)   # "open" clashes with Python builtins style
    hours = models.CharField(max_length=64)
    logo = models.URLField()
    latitude = models.FloatField()
    longitude = models.FloatField()

    def __str__(self):
        return self.name




        

class SalonCountry(models.Model):
    """
    The country a salon's map pin is in, for the map's country filter
    (MAP_COUNTRY_V1, docs/MAP_COUNTRY_API.md).

    Worked out HERE, from the branch's coordinates and the same borders the
    customer's position is judged by (apps/salons/countries.py), never from
    the typed address: platform's own branch.country_code comes from the
    address form, so a mistyped one would hide a salon. A table of ours, not
    a column on platform's branch: platform owns that table, and this
    service only reads it.

    One row per branch, keyed by branch id. `lat` and `lng` are the
    coordinates the country was worked out from: when the branch's differ
    (the salon moved its pin) or the row is missing (a new salon), the row
    is redone (map_country.refresh_salon_countries). `country_code` is null
    for a pin out at sea, more than 25 km from every country.
    """

    branch_id = models.UUIDField(primary_key=True)
    lat = models.FloatField()
    lng = models.FloatField()
    # ISO 3166-1 alpha-2, upper case ("AE").
    country_code = models.CharField(max_length=2, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "salon_country"
        indexes = [
            # The map's filter reads branch ids by country, and the count of
            # live salons per country groups on it.
            models.Index(fields=["country_code", "branch_id"], name="salon_country_code_idx"),
        ]

    def __str__(self):
        return f"{self.branch_id}: {self.country_code or '-'}"


class SearchTag(models.Model):
    """
    One chip of the Smart Search that picks services by WORDS (audit D6,
    docs/SEARCH_OPTIONS_API.md): the quick chips ("Cuts & Fade") and the
    service sub tags ("Fade", "Hot Towel", "Keratin").

    A curated list of ours, because salons name their own services and
    categories, and the platform's service.tags are free text per salon: no
    fixed chip can be read off either. A service matches a tag when one of
    its `match_terms` is in the service's name, its category chip's label
    or one of its tags, case ignored (search_tags.matches). The list is
    edited in the admin; the app reads it from GET /search/options, so a
    new chip needs no app release.

    `slug` is the id the app sends back (quick=..., service_tags=...), so it
    is unique across every group and never renamed once the app has it.
    """

    class Group(models.TextChoices):
        QUICK = "quick", "Quick chips"
        CUTS_STYLING = "cuts_styling", "Cuts & Styling"
        BEARD_SHAVE = "beard_shave", "Beard & Shave"
        COLOR_TREATMENTS = "color_treatments", "Color & Treatments"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    group = models.CharField(max_length=20, choices=Group.choices)
    slug = models.SlugField(max_length=40, unique=True, help_text="The id the app sends. Never rename it.")
    label_en = models.CharField(max_length=60)
    label_ar = models.CharField(max_length=60, blank=True, default="")
    icon = models.CharField(max_length=40, blank=True, default="", help_text="An icon key, as the platform's (lucide names).")
    # A JSON list of words, ["fade", "skin fade"]: no array column, so the
    # service needs no django.contrib.postgres.
    match_terms = models.JSONField(
        default=list,
        help_text='A list of words, like ["fade", "skin fade"]. A service matches when one is in its name, category or tags.',
    )
    sort_order = models.PositiveSmallIntegerField(default=0)
    active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "search_tag"
        ordering = ("group", "sort_order", "slug")

    def __str__(self):
        return f"{self.group}: {self.label_en}"

    def clean(self):
        terms = self.match_terms
        if (
            not isinstance(terms, list)
            or not terms
            or not all(isinstance(term, str) and term.strip() for term in terms)
        ):
            raise ValidationError({"match_terms": 'A list of one or more words, like ["fade"].'})
        self.match_terms = [" ".join(term.split()) for term in terms]


class SalonDiscoveryFlag(models.Model):
    """
    The "Verified salons" mark (MAP_FILTER_VERIFIED_V1, the audit's D2,
    Sarwar 2026-11-04): a yes/no per salon that our staff set in the admin.

    A table of ours, because the platform owns the salon tables (storefront,
    salon: managed = False here) and has no "verified by Go Style" mark.
    `salon_id` is the salon's id in the app, which is the platform's
    STOREFRONT id (one per branch), not its `salon` row (one per business).

    A salon with no row is not verified. The admin list adds a row for
    every public salon when it opens (verified.sync_flags), so staff can
    find each one by name; `salon_name` is a copy for that search only,
    refreshed from the platform each time, never shown to customers.
    """

    salon_id = models.UUIDField(unique=True, help_text="The salon's id in the app (the platform's storefront id).")
    salon_name = models.CharField(max_length=200, blank=True, default="", help_text="A copy for searching here.")
    is_verified = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="The staff member who last changed it.",
    )

    class Meta:
        db_table = "salon_discovery_flag"
        ordering = ("salon_name", "salon_id")

    def __str__(self):
        return f"{self.salon_name or self.salon_id}: {'verified' if self.is_verified else 'not verified'}"

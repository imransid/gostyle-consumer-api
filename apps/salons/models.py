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

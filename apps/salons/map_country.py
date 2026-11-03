"""
The map shows only the customer's country (MAP_COUNTRY_V1): GET /discover/map
with user_latitude and user_longitude, and `country` in its answer.

The mobile team's brief is docs/map-country-fe-contract.md; the guide for the
app is docs/MAP_COUNTRY_API.md.

Three parts:

- `resolve_country`: which country (section 3 of the brief). The phone's
  position when it is in a country, else the map's centre, else none.
- `refresh_salon_countries`: every salon's country, from its coordinates and
  the same borders (models.SalonCountry). Redone for a salon whose pin moved
  or is new, at most once a minute from the map itself
  (`keep_salons_fresh`), and in full by `manage.py salon_countries --apply`.
- `country_payload`: the `country` field, with `has_salons` from a count of
  live salons per country, cached for a minute.

The phone's position is used to pick the country and nothing else: it is
not stored, not logged (config/gunicorn_logging.py and
nginx/log-format-json.conf leave it out of the access logs) and not part of
any cache key outside this process (countries.position_country).
"""

import logging

from django.core.cache import cache
from django.db import DatabaseError, transaction
from django.db.models import Count, Exists, OuterRef, Subquery

from apps.platform_data.models import Branch

from . import countries
from .models import SalonCountry
from .selectors import map_salons

logger = logging.getLogger(__name__)

# How often the map itself looks for salons whose country is missing or out
# of date (a new salon, a moved pin): once a minute, across every worker.
CHECK_EVERY_SECONDS = 60
CHECKED_KEY = "map-country:salons-checked:v1"

# The most salons one map call redoes. The rest wait for the next minute, or
# for `manage.py salon_countries --apply`, which has no limit.
REFRESH_BATCH = 500

# Live salons per country, for `has_salons`.
COUNTS_KEY = "map-country:live-salons:v1"
COUNTS_SECONDS = 60


def resolve_country(user_position, latitude, longitude):
    """
    The country the map is limited to (the brief, section 3), first that answers:

    1. the phone's position (user_latitude, user_longitude), when sent and in a country;
    2. the map's centre (latitude, longitude), when sent and in a country:
       the customer with location off;
    3. none (the centre is out at sea): None, the box only, as before.
    """
    if user_position is not None:
        country = countries.position_country(*user_position)
        if country is not None:
            return country
    if latitude is not None and longitude is not None:
        return countries.position_country(latitude, longitude)
    return None


def country_payload(country):
    """The answer's `country`: null when the map is not limited to one."""
    if country is None:
        return None
    return {
        "code": country.code,
        "name": country.name,
        "has_salons": live_salon_counts().get(country.code, 0) > 0,
    }


def live_salon_counts():
    """
    {country code: live salons}, over every salon the map can show,
    ignoring any box and any filter. Cached for a minute.
    """
    counts = cache.get(COUNTS_KEY)
    if counts is None:
        counts = {row["country_code"]: row["salons"] for row in live_salon_counts_query()}
        cache.set(COUNTS_KEY, counts, COUNTS_SECONDS)
    return counts


def live_salon_counts_query():
    """`live_salon_counts`' one grouped query: rows of country_code and salons."""
    return (
        map_salons()
        .annotate(
            country_code=Subquery(
                SalonCountry.objects.filter(branch_id=OuterRef("branch_id")).values("country_code")[:1]
            )
        )
        .filter(country_code__isnull=False)
        .order_by()
        .values("country_code")
        .annotate(salons=Count("id"))
    )


def stale_branches():
    """
    The branches of the salons the map can show whose country is missing, or
    was worked out from other coordinates (the pin moved): as (branch id,
    lat, lng), ordered by id.
    """
    live = map_salons().values("branch_id")
    return (
        Branch.objects.filter(id__in=live, lat__isnull=False, lng__isnull=False)
        .exclude(
            Exists(
                SalonCountry.objects.filter(
                    branch_id=OuterRef("id"), lat=OuterRef("lat"), lng=OuterRef("lng")
                )
            )
        )
        .order_by("id")
        .values_list("id", "lat", "lng")
    )


def work_out(rows):
    """(branch id, lat, lng) rows as unsaved SalonCountry rows, by the borders."""
    found = []
    for branch_id, lat, lng in rows:
        country = countries.country_at(lat, lng)
        found.append(
            SalonCountry(
                branch_id=branch_id,
                lat=lat,
                lng=lng,
                country_code=country.code if country else None,
            )
        )
    return found


def save(rows):
    """Write SalonCountry rows, a new row or over the old one. Returns how many."""
    if not rows:
        return 0
    SalonCountry.objects.bulk_create(
        rows,
        update_conflicts=True,
        unique_fields=["branch_id"],
        update_fields=["lat", "lng", "country_code", "updated_at"],
    )
    # A salon may have come in, or moved country: count again.
    cache.delete(COUNTS_KEY)
    return len(rows)


def refresh_salon_countries(limit=REFRESH_BATCH):
    """Redo up to `limit` stale salons (None: every one). Returns how many were written."""
    rows = stale_branches()
    if limit is not None:
        rows = rows[:limit]
    return save(work_out(rows))


def keep_salons_fresh():
    """
    From the map: at most once a minute, across every worker, redo the
    salons whose country is missing or out of date. A failure here is logged
    and the map goes on with the countries it has.
    """
    if not cache.add(CHECKED_KEY, True, CHECK_EVERY_SECONDS):
        return
    try:
        # A savepoint: a failed write must not spoil the map's own query.
        with transaction.atomic():
            written = refresh_salon_countries()
    except DatabaseError:
        logger.exception("map country: salon countries not refreshed")
        return
    if written:
        logger.info("map country: %s salon countries worked out", written)

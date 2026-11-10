"""
The app reviews' totals as the salon and stylist screens read them.

rating = rating_sum / review_count, rounded to one decimal
(docs/rate-your-visit-fe-contract.md, "Salon and stylist totals").
"""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

from .models import StylistRating


def rating_of(rating_sum, review_count):
    """rating_sum / review_count to one decimal (4.25 is 4.3), or None with no reviews."""
    if not review_count:
        return None
    exact = Decimal(rating_sum) / Decimal(review_count)
    return float(exact.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def stylist_ratings(stylist_ids):
    """
    stylist id -> (rating, review_count), for the stylists who have app
    reviews. Empty while RATE_VISIT_V1 is off, so every screen stays as it was.
    """
    if not settings.RATE_VISIT_V1 or not stylist_ids:
        return {}
    rows = StylistRating.objects.filter(stylist_id__in=stylist_ids, review_count__gt=0)
    return {row.stylist_id: (rating_of(row.rating_sum, row.review_count), row.review_count) for row in rows}

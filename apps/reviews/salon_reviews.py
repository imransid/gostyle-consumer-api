"""
Salon Ratings & Reviews (docs/salon-reviews-fe-contract.md): the reviews of
one salon, a page at a time, and the Helpful marks.

A salon's review is the Review row from Rate your visit: its salon_stars, its
comment, its date. The summary card's numbers are the salon profile's own
(selectors.salon_rating_fields, read through salon_profile), so the two can
never disagree.
"""

import logging
from datetime import UTC
from urllib.parse import urlencode

from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import BooleanField, Count, Exists, ExpressionWrapper, F, Min, OuterRef, Q
from rest_framework.exceptions import ErrorDetail, NotFound, PermissionDenied, ValidationError

from apps.booking_data.models import BookingItem
from apps.platform_data.models import StorefrontReview

from . import visits
from .models import Review, ReviewHelpful, ReviewPhoto
from .tags import SALON_LABELS, with_labels

logger = logging.getLogger(__name__)

# sort -> the order (section "Order"). Every order ends on the review id, so
# a page never repeats or skips a review when two tie.
SORTS = {
    "top": (F("helpful_count").desc(), F("has_comment").desc(), F("submitted_at").desc(), "id"),
    "newest": (F("submitted_at").desc(), "id"),
    "highest": (F("salon_stars").desc(), F("submitted_at").desc(), "id"),
    "lowest": (F("salon_stars").asc(), F("submitted_at").desc(), "id"),
}
DEFAULT_SORT = "top"

INVALID_SORT = "Please choose top, newest, highest or lowest."
REVIEW_NOT_FOUND = "We couldn't find this review."
OWN_REVIEW = "You can't mark your own review as helpful."


def read_sort(raw):
    """The `sort` query, or a 422 invalid_sort. Absent (or empty) is top."""
    if raw is None or raw == "":
        return DEFAULT_SORT
    if raw not in SORTS:
        raise ValidationError({"sort": [ErrorDetail(INVALID_SORT, code="invalid_sort")]})
    return raw


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def summary(salon):
    """
    `salon` is the row salon_profile gave. rating and review_count are the
    profile's own two (SalonProfileSerializer), and distribution counts the
    same reviews they count, so the bars add up to review_count.
    """
    rating = None if salon.avg_rating is None else round(float(salon.avg_rating), 1)
    return {
        "rating": rating,
        "review_count": salon.review_count or 0,
        "distribution": distribution(salon.id),
    }


def distribution(salon_id):
    """Five entries, 5 stars down to 1, count 0 where there are none."""
    counts = dict.fromkeys(range(1, 6), 0)

    # The same reviews salon_rating_fields counts: the published storefront
    # reviews, and with RATE_VISIT_V1 the app's own.
    groups = list(published_star_counts(salon_id))
    if settings.RATE_VISIT_V1:
        groups += Review.objects.filter(salon_id=salon_id).values_list("salon_stars").annotate(n=Count("id"))
    for stars, n in groups:
        if stars in counts:
            counts[stars] += n

    return [{"stars": stars, "count": counts[stars]} for stars in range(5, 0, -1)]


def published_star_counts(salon_id):
    """(stars, count) of the salon's published storefront reviews (the platform's table)."""
    return (
        StorefrontReview.objects.filter(storefront_id=salon_id, state="PUBLISHED")
        .values_list("rating")
        .annotate(n=Count("id"))
    )


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


def reviews_of(salon_id, sort, has_photos=False):
    """Every review of the salon, in the order of `sort`. has_photos: only those with photos."""
    qs = Review.objects.filter(salon_id=salon_id).annotate(
        has_comment=ExpressionWrapper(Q(comment__isnull=False) & ~Q(comment=""), output_field=BooleanField()),
    )
    if has_photos:
        qs = qs.filter(Exists(ReviewPhoto.objects.filter(review=OuterRef("pk"))))
    return qs.order_by(*SORTS[sort])


def page_links(base_url, query, page, page_size, count):
    """
    Absolute next and previous, as GET /bookings gives them: next while there
    is a later page, previous whenever page > 1 (even past the end).
    """
    pages = -(-count // page_size)

    def at(number):
        return f"{base_url}?{urlencode({**query, 'page': number, 'pageSize': page_size})}"

    return (
        at(page + 1) if page < pages else None,
        at(page - 1) if page > 1 else None,
    )


def result_rows(reviews, account, build_url):
    """results[] for one page of reviews."""
    reviews = list(reviews)
    ids = [review.id for review in reviews]
    services = services_of([review.booking_id for review in reviews])

    marked = set()
    signed_in = account is not None and account.is_authenticated
    if signed_in and ids:
        marked = set(ReviewHelpful.objects.filter(review_id__in=ids, customer=account).values_list("review_id", flat=True))

    photos = {}
    for photo in ReviewPhoto.objects.filter(review_id__in=ids).select_related("file").order_by("review_id", "position"):
        photos.setdefault(photo.review_id, []).append({"id": str(photo.file_id), "url": build_url(photo.file.file.url)})

    return [
        {
            "id": str(review.id),
            "stars": review.salon_stars,
            "comment": review.comment,
            "submitted_at": utc_iso(review.submitted_at),
            "author": author(review.customer),
            "services": services.get(review.booking_id, []),
            "helpful_count": review.helpful_count,
            "is_helpful": review.id in marked,
            "is_mine": signed_in and review.customer_id == account.id,
            "photos": photos.get(review.id, []),
            "tags": with_labels(review.salon_tags, SALON_LABELS),
        }
        for review in reviews
    ]


def author(customer):
    """
    The name to print and the photo. Both null once the account is gone
    (purged): the app shows "Go Style member".
    """
    if customer.deleted_at is not None:
        return {"name": None, "avatar_url": None}
    return {
        "name": public_name(customer.full_name or customer.display_name),
        "avatar_url": customer.image or None,
    }


def public_name(full_name):
    """
    First name and last initial, the doc's suggestion and its example: the
    last name is everything after the first name, so "Omar Al Fahim" is
    "Omar A.". A name of one word stays as it is. Blank is None.
    """
    parts = (full_name or "").split()
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[1][0].upper()}."


def services_of(booking_ids):
    """
    booking id -> [{id, name}], the services done on each visit, in the order
    they were done (the line's start, then its position), from booking-api's
    database. Lines are never cancelled one by one (visits.py), so every line
    of a completed booking was done. Without booking data every list is
    empty: the chip hides, the screen still works.
    """
    if not booking_ids:
        return {}
    if visits.DB not in settings.DATABASES:
        logger.warning("Salon reviews without services: BOOKING_DB_NAME is not set.")
        return {}
    try:
        rows = list(
            BookingItem.objects.using(visits.DB)
            .filter(booking_id__in=booking_ids)
            .annotate(started_at=Min("staffreservation__start_at"))
            .values_list("booking_id", "service_id", "service_name", "position", "started_at", "booking__start_at")
        )
    except DatabaseError as exc:
        logger.warning("Salon reviews without services: %s", exc)
        return {}

    # A line with no reservation kept counts from the visit's start, as
    # Rate your visit's stylist list does.
    rows.sort(key=lambda row: (row[0], row[4] or row[5], row[3]))
    out = {}
    for booking_id, service_id, service_name, *_ in rows:
        out.setdefault(booking_id, []).append({"id": str(service_id), "name": service_name})
    return out


def utc_iso(moment):
    """ISO 8601 in UTC, to the second: 2026-11-12T09:41:07Z."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Helpful
# ---------------------------------------------------------------------------


def review_not_found():
    return NotFound(REVIEW_NOT_FOUND, code="review_not_found")


def _locked_review(review_ref):
    """The review, locked until the transaction ends, or a 404 review_not_found."""
    review_id = visits.as_uuid(review_ref)
    review = Review.objects.select_for_update().filter(id=review_id).first() if review_id else None
    if review is None:
        raise review_not_found()
    return review


def mark_helpful(review_ref, account):
    """PUT: marks it for this customer. Already marked: nothing changes."""
    with transaction.atomic():
        review = _locked_review(review_ref)
        if review.customer_id == account.id:
            raise PermissionDenied(OWN_REVIEW, code="own_review")
        _, created = ReviewHelpful.objects.get_or_create(review=review, customer=account)
        if created:
            Review.objects.filter(id=review.id).update(helpful_count=F("helpful_count") + 1)
        return _state(review.id, account)


def unmark_helpful(review_ref, account):
    """DELETE: takes this customer's mark back. Not marked: nothing changes."""
    with transaction.atomic():
        review = _locked_review(review_ref)
        removed, _ = ReviewHelpful.objects.filter(review=review, customer=account).delete()
        if removed:
            Review.objects.filter(id=review.id).update(helpful_count=F("helpful_count") - 1)
        return _state(review.id, account)


def _state(review_id, account):
    """The saved state, read back: never the one the app guessed."""
    return {
        "is_helpful": ReviewHelpful.objects.filter(review_id=review_id, customer=account).exists(),
        "helpful_count": Review.objects.filter(id=review_id).values_list("helpful_count", flat=True).get(),
    }


def remove_marks(account):
    """
    Deleting an account (ConsumerAccount.purge) removes its Helpful marks,
    and each review it marked counts one less. Its own reviews stay.
    """
    marks = ReviewHelpful.objects.filter(customer=account)
    Review.objects.filter(Exists(marks.filter(review=OuterRef("pk")))).update(helpful_count=F("helpful_count") - 1)
    marks.delete()

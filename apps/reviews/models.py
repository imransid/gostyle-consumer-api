"""
Rate your visit (docs/rate-your-visit-fe-contract.md, section 4).

One Review per booking, and one ReviewStylistRating per stylist on that
booking. The comment and the photos live once, on the Review.

The booking, the salon and the stylists belong to other services (the
booking to booking-api's own database, the salon and the stylist to the
platform), so their ids are plain uuid columns, not database foreign keys.
The customer is ours, so that one is a real foreign key.

SalonRating and StylistRating are the running totals the doc keeps "on the
Salon and Stylist rows". Those rows are the platform's tables, which this
service does not own, so the totals sit in two small tables of ours, one row
per salon and one per stylist, updated in the same transaction as the review.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.uploads.models import UploadedFile


class Review(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Unique: this is what makes it one review per booking, double taps too.
    booking_id = models.UUIDField(unique=True)
    # PROTECT: a review is final and counted in the totals. Deleting an
    # account never deletes the row (ConsumerAccount.purge only clears it).
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reviews",
    )
    # The salon id the app uses (the platform's storefront id).
    salon_id = models.UUIDField(db_index=True)
    # Kept for per-branch ratings later. Nothing reads them yet.
    branch_id = models.UUIDField(null=True, blank=True)
    tenant_id = models.UUIDField(null=True, blank=True)
    salon_stars = models.PositiveSmallIntegerField()
    # Tag codes, in the order sent.
    salon_tags = models.JSONField(default=list, blank=True)
    comment = models.TextField(null=True, blank=True)
    submitted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "review"
        constraints = [
            models.CheckConstraint(
                condition=Q(salon_stars__gte=1, salon_stars__lte=5),
                name="review_salon_stars_1_to_5",
            ),
        ]

    def __str__(self):
        return f"{self.booking_id}: {self.salon_stars} stars"


class ReviewStylistRating(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name="stylist_ratings")
    # The platform's staff_profile id, the stylist id the app uses.
    stylist_id = models.UUIDField(db_index=True)
    # The same values as the review's own two, kept for later.
    branch_id = models.UUIDField(null=True, blank=True)
    tenant_id = models.UUIDField(null=True, blank=True)
    stars = models.PositiveSmallIntegerField()
    # Tag codes, in the order sent. Always empty when defaulted.
    tags = models.JSONField(default=list, blank=True)
    # True when the customer skipped this stylist and the row got the salon's stars.
    defaulted = models.BooleanField()

    class Meta:
        db_table = "review_stylist_rating"
        constraints = [
            models.UniqueConstraint(
                fields=["review", "stylist_id"],
                name="review_stylist_rating_once_per_review",
            ),
            models.CheckConstraint(
                condition=Q(stars__gte=1, stars__lte=5),
                name="review_stylist_stars_1_to_5",
            ),
        ]

    def __str__(self):
        return f"{self.stylist_id}: {self.stars} stars"


class ReviewPhoto(models.Model):
    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name="photos")
    # One to one: a file can be on one review only.
    file = models.OneToOneField(
        UploadedFile,
        on_delete=models.PROTECT,
        related_name="review_photo",
    )
    # The order the customer sent them in, from 0.
    position = models.PositiveSmallIntegerField()

    class Meta:
        db_table = "review_photo"
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["review", "position"],
                name="review_photo_position_once",
            ),
        ]


class SalonRating(models.Model):
    """The app reviews' totals for one salon. rating = rating_sum / review_count."""

    salon_id = models.UUIDField(primary_key=True)
    rating_sum = models.PositiveIntegerField(default=0)
    review_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "salon_rating"

    def __str__(self):
        return f"{self.salon_id}: {self.rating_sum}/{self.review_count}"


class StylistRating(models.Model):
    """The app reviews' totals for one stylist, defaulted ratings included."""

    stylist_id = models.UUIDField(primary_key=True)
    rating_sum = models.PositiveIntegerField(default=0)
    review_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "stylist_rating"

    def __str__(self):
        return f"{self.stylist_id}: {self.rating_sum}/{self.review_count}"

from django.urls import path

from .salon_views import ReviewHelpfulView, SalonReviewsView
from .views import BookingReviewView

urlpatterns = [
    # <str:>, not <uuid:>: a bad id must get our JSON 404 (booking_not_found),
    # not Django's HTML one, so the view checks the id itself.
    path("booking/<str:booking_id>/review", BookingReviewView.as_view(), name="booking-review"),
    # <uuid:>, as GET /salon/<id>: the same 404 for a bad id.
    path("salon/<uuid:salon_id>/reviews", SalonReviewsView.as_view(), name="salon-reviews"),
    # <str:>: a bad id is our JSON 404 (review_not_found).
    path("review/<str:review_id>/helpful", ReviewHelpfulView.as_view(), name="review-helpful"),
]

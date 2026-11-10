from django.urls import path

from .views import BookingReviewView

urlpatterns = [
    # <str:>, not <uuid:>: a bad id must get our JSON 404 (booking_not_found),
    # not Django's HTML one, so the view checks the id itself.
    path("booking/<str:booking_id>/review", BookingReviewView.as_view(), name="booking-review"),
]

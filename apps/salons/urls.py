from django.urls import path

from . import (
    check_in_views,
    expert_profile_views,
    group_views,
    routine_views,
    series_views,
    service_detail_views,
    single_views,
)
from .views import (
    BookingCreateView,
    BookingListView,
    DiscoverMapView,
    NearestAvailableView,
    DiscoverStoryListView,
    FavouriteListView,
    SalonDiscoveryDetailView,
    SalonDiscoveryListView,
    SalonListView,
    SalonPackagesView,
    SalonProductsView,
    SalonProfileView,
    SalonServicesView,
    ServiceDetailsView,
    SalonStoriesView,
    SalonStylistsView,
    StylistListView,
    ServiceListView,
    BookingDetailView,
    ProductDetailView,
)

urlpatterns = [
    path("products/<uuid:product_id>", ProductDetailView.as_view(), name="product-detail"), 
    path("salons/", SalonListView.as_view()),
    path("discover", SalonDiscoveryListView.as_view(), name="salon-discover"),
    path("discover/map", DiscoverMapView.as_view(), name="salon-discover-map"),
    path("discover/<uuid:pk>", SalonDiscoveryDetailView.as_view(), name="salon-discover-detail"),
    path("salon/<uuid:salon_id>", SalonProfileView.as_view(), name="salon-profile"),
    path("salon/<uuid:salon_id>/services", SalonServicesView.as_view(), name="salon-services"),
    # One service, for its detail screen. <str:>, not <uuid:>: a bad id must get
    # our JSON 404, not Django's HTML one, so the view checks both ids itself.
    path(
        "salon/<str:salon_id>/service/<str:service_id>",
        service_detail_views.SalonServiceDetailView.as_view(),
        name="salon-service-detail",
    ),
    path("salon/<uuid:salon_id>/stylists", SalonStylistsView.as_view(), name="salon-stylists"),
    # One stylist, for the expert profile screen. <str:>, like the service
    # detail: a bad id must get our JSON 404, so the view checks both ids.
    path(
        "salon/<str:salon_id>/stylist/<str:stylist_id>",
        expert_profile_views.SalonExpertProfileView.as_view(),
        name="salon-expert-profile",
    ),
    path("booking", BookingCreateView.as_view(), name="booking-create"),
    path("bookings", BookingListView.as_view(), name="booking-list"),
    path(
        "booking/nearest-available/<uuid:salon_id>",
        NearestAvailableView.as_view(),
        name="booking-nearest-available",
    ),
    path("stylists", StylistListView.as_view(), name="stylists"),
    path("salon/<uuid:salon_id>/packages", SalonPackagesView.as_view(), name="salon-packages"),
    path("salon/<uuid:salon_id>/products", SalonProductsView.as_view(), name="salon-products"),
    path("salon/<uuid:salon_id>/stories", SalonStoriesView.as_view(), name="salon-stories"),
    path("discover/story", DiscoverStoryListView.as_view(), name="discover-story"),
    path("favourite", FavouriteListView.as_view(), name="favourite-list"),
    path("services", ServiceListView.as_view(), name="services"),
    path("services-details", ServiceDetailsView.as_view(), name="services-details"),
    path("booking/<uuid:booking_id>", BookingDetailView.as_view(), name="booking-detail"),
    path("booking/group-availability", group_views.GroupAvailabilityView.as_view(), name="booking-group-availability"),
    path("booking/group", group_views.GroupBookingCreateView.as_view(), name="booking-group"),
    path("booking/<uuid:booking_id>/cancel", group_views.GroupBookingCancelView.as_view(), name="booking-cancel"),
    # A single booking's move (SINGLE_BOOKING_ACTIONS_V1). Off, it is the 404
    # this path was before.
    path(
        "booking/<uuid:booking_id>/reschedule",
        single_views.SingleBookingRescheduleView.as_view(),
        name="booking-reschedule",
    ),
    # Self check-in (SELF_CHECK_IN_V1): "I am here", and the desk's answer.
    # Off, it is the 404 this path was before.
    path(
        "booking/<uuid:booking_id>/check-in",
        check_in_views.SelfCheckInView.as_view(),
        name="booking-check-in",
    ),
    # Routines (SERIES_BOOKING_V1). `series` is not a uuid, so booking/<uuid> cannot take it.
    path("booking/series", series_views.SeriesBookingCreateView.as_view(), name="booking-series"),
    path("booking/series/<uuid:series_id>", series_views.SeriesBookingDetailView.as_view(), name="booking-series-detail"),
    path("booking/series/<uuid:series_id>/cancel", series_views.SeriesBookingCancelView.as_view(), name="booking-series-cancel"),
    # The app team's routine contract (ROUTINE_CONTRACT_V1). Neither `routine` nor
    # `routine-preview` is a uuid, so booking/<uuid> cannot take them.
    path("booking/routine-preview", routine_views.RoutinePreviewView.as_view(), name="booking-routine-preview"),
    path("booking/routine", routine_views.RoutineCreateView.as_view(), name="booking-routine"),
    path(
        "booking/<uuid:booking_id>/sessions/<uuid:session_id>",
        routine_views.RoutineSessionMoveView.as_view(),
        name="booking-routine-session",
    ),
]
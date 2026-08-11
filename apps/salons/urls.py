from django.urls import path

from .views import (
    DiscoverMapView,
    SalonDiscoveryDetailView,
    SalonDiscoveryListView,
    SalonListView,
    SalonProfileView,
    SalonServicesView,
    SalonStylistsView,
)

urlpatterns = [
    path("salons/", SalonListView.as_view()),
    path("discover", SalonDiscoveryListView.as_view(), name="salon-discover"),
    path("discover/map", DiscoverMapView.as_view(), name="salon-discover-map"),
    path("discover/<uuid:pk>", SalonDiscoveryDetailView.as_view(), name="salon-discover-detail"),
    path("salon/<uuid:salon_id>", SalonProfileView.as_view(), name="salon-profile"),
    path("salon/<uuid:salon_id>/services", SalonServicesView.as_view(), name="salon-services"),
    path("salon/<uuid:salon_id>/stylists", SalonStylistsView.as_view(), name="salon-stylists"),
]
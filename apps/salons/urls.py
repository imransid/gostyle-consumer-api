from django.urls import path
from .views import SalonListView
from .views import SalonDiscoveryListView, SalonDiscoveryDetailView, DiscoverMapView

urlpatterns = [
    path("salons/", SalonListView.as_view()),
    path("discover", SalonDiscoveryListView.as_view(), name="salon-discover"),
    path("discover/map", DiscoverMapView.as_view(), name="salon-discover-map"),
    path("discover/<uuid:pk>", SalonDiscoveryDetailView.as_view(), name="salon-discover-detail")
]
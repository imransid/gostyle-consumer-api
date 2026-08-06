from django.urls import path
from .views import SalonListView
from .views import SalonDiscoveryListView, SalonDiscoveryDetailView

urlpatterns = [
    path("salons/", SalonListView.as_view()),
    path("discover", SalonDiscoveryListView.as_view(), name="salon-discover"),
    path("discover/<uuid:pk>", SalonDiscoveryDetailView.as_view(), name="salon-discover-detail")
]
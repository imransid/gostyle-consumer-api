from django.urls import path

from .views import GeofencesView, LocationView

urlpatterns = [
    path("me/location", LocationView.as_view(), name="me-location"),
    path("me/geofences", GeofencesView.as_view(), name="me-geofences"),
]

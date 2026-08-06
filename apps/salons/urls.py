from django.urls import path
from .views import SalonListView

urlpatterns = [
    path("salons/", SalonListView.as_view()),
]
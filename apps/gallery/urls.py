from django.urls import path

from .views import GalleryItemView, GalleryView

urlpatterns = [
    path("me/gallery", GalleryView.as_view(), name="gallery"),
    # <str:>, not <uuid:>: a bad id must get our JSON 404
    # (gallery_item_not_found), not Django's HTML one.
    path("me/gallery/<str:item_id>", GalleryItemView.as_view(), name="gallery-item"),
]

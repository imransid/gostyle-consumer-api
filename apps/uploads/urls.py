from django.urls import path

from .views import UploadFilesView

urlpatterns = [
    path("upload-files", UploadFilesView.as_view(), name="upload-files"),
]
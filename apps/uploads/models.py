import uuid

from django.conf import settings
from django.db import models


class FileType(models.TextChoices):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    PDF = "PDF"
    DOCUMENT = "DOCUMENT"


class UploadedFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.FileField(upload_to="uploads/%Y/%m/")
    file_type = models.CharField(max_length=20, choices=FileType.choices)
    # What the bytes really are (from the file type check), not what the
    # client said. Kept for support and cleanup; the app only sees file_type.
    mime_type = models.CharField(max_length=100, default="")
    size = models.PositiveBigIntegerField(default=0)
    original_name = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.original_name
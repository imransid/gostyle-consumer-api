"""
My Gallery & Moments (docs/my-gallery-fe-contract.md, section 4).

A GalleryItem is one reference: a photo the customer added to Haircut or
Beard. Moments are not stored here. They are the photos on the customer's own
reviews (apps.reviews ReviewPhoto), read where they are: the doc's suggestion
in section 7.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.uploads.models import UploadedFile


class GalleryItem(models.Model):
    # The gallery item's own id, the one DELETE takes. Not the upload's id.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="gallery_items",
    )
    # The photo from POST /upload-files. PROTECT: the photo is deleted only
    # after its gallery row (service.delete_items).
    file = models.ForeignKey(
        UploadedFile,
        on_delete=models.PROTECT,
        related_name="gallery_items",
    )
    # A category code: haircut or beard.
    category = models.CharField(max_length=20)
    # The photo's size as it is shown (its EXIF turn applied), read once when
    # it is added. Both or neither: null when the photo could not be read.
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    # The grid's copy, 480 px wide. Empty when the photo is not wider than
    # that, or could not be read.
    thumbnail = models.FileField(upload_to="gallery/", max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "gallery_item"
        constraints = [
            # A file once per gallery: a double tap or a retry is a 409.
            models.UniqueConstraint(fields=["customer", "file"], name="gallery_item_file_once"),
        ]
        indexes = [
            models.Index(fields=["customer", "-created_at"], name="gallery_item_newest"),
        ]

    def __str__(self):
        return f"{self.customer_id}: {self.category}"

import logging

from botocore.exceptions import BotoCoreError, ClientError
from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import APIException

from .models import UploadedFile

logger = logging.getLogger(__name__)


class StorageUnavailable(APIException):
    """503 when the file store (S3) fails. Not the customer's fault."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "File upload is temporarily unavailable. Please try again."
    default_code = "storage_unavailable"


def save_uploads(checked, user):
    """Store each checked file and its row. All rows, or none.

    If S3 fails on the third file, the rows for the first two are rolled
    back, so the table never lists a file the app was not told about. Their
    objects stay in the bucket as orphans; that is the cheaper mistake.
    """
    rows = []
    try:
        with transaction.atomic():
            for item in checked:
                upload = item["upload"]
                # django-storages sends upload.content_type to S3 as the
                # object's Content-Type. The client set it, so replace it
                # with what the bytes really are.
                upload.content_type = item["mime"]

                row = UploadedFile(
                    file_type=item["file_type"],
                    mime_type=item["mime"],
                    size=upload.size,
                    original_name=upload.name[:255],
                    uploaded_by=user,
                )
                # Our own name, never the client's: "<row id>.<ext>".
                row.file.save(f"{row.id}.{item['extension']}", upload)
                rows.append(row)
    except (BotoCoreError, ClientError) as exc:
        logger.exception("File upload to storage failed")
        raise StorageUnavailable() from exc
    return rows
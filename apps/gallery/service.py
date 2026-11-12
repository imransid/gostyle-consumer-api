"""
My Gallery & Moments (docs/my-gallery-fe-contract.md): the page, adding a
reference, removing one, and deleting a whole gallery with its account.

Two kinds of item, in one list:
- references: GalleryItem rows, Haircut and Beard, added and removed here;
- moments: the photos on the customer's own reviews (Rate your visit),
  can_add false and can_delete false, as the doc suggests (section 7).
"""

import logging
import uuid
from datetime import timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import CharField, Count, F, TextField, Value
from django.db.models.functions import Cast
from rest_framework import status
from rest_framework.exceptions import APIException, ErrorDetail, NotFound, ValidationError

from apps.reviews.models import ReviewPhoto
from apps.uploads.models import FileType, UploadedFile

from . import images
from .models import GalleryItem

logger = logging.getLogger(__name__)

MOMENT = "moment"

# The chips after the app's own All, in the order they are drawn. code is
# stable; label is display text only.
CATEGORIES = (
    {"code": "haircut", "label": "Haircut", "can_add": True},
    {"code": "beard", "label": "Beard", "can_add": True},
    {"code": MOMENT, "label": "Moments", "can_add": False},
)
CODES = tuple(category["code"] for category in CATEGORIES)
# The categories a customer adds to: their items are the references.
REFERENCES = tuple(category["code"] for category in CATEGORIES if category["can_add"])

# The doc's suggestion. Moments do not count.
MAX_REFERENCES = 100

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

# A moment has no gallery row, so its id is made from its photo's id: the
# same every time, never the upload's own id, and a uuid like the others.
MOMENT_NAMESPACE = uuid.UUID("25b16940-11f6-4e19-b81f-1bf7f9c4c311")

# What the customer reads. The app shows `message` as it is.
MESSAGES = {
    "invalid_category": "This category doesn't exist.",
    "cannot_add_here": "Photos can't be added to this category.",
    "invalid_file": "This photo can't be used. Please choose another one.",
    "already_in_gallery": "This photo is already in your gallery.",
    "gallery_full": f"You can keep up to {MAX_REFERENCES} photos. Remove one to add another.",
    "gallery_item_not_found": "We couldn't find this photo.",
    "not_deletable": "This photo can't be removed.",
}


class GalleryConflict(APIException):
    """409: already_in_gallery (field file_id), gallery_full, not_deletable (field null)."""

    status_code = status.HTTP_409_CONFLICT

    def __init__(self, code, field=None):
        message = ErrorDetail(MESSAGES[code], code=code)
        super().__init__({field: [message]} if field else message, code)


def invalid_category():
    return ValidationError({"category": [ErrorDetail(MESSAGES["invalid_category"], code="invalid_category")]})


def item_not_found():
    return NotFound(MESSAGES["gallery_item_not_found"], code="gallery_item_not_found")


# ---------------------------------------------------------------------------
# GET /me/gallery
# ---------------------------------------------------------------------------


def gallery_page(account, category, page, page_size, build_url, link):
    """
    One page, newest first, with every category's chip and the limit.
    `category` None is All. `link(page)` is a page's full URL.
    """
    counts = category_counts(account)
    count = sum(counts.values()) if category is None else counts[category]

    start = (page - 1) * page_size
    rows = list(_ordered_rows(account, category)[start : start + page_size])

    pages = -(-count // page_size)
    return {
        "count": count,
        "next": link(page + 1) if page < pages else None,
        "previous": link(page - 1) if page > 1 else None,
        "results": _items(rows, build_url),
        "categories": [
            {
                "code": chip["code"],
                "label": chip["label"],
                "count": counts[chip["code"]],
                "can_add": chip["can_add"],
            }
            for chip in CATEGORIES
        ],
        "limits": {"max_references": MAX_REFERENCES},
    }


def category_counts(account):
    counts = dict.fromkeys(CODES, 0)
    by_category = (
        GalleryItem.objects.filter(customer=account).values("category").annotate(n=Count("id")).order_by()
    )
    for row in by_category:
        if row["category"] in counts:
            counts[row["category"]] = row["n"]
    counts[MOMENT] = _moments(account).count()
    return counts


def _moments(account):
    return ReviewPhoto.objects.filter(review__customer=account)


def _ordered_rows(account, category):
    """
    (kind, key, at) for every item asked for, newest first. References and
    moments in one order: a union, so the page is cut by the database.
    The key breaks ties, so a page never repeats or skips an item.
    """
    parts = []
    if category is None or category in REFERENCES:
        references = GalleryItem.objects.filter(customer=account)
        if category is not None:
            references = references.filter(category=category)
        parts.append(_rows(references, "reference", "id", "created_at"))
    if category is None or category == MOMENT:
        parts.append(_rows(_moments(account), MOMENT, "file_id", "review__submitted_at"))

    rows = parts[0] if len(parts) == 1 else parts[0].union(parts[1], all=True)
    return rows.order_by("-at", "-key")


def _rows(queryset, kind, key, at):
    # The same three columns, in the same order, on both sides of the union.
    return queryset.annotate(
        kind=Value(kind, output_field=CharField()),
        key=Cast(key, output_field=TextField()),
        at=F(at),
    ).values_list("kind", "key", "at")


def _items(rows, build_url):
    reference_ids = [uuid.UUID(key) for kind, key, _ in rows if kind == "reference"]
    moment_file_ids = [uuid.UUID(key) for kind, key, _ in rows if kind == MOMENT]

    references = GalleryItem.objects.select_related("file").in_bulk(reference_ids)
    moments = {
        photo.file_id: photo
        for photo in ReviewPhoto.objects.filter(file_id__in=moment_file_ids).select_related("file", "review")
    }

    items = []
    for kind, key, _ in rows:
        if kind == "reference":
            item = references.get(uuid.UUID(key))
            if item is not None:
                items.append(item_object(item, build_url))
        else:
            photo = moments.get(uuid.UUID(key))
            if photo is not None:
                items.append(_moment_object(photo, build_url))
    return items


def item_object(item, build_url):
    """One row of results[], and POST's answer."""
    return {
        "id": str(item.id),
        "category": item.category,
        "image_url": build_url(item.file.file.url),
        "thumbnail_url": build_url(item.thumbnail.url) if item.thumbnail else None,
        "width": item.width,
        "height": item.height,
        "can_delete": True,
        "created_at": _utc(item.created_at),
    }


def _moment_object(photo, build_url):
    # The photo was never added here, so its size was never read.
    return {
        "id": str(moment_id(photo.file_id)),
        "category": MOMENT,
        "image_url": build_url(photo.file.file.url),
        "thumbnail_url": None,
        "width": None,
        "height": None,
        "can_delete": False,
        "created_at": _utc(photo.review.submitted_at),
    }


def moment_id(file_id):
    return uuid.uuid5(MOMENT_NAMESPACE, str(file_id))


def _utc(moment):
    """ISO 8601 in UTC, as in the doc: 2026-11-12T09:41:07Z."""
    return moment.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# POST /me/gallery
# ---------------------------------------------------------------------------


def add_reference(account, body):
    """
    Adds an uploaded photo as a reference and returns its GalleryItem.
    422 invalid_file / invalid_category, 409 already_in_gallery / gallery_full,
    with nothing saved. Then its size and grid copy are read and kept.
    """
    upload, category = _check(body, account)

    try:
        with transaction.atomic():
            # One add at a time per customer, so two at once can't pass the
            # limit together.
            list(get_user_model().objects.select_for_update().filter(pk=account.pk).values_list("pk"))
            in_gallery = (
                GalleryItem.objects.filter(customer=account, file=upload).exists()
                or _moments(account).filter(file=upload).exists()
            )
            if in_gallery:
                raise GalleryConflict("already_in_gallery", field="file_id")
            if GalleryItem.objects.filter(customer=account).count() >= MAX_REFERENCES:
                raise GalleryConflict("gallery_full")
            item = GalleryItem.objects.create(customer=account, file=upload, category=category)
    except IntegrityError:
        # The unique (customer, file): the same file got there first.
        raise GalleryConflict("already_in_gallery", field="file_id") from None

    _read_photo(item)
    return item


def _check(body, account):
    if not isinstance(body, dict):
        body = {}
    errors = {}

    upload = _usable_upload(body.get("file_id"), account)
    if upload is None:
        errors["file_id"] = [ErrorDetail(MESSAGES["invalid_file"], code="invalid_file")]

    category = body.get("category")
    if not isinstance(category, str) or category not in REFERENCES:
        message = MESSAGES["cannot_add_here"] if category in CODES else MESSAGES["invalid_category"]
        errors["category"] = [ErrorDetail(message, code="invalid_category")]

    if errors:
        raise ValidationError(errors)
    return upload, category


def _usable_upload(value, account):
    """The customer's own uploaded image with this id, or None."""
    file_id = _as_uuid(value)
    if file_id is None:
        return None
    return UploadedFile.objects.filter(id=file_id, uploaded_by=account, file_type=FileType.IMAGE).first()


def _read_photo(item):
    """The shown size and the grid copy, kept on the row. Nulls if the photo can't be read."""
    width, height, thumbnail = images.read(item.file)
    if width is None:
        return
    item.width, item.height = width, height
    if thumbnail is not None:
        try:
            item.thumbnail.save(f"{item.id}-{images.THUMBNAIL_WIDTH}.jpg", ContentFile(thumbnail), save=False)
        except Exception:
            logger.warning("My gallery: grid copy of %s not saved", item.id, exc_info=True)
            item.thumbnail = ""
    GalleryItem.objects.filter(pk=item.pk).update(
        width=item.width,
        height=item.height,
        thumbnail=item.thumbnail.name or "",
    )


# ---------------------------------------------------------------------------
# DELETE /me/gallery/{id}
# ---------------------------------------------------------------------------


def remove(account, item_id):
    """
    Removes one of the customer's references. 404 gallery_item_not_found for
    no such item and for someone else's (never says which), 409
    not_deletable for a moment.
    """
    parsed = _as_uuid(item_id)
    item = None
    if parsed is not None:
        item = GalleryItem.objects.filter(pk=parsed, customer=account).first()
    if item is None:
        if parsed is not None and parsed in _moment_ids(account):
            raise GalleryConflict("not_deletable")
        raise item_not_found()
    delete_items([item])


def _moment_ids(account):
    return {moment_id(file_id) for file_id in _moments(account).values_list("file_id", flat=True)}


def delete_gallery(account):
    """Account deletion: the whole gallery and its files. Moments stay with their reviews."""
    delete_items(list(GalleryItem.objects.filter(customer=account)))


def delete_items(items):
    """
    The rows, their grid copies and their photos: the photo's row and its
    file in storage too, unless a review also shows it (a review is kept).
    Storage is cleaned once the database has committed, and a failure there
    is only logged.
    """
    if not items:
        return
    with transaction.atomic():
        GalleryItem.objects.filter(pk__in=[item.pk for item in items]).delete()
        photos = UploadedFile.objects.filter(
            pk__in=[item.file_id for item in items],
            review_photo__isnull=True,
            gallery_items__isnull=True,
        )
        names = [item.thumbnail.name for item in items if item.thumbnail]
        names += [photo.file.name for photo in photos]
        photos.delete()
        transaction.on_commit(lambda: _delete_from_storage(names))


def _delete_from_storage(names):
    for name in names:
        try:
            default_storage.delete(name)
        except Exception:
            logger.warning("My gallery: %s not deleted from storage", name, exc_info=True)


def _as_uuid(value):
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None

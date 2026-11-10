"""
Rate your visit (docs/rate-your-visit-fe-contract.md): the form the screen draws,
the checks on a review, saving it, and the review object.
"""

from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import F
from rest_framework import status
from rest_framework.exceptions import APIException, ErrorDetail, NotFound, ValidationError

from apps.salons import timezones
from apps.salons.selectors import salon_cards_for_refs, salon_category, stylist_cards
from apps.uploads.models import FileType, UploadedFile

from . import visits
from .models import Review, ReviewPhoto, ReviewStylistRating, SalonRating, StylistRating
from .tags import SALON_LABELS, STYLIST_LABELS, tags_for, with_labels

MAX_PHOTOS = 3
COMMENT_MAX_LENGTH = 500

# What the customer reads. The app shows `message` as it is.
BOOKING_NOT_FOUND = "We couldn't find this booking."
CONFLICTS = {
    "already_reviewed": "You've already rated this visit. Reviews can't be changed.",
    "not_completed": "You can rate this visit once it's done.",
    "window_closed": "This visit can no longer be rated.",
}


class ReviewConflict(APIException):
    """409: this booking cannot take a review (already_reviewed, not_completed, window_closed)."""

    status_code = status.HTTP_409_CONFLICT

    def __init__(self, code):
        super().__init__(CONFLICTS[code], code)


def booking_not_found():
    return NotFound(BOOKING_NOT_FOUND, code="booking_not_found")


@dataclass(frozen=True)
class Screen:
    """Everything about one visit the form, the checks and the save need."""

    visit: visits.Visit
    salon: dict
    stylists: list
    tags: dict

    @property
    def tz(self):
        return timezones.resolve(self.salon.get("timezone"), self.salon["id"])


def open_screen(booking_ref, account):
    """The caller's visit for this id, ready for the screen, or None (a 404)."""
    visit = visits.find_visit(booking_ref, account.id)
    if visit is None:
        return None
    ref = str(visit.branch_id)
    salon = salon_cards_for_refs([ref]).get(ref)
    if salon is None:
        # The salon is gone from the app: there is nothing to rate it as.
        return None
    cards = stylist_cards({line.staff_id for line in visit.lines})
    return Screen(
        visit=visit,
        salon=salon,
        stylists=visit_stylists(visit, cards),
        tags=tags_for(salon_category(salon["id"])),
    )


def visit_stylists(visit, cards):
    """
    The stylists who worked on the visit, as `stylists[]`: one entry per
    stylist with every line they did, in the order their first line started.
    A line whose stylist is not known here is left out (it still counts in
    service_count). `cards` is selectors.stylist_cards for the visit.
    """
    stylists = {}
    for line in sorted(visit.lines, key=lambda line: (line.started_at or visit.start_at, line.position)):
        card = cards.get(line.staff_id)
        if card is None:
            continue
        stylist = stylists.setdefault(
            line.staff_id,
            {"id": str(line.staff_id), "name": card["name"], "avatar_url": card["avatar_url"], "services": []},
        )
        stylist["services"].append({"id": line.service_id, "name": line.service_name})
    return list(stylists.values())


def existing_review(screen):
    return Review.objects.filter(booking_id=screen.visit.booking_id).first()


def rate_state(screen, review, now):
    visit = screen.visit
    return visits.rate_state(
        status=visit.status,
        finished_at=visit.finished_at,
        reviewed=review is not None,
        now=now,
    )


def review_form(screen, review, now, build_url):
    """GET /booking/:id/review."""
    visit = screen.visit
    can_rate, reason = rate_state(screen, review, now)
    return {
        "booking": {
            "id": str(visit.booking_id),
            "status": visit.app_status,
            "completed_at": _iso(visit.finished_at, screen.tz),
            "service_count": len(visit.lines),
        },
        "salon": {
            "id": screen.salon["id"],
            "name": screen.salon["name"],
            "logo_url": screen.salon["logo_url"],
        },
        "stylists": screen.stylists,
        "tags": screen.tags,
        "limits": {"max_photos": MAX_PHOTOS, "comment_max_length": COMMENT_MAX_LENGTH},
        "can_rate": can_rate,
        "reason": reason,
        "review": review_object(review, screen, build_url) if review else None,
    }


# ---------------------------------------------------------------------------
# The checks on POST /booking/:id/review
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Submission:
    salon_stars: int
    salon_tags: list
    # stylist id (as in stylists[].id) -> (stars, tags), only the ones the customer rated
    stylists: dict
    comment: str | None
    photos: list


class _Errors:
    """Field errors in the order found, raised together as one 422."""

    def __init__(self):
        self.by_field = {}

    def add(self, field, code, message):
        self.by_field.setdefault(field, []).append(ErrorDetail(message, code=code))

    def raise_any(self):
        if self.by_field:
            raise ValidationError(self.by_field)


def check_review(body, screen, account):
    """The request body, checked for this visit. Raises a 422 with every problem found."""
    errors = _Errors()
    body = body if isinstance(body, dict) else {}

    salon = body.get("salon")
    salon_stars, salon_tags = None, []
    if not isinstance(salon, dict):
        errors.add("salon", "salon_required", "Please rate the salon.")
    else:
        if salon.get("stars") is None:
            errors.add("salon.stars", "salon_required", "Please rate the salon.")
        else:
            salon_stars = _stars(salon["stars"], "salon.stars", errors)
        salon_tags = _tags(salon.get("tags"), "salon.tags", screen.tags["salon"], errors)

    submission = Submission(
        salon_stars=salon_stars,
        salon_tags=salon_tags,
        stylists=_stylists(body.get("stylists"), screen, errors),
        comment=_comment(body.get("comment"), errors),
        photos=_photos(body.get("photo_ids"), account, errors),
    )
    errors.raise_any()
    return submission


def _stars(value, field, errors):
    # bool is an int in Python, and true is not a star rating.
    if type(value) is not int or not 1 <= value <= 5:
        errors.add(field, "invalid_stars", "Please choose 1 to 5 stars.")
        return None
    return value


def _tags(value, field, offered, errors):
    if value is None:
        return []
    if not isinstance(value, list):
        errors.add(field, "unknown_tag", "Please pick the tags from the list.")
        return []
    allowed = {tag["code"] for tag in offered}
    codes = []
    for i, code in enumerate(value):
        if not isinstance(code, str) or code not in allowed:
            errors.add(f"{field}[{i}]", "unknown_tag", "This tag can't be picked here.")
        elif code in codes:
            errors.add(f"{field}[{i}]", "unknown_tag", "You picked this tag twice.")
        else:
            codes.append(code)
    return codes


def _stylists(value, screen, errors):
    if value is None:
        return {}
    if not isinstance(value, list):
        errors.add("stylists", "stylist_not_on_booking", "This stylist didn't work on this visit.")
        return {}
    on_booking = {stylist["id"] for stylist in screen.stylists}
    rated = {}
    for i, item in enumerate(value):
        item = item if isinstance(item, dict) else {}
        stylist_id = _id_text(item.get("stylist_id"))
        if stylist_id not in on_booking:
            errors.add(f"stylists[{i}].stylist_id", "stylist_not_on_booking", "This stylist didn't work on this visit.")
            continue
        if stylist_id in rated:
            errors.add(f"stylists[{i}].stylist_id", "duplicate_stylist", "You rated this stylist twice.")
            continue
        stars = _stars(item.get("stars"), f"stylists[{i}].stars", errors)
        tags = _tags(item.get("tags"), f"stylists[{i}].tags", screen.tags["stylist"], errors)
        rated[stylist_id] = (stars, tags)
    return rated


def _comment(value, errors):
    if value is None:
        return None
    if not isinstance(value, str):
        errors.add("comment", "invalid", "Your comment must be text.")
        return None
    value = value.strip()
    if len(value) > COMMENT_MAX_LENGTH:
        errors.add("comment", "comment_too_long", f"Your comment can be up to {COMMENT_MAX_LENGTH} characters.")
        return None
    return value or None


def _photos(value, account, errors):
    if value is None:
        return []
    if not isinstance(value, list):
        errors.add("photo_ids", "invalid_photo", "This photo can't be used. Please add it again.")
        return []
    if len(value) > MAX_PHOTOS:
        errors.add("photo_ids", "too_many_photos", f"You can add up to {MAX_PHOTOS} photos.")
        return []

    ids = [visits.as_uuid(photo_id) if isinstance(photo_id, str) else None for photo_id in value]
    # Uploaded by this customer, an image, and not on another review yet.
    usable = UploadedFile.objects.filter(
        id__in=[photo_id for photo_id in ids if photo_id],
        uploaded_by=account,
        file_type=FileType.IMAGE,
        review_photo__isnull=True,
    ).in_bulk()

    photos = []
    for i, photo_id in enumerate(ids):
        photo = usable.get(photo_id)
        if photo is None or photo in photos:
            errors.add(f"photo_ids[{i}]", "invalid_photo", "This photo can't be used. Please add it again.")
        else:
            photos.append(photo)
    return photos


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def save_review(screen, submission, account):
    """
    The review, its stylist ratings (given and defaulted), its photos and the
    salon's and stylists' totals, in one transaction: all of it or none.

    Every stylist of the visit gets a row. One the customer skipped gets the
    salon's stars, no tags, and `defaulted`.
    """
    visit = screen.visit
    try:
        with transaction.atomic():
            review = Review.objects.create(
                booking_id=visit.booking_id,
                customer=account,
                salon_id=screen.salon["id"],
                branch_id=visit.branch_id,
                tenant_id=visit.tenant_id,
                salon_stars=submission.salon_stars,
                salon_tags=submission.salon_tags,
                comment=submission.comment,
            )

            ratings = []
            for stylist in screen.stylists:
                given = submission.stylists.get(stylist["id"])
                stars, tags = given or (submission.salon_stars, [])
                ratings.append(
                    ReviewStylistRating(
                        review=review,
                        stylist_id=stylist["id"],
                        branch_id=review.branch_id,
                        tenant_id=review.tenant_id,
                        stars=stars,
                        tags=tags,
                        defaulted=given is None,
                    )
                )
            ReviewStylistRating.objects.bulk_create(ratings)

            ReviewPhoto.objects.bulk_create(
                ReviewPhoto(review=review, file=photo, position=position)
                for position, photo in enumerate(submission.photos)
            )

            _add_to_totals(SalonRating, salon_id=review.salon_id, stars=review.salon_stars)
            for rating in ratings:
                _add_to_totals(StylistRating, stylist_id=rating.stylist_id, stars=rating.stars)
    except IntegrityError:
        # Someone got there first: a second submit of this booking (a double
        # tap), or one of the photos went on another review meanwhile.
        if Review.objects.filter(booking_id=visit.booking_id).exists():
            raise ReviewConflict("already_reviewed") from None
        raise ValidationError(
            {"photo_ids": [ErrorDetail("This photo can't be used. Please add it again.", code="invalid_photo")]}
        ) from None
    return review


def _add_to_totals(model, stars, **key):
    """Adds one review's stars to a salon's or a stylist's totals row."""
    model.objects.get_or_create(**key)
    model.objects.filter(**key).update(
        rating_sum=F("rating_sum") + stars,
        review_count=F("review_count") + 1,
    )


# ---------------------------------------------------------------------------
# The review object (POST's answer, and `review` in the GET)
# ---------------------------------------------------------------------------


def review_object(review, screen, build_url):
    # Read back from the database, so the answer is what was saved.
    ratings = list(review.stylist_ratings.all())
    cards = stylist_cards([rating.stylist_id for rating in ratings])

    # Stylists in the order the form lists them.
    order = {stylist["id"]: i for i, stylist in enumerate(screen.stylists)}
    ratings.sort(key=lambda rating: (order.get(str(rating.stylist_id), len(order)), str(rating.stylist_id)))

    stylists = []
    for rating in ratings:
        card = cards.get(rating.stylist_id) or {}
        stylists.append(
            {
                "stylist_id": str(rating.stylist_id),
                "name": card.get("name"),
                "avatar_url": card.get("avatar_url"),
                "stars": rating.stars,
                "tags": with_labels(rating.tags, STYLIST_LABELS),
                "defaulted": rating.defaulted,
            }
        )

    photos = review.photos.select_related("file").order_by("position")
    return {
        "id": str(review.id),
        "booking_id": str(review.booking_id),
        "submitted_at": _iso(review.submitted_at, screen.tz),
        "salon": {
            "stars": review.salon_stars,
            "tags": with_labels(review.salon_tags, SALON_LABELS),
        },
        "stylists": stylists,
        "comment": review.comment,
        "photos": [{"id": str(photo.file_id), "url": build_url(photo.file.file.url)} for photo in photos],
    }


def _id_text(value):
    """A uuid sent as text, in the form stylists[].id uses, or None."""
    if not isinstance(value, str):
        return None
    parsed = visits.as_uuid(value)
    return str(parsed) if parsed else None


def _iso(moment, tz):
    """ISO 8601 on the salon's clock, as the other booking times are: 2026-11-10T13:45:00+04:00."""
    return moment.astimezone(tz).isoformat(timespec="seconds")

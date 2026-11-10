"""GET and POST /api/v1/booking/<id>/review (docs/rate-your-visit-fe-contract.md)."""

import logging

from django.conf import settings
from django.http import Http404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.salons.views import _OUR_ENVELOPE, BookingApiDown

from . import service, visits

logger = logging.getLogger(__name__)

DARIUS = "c1a2b3c4-0000-4000-8000-000000000001"
MARCO = "c1a2b3c4-0000-4000-8000-000000000002"
BOOKING = "b7f1c2e4-1d2a-4c3b-9f8e-0a1b2c3d4e5f"

REVIEW_EXAMPLE = {
    "id": "0d9c7a52-5b8e-4a3f-9d61-2f0c1b7e4a10",
    "booking_id": BOOKING,
    "submitted_at": "2026-11-10T15:02:00+04:00",
    "salon": {
        "stars": 4,
        "tags": [
            {"code": "cleanliness", "label": "Cleanliness"},
            {"code": "ambience", "label": "Ambience"},
        ],
    },
    "stylists": [
        {
            "stylist_id": DARIUS,
            "name": "Darius Stone",
            "avatar_url": "https://cdn.example.com/stylists/darius.jpg",
            "stars": 5,
            "tags": [
                {"code": "skill", "label": "Skill"},
                {"code": "attention_to_detail", "label": "Attention to detail"},
            ],
            "defaulted": False,
        },
        {
            "stylist_id": MARCO,
            "name": "Marco Bell",
            "avatar_url": None,
            "stars": 4,
            "tags": [],
            "defaulted": True,
        },
    ],
    "comment": "Great fade and they started right on time.",
    "photos": [
        {"id": "5e0a1c9b-81c2-4d7e-9f30-6b2a8d4c1e01", "url": "https://cdn.example.com/reviews/1.jpg"},
    ],
}

FORM_EXAMPLE = {
    "booking": {
        "id": BOOKING,
        "status": "completed",
        "completed_at": "2026-11-10T13:45:00+04:00",
        "service_count": 3,
    },
    "salon": {
        "id": "5d6a110f-5633-4bfe-942f-19be062a4373",
        "name": "The Iron Razor Barbershop",
        "logo_url": "https://cdn.example.com/salons/iron-razor/logo.png",
    },
    "stylists": [
        {
            "id": DARIUS,
            "name": "Darius Stone",
            "avatar_url": "https://cdn.example.com/stylists/darius.jpg",
            "services": [{"id": "6f1d2e3a-0000-4000-8000-000000000001", "name": "Modern Fade"}],
        },
        {
            "id": MARCO,
            "name": "Marco Bell",
            "avatar_url": None,
            "services": [
                {"id": "6f1d2e3a-0000-4000-8000-000000000002", "name": "Beard trim"},
                {"id": "6f1d2e3a-0000-4000-8000-000000000003", "name": "Grey blending"},
            ],
        },
    ],
    "tags": {
        "salon": [{"code": "cleanliness", "label": "Cleanliness"}, {"code": "wifi", "label": "Wi-Fi"}],
        "stylist": [{"code": "skill", "label": "Skill"}, {"code": "beard_work", "label": "Beard work"}],
    },
    "limits": {"max_photos": service.MAX_PHOTOS, "comment_max_length": service.COMMENT_MAX_LENGTH},
    "can_rate": True,
    "reason": None,
    "review": None,
}

BOOKING_PARAM = OpenApiParameter(
    "booking_id",
    OpenApiTypes.STR,
    OpenApiParameter.PATH,
    description="The id the app uses for GET /booking/{id}: a booking's, or a party's (then the caller's own booking in it).",
)


def _error(code, message, field=None):
    return {"detail": message, "code": code, "errors": [{"field": field, "code": code, "message": message}]}


@extend_schema(
    parameters=[BOOKING_PARAM],
    summary="Rate your visit: the form",
    description=(
        "Fills the Rate your visit screen. Only the booking's owner: another "
        "customer's booking is the same 404 as one that does not exist.\n\n"
        "`can_rate` is true only for a completed booking with no review yet, "
        "within 14 days of `completed_at`. Otherwise `reason` says why "
        "(`not_completed`, `already_reviewed`, `window_closed`) and, once "
        "rated, `review` is what was sent. Not being able to rate is a 200, "
        "not an error. Behind RATE_VISIT_V1 (off: 404). See "
        "docs/rate-your-visit-fe-contract.md."
    ),
    responses={
        200: OpenApiResponse(
            response={"type": "object"},
            description="The form.",
            examples=[OpenApiExample("Can rate", value=FORM_EXAMPLE)],
        ),
        401: OpenApiResponse(response=_OUR_ENVELOPE, description="Not signed in, or the token has expired."),
        404: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description="`booking_not_found`: no such booking, or another customer's.",
            examples=[
                OpenApiExample(
                    "Not found",
                    value=_error("booking_not_found", service.BOOKING_NOT_FOUND),
                )
            ],
        ),
        503: OpenApiResponse(response=_OUR_ENVELOPE, description="The booking could not be read. Try again."),
    },
    methods=["GET"],
)
@extend_schema(
    parameters=[BOOKING_PARAM],
    summary="Rate your visit: send the review",
    description=(
        "The salon's stars are required. Stylists are optional: every stylist "
        "of the visit the customer did not rate gets the salon's stars, no "
        "tags and `defaulted: true`. One comment and up to 3 photos (ids from "
        "POST /upload-files) for the whole review. A review is final: one per "
        "booking, never changed. A double tap gets 409 `already_reviewed`. "
        "Behind RATE_VISIT_V1 (off: 404)."
    ),
    request={
        "application/json": {
            "type": "object",
            "properties": {
                "salon": {
                    "type": "object",
                    "properties": {
                        "stars": {"type": "integer", "minimum": 1, "maximum": 5},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["stars"],
                },
                "stylists": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "stylist_id": {"type": "string"},
                            "stars": {"type": "integer", "minimum": 1, "maximum": 5},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["stylist_id", "stars"],
                    },
                },
                "comment": {"type": "string", "maxLength": service.COMMENT_MAX_LENGTH},
                "photo_ids": {"type": "array", "items": {"type": "string"}, "maxItems": service.MAX_PHOTOS},
            },
            "required": ["salon"],
        }
    },
    examples=[
        OpenApiExample(
            "Darius rated, Marco skipped",
            request_only=True,
            value={
                "salon": {"stars": 4, "tags": ["cleanliness", "ambience"]},
                "stylists": [{"stylist_id": DARIUS, "stars": 5, "tags": ["skill", "attention_to_detail"]}],
                "comment": "Great fade and they started right on time.",
                "photo_ids": ["5e0a1c9b-81c2-4d7e-9f30-6b2a8d4c1e01"],
            },
        ),
    ],
    responses={
        201: OpenApiResponse(
            response={"type": "object"},
            description="Saved. The review object.",
            examples=[OpenApiExample("Saved", value=REVIEW_EXAMPLE)],
        ),
        401: OpenApiResponse(response=_OUR_ENVELOPE, description="Not signed in, or the token has expired."),
        404: OpenApiResponse(response=_OUR_ENVELOPE, description="`booking_not_found`."),
        409: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description="`already_reviewed`, `not_completed` or `window_closed`.",
            examples=[
                OpenApiExample(
                    "Already reviewed",
                    value=_error("already_reviewed", service.CONFLICTS["already_reviewed"]),
                )
            ],
        ),
        422: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description=(
                "`salon_required`, `invalid_stars`, `unknown_tag`, "
                "`stylist_not_on_booking`, `duplicate_stylist`, "
                "`comment_too_long`, `too_many_photos` or `invalid_photo`."
            ),
            examples=[
                OpenApiExample(
                    "A stylist from another visit",
                    value={
                        "detail": "This stylist didn't work on this visit.",
                        "code": "validation_error",
                        "errors": [
                            {
                                "field": "stylists[1].stylist_id",
                                "code": "stylist_not_on_booking",
                                "message": "This stylist didn't work on this visit.",
                            }
                        ],
                    },
                )
            ],
        ),
        503: OpenApiResponse(response=_OUR_ENVELOPE, description="The booking could not be read. Try again."),
    },
    methods=["POST"],
)
class BookingReviewView(APIView):
    """GET and POST /api/v1/booking/<id>/review"""

    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def get(self, request, booking_id):
        screen = self._screen(request, booking_id)
        review = service.existing_review(screen)
        form = service.review_form(screen, review, timezone.now(), request.build_absolute_uri)
        return Response(form)

    def post(self, request, booking_id):
        screen = self._screen(request, booking_id)

        can_rate, reason = service.rate_state(screen, service.existing_review(screen), timezone.now())
        if not can_rate:
            raise service.ReviewConflict(reason)

        submission = service.check_review(request.data, screen, request.user)
        review = service.save_review(screen, submission, request.user)
        return Response(
            service.review_object(review, screen, request.build_absolute_uri),
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _screen(request, booking_id):
        if not settings.RATE_VISIT_V1:
            raise Http404("Not found")
        try:
            screen = service.open_screen(booking_id, request.user)
        except visits.BookingDataUnavailable as exc:
            logger.error("Rate your visit: booking %s could not be read: %s", booking_id, exc)
            raise BookingApiDown() from exc
        if screen is None:
            raise service.booking_not_found()
        return screen

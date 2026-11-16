"""
GET /api/v1/salon/<id>/reviews, PUT and DELETE /api/v1/review/<id>/helpful
(docs/salon-reviews-fe-contract.md). Behind SALON_REVIEWS_V1 (off: 404).
"""

from django.conf import settings
from django.http import Http404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.salons.selectors import salon_profile
from apps.salons.views import _OUR_ENVELOPE, DEFAULT_BOOKING_PAGE_SIZE, MAX_BOOKING_PAGE_SIZE, _positive_int

from . import salon_reviews

SALON = "5d6a110f-5633-4bfe-942f-19be062a4373"

LIST_EXAMPLE = {
    "summary": {
        "rating": 4.8,
        "review_count": 320,
        "distribution": [
            {"stars": 5, "count": 280},
            {"stars": 4, "count": 26},
            {"stars": 3, "count": 8},
            {"stars": 2, "count": 4},
            {"stars": 1, "count": 2},
        ],
    },
    "count": 320,
    "next": f"https://api.gostyle.uk/api/v1/salon/{SALON}/reviews?sort=top&page=2&pageSize=20",
    "previous": None,
    "results": [
        {
            "id": "3e9b6c1d-8a24-4f70-b5d2-7c1e0f9a6b43",
            "stars": 5,
            "comment": "Best fade I have had in Dubai. In and out in forty minutes.",
            "submitted_at": "2026-11-12T09:41:07Z",
            "author": {"name": "Omar A.", "avatar_url": "https://cdn.example.com/customers/omar.jpg"},
            "services": [
                {"id": "55a98876-efdc-4d04-b4bc-f783ced6a8fb", "name": "Modern Fade"},
                {"id": "7a7fafa5-bc8d-485e-b1ef-15a8f1634906", "name": "Beard Trim"},
            ],
            "helpful_count": 13,
            "is_helpful": False,
            "is_mine": False,
            "photos": [{"id": "5e0a1c9b-81c2-4d7e-9f30-6b2a8d4c1e01", "url": "https://cdn.example.com/reviews/1.jpg"}],
            "tags": [{"code": "cleanliness", "label": "Cleanliness"}],
        },
        {
            "id": "c40f2e87-1b5a-4d39-9e6c-0a8d7f3b2e15",
            "stars": 4,
            "comment": None,
            "submitted_at": "2026-11-11T17:20:44Z",
            "author": {"name": "Layla H.", "avatar_url": None},
            "services": [{"id": "58c49adf-2781-40b3-8ec2-cb2beabe2575", "name": "Hair Cut & Spa"}],
            "helpful_count": 0,
            "is_helpful": False,
            "is_mine": True,
            "photos": [],
            "tags": [],
        },
    ],
}


def _error(code, message, field=None, top=None):
    return {"detail": message, "code": top or code, "errors": [{"field": field, "code": code, "message": message}]}


class SalonReviewsView(APIView):
    """GET /api/v1/salon/<id>/reviews"""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Ratings & Reviews: a salon's reviews",
        description=(
            "Fills the Ratings & Reviews screen, a page at a time. Public, as "
            "GET /salon/{id}: with a token, `is_helpful` and `is_mine` are about "
            "that customer; without one they are false.\n\n"
            "`summary` is on every page, whatever the sort: `rating` and "
            "`review_count` are exactly the salon profile's, and `distribution` "
            "is always five entries, 5 stars down to 1, adding up to "
            "`review_count`. Every review is listed, with or without a comment. "
            "`count`, `next`, `previous` and `results` page like GET /bookings.\n\n"
            "Order: `top` = helpful_count high to low, then reviews with a "
            "comment first, then newest first. `newest` = newest first. "
            "`highest` / `lowest` = stars, then newest first. Every order ends "
            "on the review id.\n\n"
            "`has_photos=true`: only the reviews with photos (`count` covers "
            "only those; `summary` stays the whole salon's). Behind "
            "SALON_REVIEWS_V1 (off: 404). See docs/salon-reviews-fe-contract.md."
        ),
        parameters=[
            OpenApiParameter("salon_id", OpenApiTypes.UUID, OpenApiParameter.PATH, description="The id GET /salon/{id} takes."),
            OpenApiParameter("sort", OpenApiTypes.STR, enum=list(salon_reviews.SORTS), default="top"),
            OpenApiParameter("page", OpenApiTypes.INT, default=1, description="As on GET /bookings."),
            OpenApiParameter("pageSize", OpenApiTypes.INT, default=20, description="Capped at 50, as on GET /bookings."),
            OpenApiParameter("has_photos", OpenApiTypes.BOOL, description="true: only the reviews with photos."),
        ],
        responses={
            200: OpenApiResponse(
                response={"type": "object"},
                description="One page of the salon's reviews, with the summary card.",
                examples=[OpenApiExample("A page", value=LIST_EXAMPLE)],
            ),
            401: OpenApiResponse(response=_OUR_ENVELOPE, description="A token was sent and it is bad or expired."),
            404: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description="`not_found`: no such salon, as GET /salon/{id} answers.",
                examples=[OpenApiExample("No such salon", value=_error("not_found", "Salon not found"))],
            ),
            422: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description="`invalid_sort`: not one of top, newest, highest, lowest.",
                examples=[
                    OpenApiExample(
                        "Bad sort",
                        value={
                            "detail": "Please correct the highlighted fields.",
                            "code": "validation_error",
                            "errors": [{"field": "sort", "code": "invalid_sort", "message": salon_reviews.INVALID_SORT}],
                        },
                    )
                ],
            ),
        },
    )
    def get(self, request, salon_id):
        if not settings.SALON_REVIEWS_V1:
            raise Http404("Not found")

        params = request.query_params
        sort = salon_reviews.read_sort(params.get("sort"))
        has_photos = params.get("has_photos") == "true"
        page = _positive_int(params.get("page"), 1)
        page_size = min(
            _positive_int(params.get("pageSize", params.get("page_size")), DEFAULT_BOOKING_PAGE_SIZE),
            MAX_BOOKING_PAGE_SIZE,
        )

        # The same read, and the same 404, as GET /salon/{id}.
        salon = salon_profile(salon_id, user=request.user)
        if salon is None:
            raise Http404("Salon not found")

        reviews = salon_reviews.reviews_of(salon.id, sort, has_photos=has_photos)
        count = reviews.count()
        start = (page - 1) * page_size
        page_rows = reviews.select_related("customer")[start : start + page_size]

        query = {"sort": sort, **({"has_photos": "true"} if has_photos else {})}
        next_url, previous_url = salon_reviews.page_links(
            request.build_absolute_uri(request.path), query, page, page_size, count
        )
        return Response(
            {
                "summary": salon_reviews.summary(salon),
                "count": count,
                "next": next_url,
                "previous": previous_url,
                "results": salon_reviews.result_rows(page_rows, request.user, request.build_absolute_uri),
            }
        )


_REVIEW_ID = OpenApiParameter(
    "review_id", OpenApiTypes.STR, OpenApiParameter.PATH, description="The review's id (results[].id)."
)
_STATE = {
    "type": "object",
    "properties": {"is_helpful": {"type": "boolean"}, "helpful_count": {"type": "integer"}},
}
_HELPFUL_ERRORS = {
    401: OpenApiResponse(response=_OUR_ENVELOPE, description="No token, or it has expired."),
    404: OpenApiResponse(
        response=_OUR_ENVELOPE,
        description="`review_not_found`: no such review.",
        examples=[
            OpenApiExample(
                "No such review",
                value=_error("review_not_found", salon_reviews.REVIEW_NOT_FOUND, top="not_found"),
            )
        ],
    ),
}


class ReviewHelpfulView(APIView):
    """PUT marks a review helpful, DELETE takes the mark back."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Mark a review helpful",
        description=(
            "No body. Safe to repeat: a review already marked changes nothing "
            "and answers the current state. Not on the customer's own review "
            "(403 `own_review`). Behind SALON_REVIEWS_V1 (off: 404)."
        ),
        parameters=[_REVIEW_ID],
        request=None,
        responses={
            200: OpenApiResponse(
                response=_STATE,
                description="The new state.",
                examples=[OpenApiExample("Marked", value={"is_helpful": True, "helpful_count": 14})],
            ),
            403: OpenApiResponse(
                response=_OUR_ENVELOPE,
                description="`own_review`: the customer wrote this review.",
                examples=[
                    OpenApiExample(
                        "Own review",
                        value=_error("own_review", salon_reviews.OWN_REVIEW, top="permission_denied"),
                    )
                ],
            ),
            **_HELPFUL_ERRORS,
        },
    )
    def put(self, request, review_id):
        self._check_on()
        return Response(salon_reviews.mark_helpful(review_id, request.user))

    @extend_schema(
        summary="Take a Helpful mark back",
        description=(
            "No body. Safe to repeat: a review not marked changes nothing and "
            "answers the current state. Behind SALON_REVIEWS_V1 (off: 404)."
        ),
        parameters=[_REVIEW_ID],
        request=None,
        responses={
            200: OpenApiResponse(
                response=_STATE,
                description="The new state.",
                examples=[OpenApiExample("Not marked", value={"is_helpful": False, "helpful_count": 13})],
            ),
            **_HELPFUL_ERRORS,
        },
    )
    def delete(self, request, review_id):
        self._check_on()
        return Response(salon_reviews.unmark_helpful(review_id, request.user))

    @staticmethod
    def _check_on():
        if not settings.SALON_REVIEWS_V1:
            raise Http404("Not found")

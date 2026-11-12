"""GET and POST /api/v1/me/gallery, DELETE /api/v1/me/gallery/<id> (docs/my-gallery-fe-contract.md)."""

import urllib.parse

from django.conf import settings
from django.http import Http404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.salons.views import _OUR_ENVELOPE, _positive_int

from . import service

HAIRCUT_ITEM = {
    "id": "8f3c2a71-5e4d-4b19-a0c6-2d7e9b1f4a53",
    "category": "haircut",
    "image_url": "https://cdn.example.com/customers/gallery/8f3c2a71.jpg",
    "thumbnail_url": "https://cdn.example.com/customers/gallery/8f3c2a71-480.jpg",
    "width": 1080,
    "height": 1350,
    "can_delete": True,
    "created_at": "2026-11-12T09:41:07Z",
}

PAGE_EXAMPLE = {
    "count": 37,
    "next": "https://api.gostyle.uk/api/v1/me/gallery?page=2&pageSize=20",
    "previous": None,
    "results": [
        HAIRCUT_ITEM,
        {
            "id": "b51e0d94-7c2a-4f36-9e81-6a3d0c5b7f28",
            "category": "beard",
            "image_url": "https://cdn.example.com/customers/gallery/b51e0d94.jpg",
            "thumbnail_url": None,
            "width": None,
            "height": None,
            "can_delete": True,
            "created_at": "2026-11-11T18:02:55Z",
        },
    ],
    "categories": [
        {"code": "haircut", "label": "Haircut", "count": 24, "can_add": True},
        {"code": "beard", "label": "Beard", "count": 10, "can_add": True},
        {"code": "moment", "label": "Moments", "count": 3, "can_add": False},
    ],
    "limits": {"max_references": service.MAX_REFERENCES},
}


def _error(code, message, field=None, top="validation_error"):
    return {"detail": message, "code": top, "errors": [{"field": field, "code": code, "message": message}]}


def _switch():
    if not settings.MY_GALLERY_V1:
        raise Http404("Not found")


@extend_schema(
    summary="My gallery: one page",
    description=(
        "The customer's own gallery, newest first: references (Haircut, "
        "Beard) and Moments (the photos on their own reviews). Pages like "
        "GET /bookings. `categories` and `limits` come on every page, "
        "whatever the category. Behind MY_GALLERY_V1 (off: 404). See "
        "docs/my-gallery-fe-contract.md."
    ),
    parameters=[
        OpenApiParameter(
            "category",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            description="One of categories[].code. Left out: every category (All). Unknown: 422 invalid_category.",
        ),
        OpenApiParameter("page", OpenApiTypes.INT, OpenApiParameter.QUERY, description="Default 1."),
        OpenApiParameter(
            "pageSize",
            OpenApiTypes.INT,
            OpenApiParameter.QUERY,
            description=f"Default {service.DEFAULT_PAGE_SIZE}, capped at {service.MAX_PAGE_SIZE}.",
        ),
    ],
    responses={
        200: OpenApiResponse(
            response={"type": "object"},
            description="One page.",
            examples=[OpenApiExample("All", value=PAGE_EXAMPLE)],
        ),
        401: OpenApiResponse(response=_OUR_ENVELOPE, description="Not signed in, or the token has expired."),
        422: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description="`invalid_category`: not one of the codes.",
            examples=[
                OpenApiExample(
                    "Unknown category",
                    value=_error("invalid_category", service.MESSAGES["invalid_category"], "category"),
                )
            ],
        ),
    },
    methods=["GET"],
)
@extend_schema(
    summary="My gallery: add a reference",
    description=(
        "Adds a photo from POST /upload-files (this customer's, an image) to a "
        "category whose can_add is true. The answer is the new item, as one "
        "row of results[]. The same file_id twice is a 409 "
        "`already_in_gallery` with nothing saved. Behind MY_GALLERY_V1 (off: 404)."
    ),
    request={
        "application/json": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "format": "uuid"},
                "category": {"type": "string"},
            },
            "required": ["file_id", "category"],
        }
    },
    examples=[
        OpenApiExample(
            "Add a haircut",
            request_only=True,
            value={"file_id": "e8a3f1c6-47b2-4d90-8c5e-1b6f0a9d2e74", "category": "haircut"},
        ),
    ],
    responses={
        201: OpenApiResponse(
            response={"type": "object"},
            description="Added.",
            examples=[OpenApiExample("Added", value=HAIRCUT_ITEM)],
        ),
        401: OpenApiResponse(response=_OUR_ENVELOPE, description="Not signed in, or the token has expired."),
        409: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description="`already_in_gallery` (field file_id) or `gallery_full` (field null).",
            examples=[
                OpenApiExample(
                    "Full",
                    value=_error("gallery_full", service.MESSAGES["gallery_full"], top="conflict"),
                )
            ],
        ),
        422: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description="`invalid_file` (field file_id) or `invalid_category` (field category).",
            examples=[
                OpenApiExample(
                    "Not this customer's upload",
                    value=_error("invalid_file", service.MESSAGES["invalid_file"], "file_id"),
                )
            ],
        ),
    },
    methods=["POST"],
)
class GalleryView(APIView):
    """GET and POST /api/v1/me/gallery"""

    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def get(self, request):
        _switch()
        params = request.query_params

        category = None
        if "category" in params:
            category = params["category"]
            if category not in service.CODES:
                raise service.invalid_category()

        # As GET /bookings: a bad number is the default, never an error.
        page = _positive_int(params.get("page"), 1)
        page_size = min(
            _positive_int(params.get("pageSize", params.get("page_size")), service.DEFAULT_PAGE_SIZE),
            service.MAX_PAGE_SIZE,
        )

        def link(number):
            query = {"category": category} if category is not None else {}
            query.update(page=number, pageSize=page_size)
            return request.build_absolute_uri(f"{request.path}?{urllib.parse.urlencode(query)}")

        return Response(
            service.gallery_page(request.user, category, page, page_size, request.build_absolute_uri, link)
        )

    def post(self, request):
        _switch()
        item = service.add_reference(request.user, request.data)
        return Response(service.item_object(item, request.build_absolute_uri), status=status.HTTP_201_CREATED)


@extend_schema(
    summary="My gallery: remove a reference",
    description=(
        "Only an item with can_delete true. 404 `gallery_item_not_found` for "
        "no such item, one already removed, or someone else's (never says "
        "which). 409 `not_deletable` for a Moment. Behind MY_GALLERY_V1 (off: 404)."
    ),
    parameters=[
        OpenApiParameter("item_id", OpenApiTypes.STR, OpenApiParameter.PATH, description="results[].id"),
    ],
    responses={
        204: OpenApiResponse(description="Removed. No body."),
        401: OpenApiResponse(response=_OUR_ENVELOPE, description="Not signed in, or the token has expired."),
        404: OpenApiResponse(
            response=_OUR_ENVELOPE,
            description="`gallery_item_not_found`.",
            examples=[
                OpenApiExample(
                    "Not found",
                    value=_error(
                        "gallery_item_not_found", service.MESSAGES["gallery_item_not_found"], top="not_found"
                    ),
                )
            ],
        ),
        409: OpenApiResponse(response=_OUR_ENVELOPE, description="`not_deletable`: a Moment."),
    },
)
class GalleryItemView(APIView):
    """DELETE /api/v1/me/gallery/<id>"""

    permission_classes = [IsAuthenticated]

    def delete(self, request, item_id):
        _switch()
        service.remove(request.user, item_id)
        return Response(status=status.HTTP_204_NO_CONTENT)

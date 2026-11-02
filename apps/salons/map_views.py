"""GET /api/v1/me/map/filters: the map's Filters drawer (MAP_FILTER_V1)."""

from django.conf import settings
from django.http import Http404
from drf_spectacular.utils import OpenApiExample, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .map_filters import category_option_id, drawer

# The drawer as it reads with two categories on the map. The ids are the real
# ones for those names (map_filters.category_option_id).
EXAMPLE = {
    "sections": [
        {
            "key": "service_categories",
            "title": "Service Categories",
            "multiple": True,
            "default": [],
            "options": [
                {"id": category_option_id("Hair"), "label": "Hair"},
                {"id": category_option_id("Beard"), "label": "Beard"},
            ],
        },
        {
            "key": "rating",
            "title": "Rating",
            "multiple": False,
            "default": ["any"],
            "options": [
                {"id": "any", "label": "Any"},
                {"id": "3", "label": "3.0+"},
                {"id": "4", "label": "4.0+"},
                {"id": "4.5", "label": "4.5+"},
                {"id": "5", "label": "5.0"},
            ],
        },
        {
            "key": "availability",
            "title": "Availability",
            "multiple": False,
            "default": ["any"],
            "options": [
                {"id": "any", "label": "Any"},
                {"id": "today", "label": "Today"},
                {"id": "tomorrow", "label": "Tomorrow"},
                {"id": "week", "label": "This week"},
            ],
        },
        {
            "key": "distance",
            "title": "Distance",
            "multiple": False,
            "default": ["any"],
            "options": [
                {"id": "any", "label": "Any"},
                {"id": "5", "label": "5 km"},
                {"id": "10", "label": "10 km"},
                {"id": "20", "label": "20 km"},
                {"id": "100", "label": "100 km"},
            ],
        },
        {
            "key": "additional",
            "title": "Additional",
            "multiple": True,
            "default": ["open_now"],
            "options": [{"id": "open_now", "label": "Open now"}],
        },
    ]
}

_SECTION_FIELDS = {
    "key": serializers.CharField(),
    "title": serializers.CharField(),
    "multiple": serializers.BooleanField(),
    "default": serializers.ListField(child=serializers.CharField()),
    "options": inline_serializer(
        "MapFilterOption",
        fields={"id": serializers.CharField(), "label": serializers.CharField()},
        many=True,
    ),
}


class MapFiltersView(APIView):
    """
    The drawer's sections and options. Each section's `key` is the param
    GET /discover/map takes, and each option's `id` a value it accepts
    (map_filters.py keeps both ends). Public, like the map: a token is read
    when sent and not needed.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        summary="The map's Filters drawer",
        description=(
            "MAP_FILTER_V1 (off: 404). The sections in the drawer's order. Send the "
            "picks back to GET /discover/map: the section's `key` as the param, the "
            "picked option ids as its value (comma separated where `multiple`). "
            "`default` is what the drawer opens with; `any` and an empty list mean "
            "no filter, so the param is left out."
        ),
        responses={
            200: inline_serializer(
                "MapFilters",
                fields={
                    "sections": inline_serializer(
                        "MapFilterSection", fields=_SECTION_FIELDS, many=True
                    )
                },
            )
        },
        examples=[OpenApiExample("The drawer", value=EXAMPLE, response_only=True)],
    )
    def get(self, request):
        if not settings.MAP_FILTER_V1:
            raise Http404
        return Response(drawer())

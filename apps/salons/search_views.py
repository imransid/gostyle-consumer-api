"""
GET /api/v1/search/salons and GET /api/v1/search/services: the Search
Results screen's two tabs, with every Smart Search filter at once
(SEARCH_RESULTS_V1; off, both are 404). The rules are search_results.py's;
docs/SEARCH_RESULTS_API.md is the app team's contract.

App routes of their own (the audit's D9): /discover, /discover/map and
/services keep their answers.
"""

import logging
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.http import Http404
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.exceptions import ErrorDetail, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from config.pagination import ClientPageNumberPagination

from . import map_availability, search_options, search_results, search_suggest
from .map_filters import ADDITIONAL_OPTIONS, AVAILABILITY_OPTIONS, DISTANCE_OPTIONS, RATING_IDS
from . import verified as verified_rule
from .params import ParamError
from .serializers import SalonCardSerializer
from .service_search_views import _pages_to_spread
from .views import MapAvailabilityDown, MapFilterInvalid

logger = logging.getLogger(__name__)


def _ids(pairs):
    return ", ".join(option_id for option_id, _ in pairs)


PARAMETERS = [
    OpenApiParameter("q", str, description=(
        "Up to 100 characters. Salons tab: the salon's name or city, or a service on its menu "
        "(name, category label or tag). Services tab: the service's name, category label or tag."
    )),
    OpenApiParameter("latitude", float, description="The phone's position. Send with longitude."),
    OpenApiParameter("longitude", float, description="The phone's position. Send with latitude."),
    OpenApiParameter("tab", str, description=(
        f"One of: {_ids(search_results.TAB_OPTIONS)}. near_me needs a position (nearest first); "
        "packages: salons whose Packages tab lists a package."
    )),
    OpenApiParameter("gender", str, enum=["all", "gents", "ladies", "unisex"], description=(
        "As category on /discover: strict, a unisex salon only under all."
    )),
    OpenApiParameter("service_categories", str, description=(
        "Category ids from GET /me/map/filters, comma separated. An unknown id is ignored."
    )),
    OpenApiParameter("rating", str, enum=["any", *RATING_IDS], description=(
        "The map drawer's ids (GET /me/map/filters): the salon's average is at least this."
    )),
    OpenApiParameter("availability", str, description=f"any, {_ids(AVAILABILITY_OPTIONS)}: a start left, as on the map."),
    OpenApiParameter("distance", str, description=f"any, {_ids(DISTANCE_OPTIONS)} (km from latitude, longitude)."),
    OpenApiParameter("additional", str, description=(
        f"Comma separated: {_ids(ADDITIONAL_OPTIONS)}; with MAP_FILTER_VERIFIED_V1 on, verified too."
    )),
    OpenApiParameter("verified", bool, description=(
        "MAP_FILTER_VERIFIED_V1 (off: ignored). true: only the salons our staff marked verified. "
        "false or anything else: no filter."
    )),
    OpenApiParameter("amenities", str, description=(
        f"Comma separated: {_ids(search_results.AMENITY_OPTIONS)}. parking: the profile lists "
        "parking or shows the Free Parking badge; free_parking: the badge only."
    )),
    OpenApiParameter("quick", str, description=(
        "SEARCH_OPTIONS_V1. One quick chip id from GET /search/options: a salon with a "
        "service that matches it (Services tab: the service matches it). A gone id is ignored."
    )),
    OpenApiParameter("service_tags", str, description=(
        "SEARCH_OPTIONS_V1. Sub tag ids from GET /search/options, comma separated: a "
        "service that matches any of them. A gone id is ignored."
    )),
    OpenApiParameter("page", int, description="From 1."),
    OpenApiParameter("pageSize", int, description="1 to 50, default 20."),
]

ERRORS = {
    400: OpenApiResponse(description=(
        "An id the lists do not offer (tab, rating, availability, distance, additional, "
        "amenities), or distance or tab=near_me without a position (the map's 400)."
    )),
    404: OpenApiResponse(description="SEARCH_RESULTS_V1 is off."),
    422: OpenApiResponse(description=(
        "Another param cannot be read: latitude without longitude, q too long, gender, sort "
        "(sort=distance without a position), pageSize."
    )),
    503: OpenApiResponse(description="availability was asked for and who is busy cannot be read."),
}


class _SearchView(APIView):
    """Both tabs: the switch, the params, the errors, the paging."""

    permission_classes = [AllowAny]
    sorts = frozenset()
    default_sort = None

    def query(self, request):
        if not settings.SEARCH_RESULTS_V1:
            raise Http404
        try:
            return search_results.parse_search(request.query_params, self.sorts, self.default_sort)
        except ParamError as exc:
            detail = {exc.param: [ErrorDetail(exc.message, code=exc.code)]}
            if exc.param in search_results.DRAWER_PARAMS:
                # The map's own answer for its filter params, so one error
                # reader serves the map and the search.
                raise MapFilterInvalid(detail) from exc
            raise ValidationError(detail) from exc

    @staticmethod
    def paginator(query):
        paginator = ClientPageNumberPagination()
        paginator.settled_page_size = query.page_size
        return paginator


class SearchSalonsView(_SearchView):
    """GET /api/v1/search/salons: the Salons tab, as /discover's cards."""

    sorts = search_results.SALON_SORTS
    default_sort = "rating"

    @extend_schema(
        summary="Search results: the Salons tab",
        description=(
            "SEARCH_RESULTS_V1 (off: 404). Every filter at once, the same card as "
            "/discover, in its page shape {count, next, previous, results}. `count` is "
            "every match: call with pageSize=1 for the Apply button's number. sort: "
            "distance (needs a position; the default with one), rating (the default "
            "without), popular (the weighted rating of /discover?is_popular)."
        ),
        parameters=[
            *PARAMETERS,
            OpenApiParameter("sort", str, enum=sorted(search_results.SALON_SORTS)),
        ],
        responses={200: SalonCardSerializer(many=True), **ERRORS},
    )
    def get(self, request):
        query = self.query(request)
        try:
            qs = search_results.salons(query, now=datetime.now(dt_timezone.utc))
        except map_availability.AvailabilityUnavailable as exc:
            logger.error("search availability: %s", exc)
            raise MapAvailabilityDown() from exc

        paginator = self.paginator(query)
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            SalonCardSerializer(page, many=True, context={"request": request}).data
        )


class SearchServicesView(_SearchView):
    """GET /api/v1/search/services: the Services tab."""

    sorts = search_results.SERVICE_SORTS
    default_sort = "popular"

    @extend_schema(
        summary="Search results: the Services tab",
        description=(
            "SEARCH_RESULTS_V1 (off: 404). The services of the salons the same filters "
            "keep, the text and the categories matched on each service. Each result: "
            "id, name, description, price, currency, duration_min, duration_max, images "
            "(up to 4), category {id, label}, salon {id, name, logo_url, category, "
            "distance, rating}. sort: distance (the salon's; the default with a "
            "position), rating (the salon's), popular (most booked in 30 days, at most "
            "2 of one salon on a page; the default without a position), price (lowest "
            "first)."
        ),
        parameters=[
            *PARAMETERS,
            OpenApiParameter("sort", str, enum=sorted(search_results.SERVICE_SORTS)),
        ],
        responses={200: OpenApiResponse(description="{count, next, previous, results}"), **ERRORS},
    )
    def get(self, request):
        query = self.query(request)
        try:
            rows, categories = search_results.service_list(query, now=datetime.now(dt_timezone.utc))
        except map_availability.AvailabilityUnavailable as exc:
            logger.error("search availability: %s", exc)
            raise MapAvailabilityDown() from exc

        rows = search_results.order_services(
            rows, query, _pages_to_spread(request, len(rows), query.page_size)
        )
        paginator = self.paginator(query)
        page = paginator.paginate_queryset(rows, request, view=self)
        images = search_results.images_for(page)
        return paginator.get_paginated_response(
            [search_results.service_result(row, categories, images) for row in page]
        )


class SearchOptionsView(APIView):
    """GET /api/v1/search/options: the Smart Search's lists (SEARCH_OPTIONS_V1)."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="The Smart Search's lists",
        description=(
            "SEARCH_OPTIONS_V1 (off: 404). The map drawer's shape: sections, each with "
            "`key` (the param GET /search/salons and /search/services take), `title`, "
            "`multiple`, `default` and `options` [{id, label}] (the quick chips and sub "
            "tags add `icon`, null when none). service_tags has `groups` [{key, title, "
            "options}] instead of options. Only chips with data are sent: one no public "
            "salon could match is left out. Cached 60 s."
        ),
        responses={200: OpenApiResponse(description="{sections: [...]}"), 404: ERRORS[404]},
    )
    def get(self, request):
        if not settings.SEARCH_OPTIONS_V1:
            raise Http404
        return Response(search_options.options())


class SearchSuggestView(APIView):
    """GET /api/v1/search/suggest: the type-ahead (SEARCH_SUGGEST_V1)."""

    permission_classes = [AllowAny]
    # Per address (per account with a token): SEARCH_SUGGEST_RATE, 429 past it.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "search_suggest"

    @extend_schema(
        summary="Search suggestions while typing",
        description=(
            "SEARCH_SUGGEST_V1 (off: 404). Up to 5 of each: salons {id, name, logo_url, "
            "distance} (name, branch name or city holds q), services {name, salons} (one "
            "per name across every public salon, with how many offer it; tapping one runs "
            "the search with that name), stylists {id, name, photo_url, salon {id, name}}. "
            "The best match first (the name starts with q, then a word of it does). "
            "Throttled per address: 429 past SEARCH_SUGGEST_RATE."
        ),
        parameters=[
            OpenApiParameter("q", str, required=True, description="2 to 100 characters."),
            OpenApiParameter("latitude", float, description="The phone's position, for the salons' distance."),
            OpenApiParameter("longitude", float, description="With latitude."),
            OpenApiParameter("verified", bool, description=(
                "MAP_FILTER_VERIFIED_V1 (off: ignored). true: only verified salons in `salons`."
            )),
        ],
        responses={
            200: OpenApiResponse(description="{salons, services, stylists}"),
            404: ERRORS[404],
            422: OpenApiResponse(description="q missing (required) or shorter than 2 (too_short); half a position."),
            429: OpenApiResponse(description="Too many calls from this address: wait and type on."),
        },
    )
    def get(self, request):
        if not settings.SEARCH_SUGGEST_V1:
            raise Http404
        try:
            q, latitude, longitude = search_suggest.parse(request.query_params)
        except ParamError as exc:
            raise ValidationError({exc.param: [ErrorDetail(exc.message, code=exc.code)]}) from exc
        return Response(
            search_suggest.suggest(q, latitude, longitude, verified_rule.wanted(request.query_params))
        )

"""
GET /api/v1/services without a salon's ids (SEARCH_POPULAR_V1): services from
every salon, for the search over the map (docs/SEARCH_POPULAR_API.md).

The old route, one salon's menu by tenant_id, is still views.ServiceListView,
on the same path: it hands a call with none of tenant_id, branch_id and
category_id here. The rules are service_search.py's; this reads the rows.
"""

import logging

from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from config.pagination import ClientPageNumberPagination

from . import service_search
from .params import ParamError, parse_service_search
from .selectors import (
    BookingCountsUnavailable,
    branch_service_rows,
    categories_for_tenants,
    menu_services,
    search_salons,
    service_booking_counts,
)

logger = logging.getLogger(__name__)


def service_search_response(request):
    """The page of results, in /discover's page shape (count, next, previous, results)."""
    try:
        params = parse_service_search(request.query_params)
    except ParamError as exc:
        raise ValidationError({exc.param: [exc.message]}) from exc

    salons = list(search_salons(params["latitude"], params["longitude"], params["radius_km"]))
    tenants = {salon["tenant_id"] for salon in salons}
    categories = categories_for_tenants(tenants)

    # SERVICE_CURRENCY_V1: each result also says the currency of its price.
    # Off, the currency columns are not even read.
    with_currency = settings.SERVICE_CURRENCY_V1

    term = params["search"]
    services = menu_services(
        tenants,
        term=term,
        label_ids=service_search.label_ids(categories, term) if term else (),
        other_from=list(categories) if term and service_search.finds_other(term) else None,
        with_currency=with_currency,
    )
    rows = service_search.listing(
        salons,
        services,
        branch_service_rows(
            {salon["branch_id"] for salon in salons}, with_currency=with_currency
        ),
    )
    if term:
        # The query only narrowed the services down; this is the rule.
        rows = [row for row in rows if service_search.matches(row, categories, term)]

    page_size = params["page_size"]
    if params["is_popular"]:
        rows = service_search.popular_first(rows, _booking_counts(salons))
        rows = service_search.spread(rows, page_size, _pages_to_spread(request, len(rows), page_size))
    else:
        rows = service_search.newest_first(rows)

    paginator = ClientPageNumberPagination()
    paginator.settled_page_size = page_size
    page = paginator.paginate_queryset(rows, request)
    return paginator.get_paginated_response(
        [service_search.result(row, categories, with_currency=with_currency) for row in page]
    )


def _booking_counts(salons):
    """
    The popular order's counts. booking-api's database not set, or down:
    none at all, so the order is its tie-break (the salon's rating, then the
    newest), never an error. Logged, so it does not pass unseen.
    """
    try:
        return service_booking_counts(
            {salon["branch_id"] for salon in salons},
            service_search.popular_since(timezone.now()),
            service_search.COUNTED_STATUSES,
        )
    except BookingCountsUnavailable as exc:
        logger.warning("popular services without booking counts: %s", exc)
        return {}


def _pages_to_spread(request, total, page_size):
    """
    How many pages service_search.spread must lay out: up to the one asked
    for. "last" is every page; anything else that is no page number is the
    paginator's 404 anyway.
    """
    raw = str(request.query_params.get(ClientPageNumberPagination.page_query_param) or "1").strip()
    if raw == "last":
        return max(1, -(-total // page_size))
    # ASCII only: "²".isdigit() is True, and int() of it fails.
    return int(raw) if raw.isascii() and raw.isdigit() and int(raw) > 0 else 1

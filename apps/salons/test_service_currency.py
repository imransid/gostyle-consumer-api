"""
The currency of each service the search lists (SERVICE_CURRENCY_V1,
docs/SEARCH_DISCOVERY_AUDIT.md X3): GET /services across salons also sends
`currency` right after `price`. Off, the results and the columns read are
exactly as before.

The querysets are built and inspected, never run. The view is called
through its real URL with its selectors mocked.
"""

import types
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.salons import selectors, service_search, service_search_views

TENANT = uuid.UUID("11111111-1111-1111-1111-1111111111a0")
SALON = uuid.UUID("33333333-3333-3333-3333-3333333333a1")
BRANCH = uuid.UUID("22222222-2222-2222-2222-2222222222a1")
CUT = uuid.UUID("66666666-6666-6666-6666-666666666601")
SERVICES_URL = "/api/v1/services"
NOW = datetime(2026, 11, 4, 8, 0, tzinfo=dt_timezone.utc)

SALON_ROW = {
    "id": SALON, "tenant_id": TENANT, "branch_id": BRANCH, "published_name": "Del Capo Barber",
    "branch_name": "Marina", "category": "male", "avg_rating": None, "review_count": 0,
}


def service_dict(currency="AED"):
    row = {
        "id": CUT, "tenant_id": TENANT, "name": "The Gentleman's Cut", "description": "A classic cut",
        "price_minor": 18000, "duration_minutes": 30, "category_0_id": None, "created_at": NOW,
    }
    if currency is not None:
        row["currency"] = currency
    return row


def result(service, branch_price_minor=None, branch_currency=None, with_currency=True):
    row = types.SimpleNamespace(
        salon=SALON_ROW, service=service,
        branch_price_minor=branch_price_minor, branch_currency=branch_currency,
    )
    return service_search.result(row, {}, with_currency=with_currency)


class ColumnsTests(SimpleTestCase):
    """The currency columns are read only with the switch's flag."""

    def test_without_it_menu_services_reads_today_s_columns(self):
        qs = selectors.menu_services([TENANT])
        self.assertNotIn("currency", qs.query.values_select)

    def test_with_it_menu_services_reads_the_service_s_currency(self):
        qs = selectors.menu_services([TENANT], with_currency=True)
        self.assertEqual(
            set(qs.query.values_select),
            {"id", "tenant_id", "name", "description", "price_minor", "duration_minutes",
             "category_0_id", "created_at", "currency"},
        )

    def test_branch_rows_read_the_branch_s_currency_only_with_it(self):
        for with_currency, fields in (
            (False, ("service_id", "branch_id", "price_minor", "available")),
            (True, ("service_id", "branch_id", "price_minor", "available", "currency")),
        ):
            with self.subTest(with_currency=with_currency), mock.patch.object(
                selectors.ServiceBranchAvailability, "objects"
            ) as objects:
                objects.filter.return_value.values.return_value = []
                selectors.branch_service_rows([BRANCH], with_currency=with_currency)
                objects.filter.return_value.values.assert_called_once_with(*fields)


class ListingTests(SimpleTestCase):
    """service_search.listing carries the branch row's currency."""

    def test_the_branch_s_currency_rides_with_its_price(self):
        rows = service_search.listing(
            [SALON_ROW], [service_dict()],
            {(CUT, BRANCH): {"price_minor": 20000, "available": True, "currency": "BDT"}},
        )
        self.assertEqual((rows[0].branch_price_minor, rows[0].branch_currency), (20000, "BDT"))

    def test_no_branch_row_no_branch_currency(self):
        rows = service_search.listing([SALON_ROW], [service_dict()], {})
        self.assertIsNone(rows[0].branch_currency)


class ResultTests(SimpleTestCase):
    """service_search.result: `currency` follows `price`, by menu.service_currency."""

    def test_without_it_there_is_no_currency_key(self):
        self.assertNotIn("currency", result(service_dict(), with_currency=False))

    def test_with_it_currency_comes_right_after_price(self):
        self.assertEqual(
            list(result(service_dict())),
            ["id", "name", "description", "price", "currency", "duration_min", "duration_max",
             "category", "salon"],
        )

    def test_the_service_s_own_currency(self):
        found = result(service_dict("AED"))
        self.assertEqual((found["price"], found["currency"]), (180, "AED"))

    def test_the_branch_s_currency_wins_with_its_price(self):
        found = result(service_dict("AED"), branch_price_minor=150000, branch_currency="BDT")
        self.assertEqual((found["price"], found["currency"]), (1500, "BDT"))

    def test_a_branch_price_without_a_currency_keeps_the_service_s(self):
        # Price and currency fall back separately, as on the platform.
        found = result(service_dict("AED"), branch_price_minor=20000, branch_currency=None)
        self.assertEqual((found["price"], found["currency"]), (200, "AED"))

    def test_the_rest_of_the_result_is_unchanged(self):
        on = result(service_dict())
        off = result(service_dict(), with_currency=False)
        on.pop("currency")
        self.assertEqual(on, off)


class ViewTests(SimpleTestCase):
    """GET /services across salons, through the real URL."""

    def call(self):
        with ExitStack() as stack:
            def patch(name, **kw):
                return stack.enter_context(mock.patch.object(service_search_views, name, **kw))

            patch("search_salons", return_value=[SALON_ROW])
            patch("categories_for_tenants", return_value={})
            self.menu = patch("menu_services", side_effect=lambda tenants, **kw: [
                service_dict("AED" if kw.get("with_currency") else None)
            ])
            self.branch_rows = patch("branch_service_rows", return_value={})
            patch("service_booking_counts", return_value={})
            return self.client.get(SERVICES_URL)

    @override_settings(SEARCH_POPULAR_V1=True)
    def test_off_the_result_is_today_s(self):
        body = self.call().json()
        self.assertEqual(body["results"], [{
            "id": str(CUT), "name": "The Gentleman's Cut", "description": "A classic cut",
            "price": 180, "duration_min": 30, "duration_max": 30,
            "category": {"id": "other", "label": "Other"},
            "salon": {"id": str(SALON), "name": "Del Capo Barber", "category": "gents"},
        }])
        self.assertIs(self.menu.call_args.kwargs["with_currency"], False)
        self.assertIs(self.branch_rows.call_args.kwargs["with_currency"], False)

    @override_settings(SEARCH_POPULAR_V1=True, SERVICE_CURRENCY_V1=True)
    def test_on_each_result_says_its_currency(self):
        body = self.call().json()
        self.assertEqual(body["results"][0]["currency"], "AED")
        self.assertEqual(body["results"][0]["price"], 180)
        self.assertIs(self.menu.call_args.kwargs["with_currency"], True)
        self.assertIs(self.branch_rows.call_args.kwargs["with_currency"], True)

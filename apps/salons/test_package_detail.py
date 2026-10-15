"""
GET /api/v1/salon/<salon_id>/package/<package_id>, the Package Details screen
(docs/package-detail-fe-contract.md, docs/PACKAGE_DETAIL_AUDIT.md section 7),
and the Packages tab it is opened from.

P1: the tab pinned as it answers today, before anything moves.

The selectors are mocked; no database.
"""

import types
import uuid
from contextlib import ExitStack
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase

from apps.salons import selectors

SALON = uuid.UUID("33333333-3333-3333-3333-333333333333")
TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
BRANCH = uuid.UUID("22222222-2222-2222-2222-222222222222")
PACKAGE = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1")
PACKAGE_2 = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2")


def tab_url(salon=SALON):
    return f"/api/v1/salon/{salon}/packages"


def old_tab_row(package_id=PACKAGE, **overrides):
    """A row of `selectors.salon_packages` as the tab reads it today."""
    return types.SimpleNamespace(**{
        "id": package_id,
        "name": "Groom and Go",
        "description": "Leave sharp, stay sharp.",
        "highlights": ["Gentleman's Cut", "Hot Towel Shave"],
        "total_minutes": 65,
        "price_minor": 37000,
        "sum_minor": 45900,
        "color_theme": "gold",
        **overrides,
    })


class OldTabSeams:
    """The tab's view with its two reads mocked."""

    def tab(self, rows=(), *, salon=True):
        found = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH) if salon else None
        with ExitStack() as stack:
            stack.enter_context(mock.patch("apps.salons.views.salon_profile", return_value=found))
            self.packages_lookup = stack.enter_context(
                mock.patch("apps.salons.views.salon_packages", return_value=list(rows))
            )
            return self.client.get(tab_url())


class TabPinnedTests(OldTabSeams, SimpleTestCase):
    """GET /salon/<id>/packages as it answers today (P1)."""

    def test_the_whole_answer(self):
        body = self.tab([old_tab_row()]).json()
        self.assertEqual(body, {
            "my_packages": [],
            "bundles": [{
                "id": str(PACKAGE),
                "name": "Groom and Go",
                "description": "Leave sharp, stay sharp.",
                "features": ["Gentleman's Cut", "Hot Towel Shave"],
                "duration_minutes": 65,
                "price": 370.0,
                "price_before": 459.0,
                "save_amount": 89.0,
                "theme": "gold",
            }],
        })

    def test_no_saving_is_null_not_zero(self):
        for sum_minor in (37000, 30000, None):
            with self.subTest(sum_minor=sum_minor):
                bundle = self.tab([old_tab_row(sum_minor=sum_minor)]).json()["bundles"][0]
                self.assertIsNone(bundle["price_before"])
                self.assertIsNone(bundle["save_amount"])

    def test_features_only_from_a_list(self):
        for highlights in ("not a list", None):
            with self.subTest(highlights=highlights):
                bundle = self.tab([old_tab_row(highlights=highlights)]).json()["bundles"][0]
                self.assertEqual(bundle["features"], [])

    def test_the_selector_s_order_is_kept(self):
        rows = [old_tab_row(PACKAGE_2, name="B"), old_tab_row(PACKAGE, name="A")]
        names = [b["name"] for b in self.tab(rows).json()["bundles"]]
        self.assertEqual(names, ["B", "A"])

    def test_nulls_pass_through(self):
        bundle = self.tab([old_tab_row(description=None, total_minutes=None,
                                       color_theme=None)]).json()["bundles"][0]
        self.assertIsNone(bundle["description"])
        self.assertIsNone(bundle["duration_minutes"])
        self.assertIsNone(bundle["theme"])

    def test_no_packages_is_an_empty_list(self):
        self.assertEqual(self.tab([]).json(), {"my_packages": [], "bundles": []})

    def test_an_unknown_salon_is_404(self):
        self.assertEqual(self.tab(salon=False).status_code, 404)


class OldTabSelectorTests(SimpleTestCase):
    """`selectors.salon_packages`, the tab's read today (P1)."""

    storefront = types.SimpleNamespace(tenant_id=TENANT, branch_id=BRANCH)

    def test_its_filters_and_order(self):
        with mock.patch.object(selectors, "ServicePackage") as package:
            selectors.salon_packages(self.storefront)
        package.objects.filter.assert_called_once_with(
            tenant_id=TENANT,
            status="PUBLISHED",
            deleted_at__isnull=True,
            online_booking_enabled=True,
        )
        annotated = package.objects.filter.return_value.annotate
        self.assertEqual(
            set(annotated.call_args.kwargs), {"branch_available", "total_minutes", "sum_minor"}
        )
        # F1: today a package needs a true row for this branch.
        annotated.return_value.filter.assert_called_once_with(branch_available=True)
        annotated.return_value.filter.return_value.order_by.assert_called_once_with("name")

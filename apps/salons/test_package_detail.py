"""
GET /api/v1/salon/<salon_id>/package/<package_id>, the Package Details screen
(docs/package-detail-fe-contract.md, docs/PACKAGE_DETAIL_AUDIT.md section 7),
and the Packages tab it is opened from.

P1: the tab pinned as it answers today, before anything moves.

P2: the platform's package rules in one place (packages.py, the package
selectors), and the tab on them behind PACKAGES_LIVE_RULES_V1.

P3: the route, its 404s, every field, and the OpenAPI shape and example
checked against what the view really answers.

P4: docs/PACKAGE_DETAIL_API.md's examples are real answers.

The selectors are mocked; no database.
"""

import json
import re
import types
import unittest
import uuid
from contextlib import ExitStack
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.db.models import OuterRef, Q
from django.test import SimpleTestCase, override_settings
from django.urls import resolve, reverse
from drf_spectacular.generators import SchemaGenerator

from apps.salons import menu, packages, selectors
from apps.salons.package_detail_serializers import EXAMPLE
from apps.salons.package_detail_views import NO_PACKAGE, NO_SALON, SalonPackageDetailView
from apps.salons.views import SalonPackagesView

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


# ---------------------------------------------------------------------------
# P2 fixtures: a package as the platform stores it, and the services it holds.
# ---------------------------------------------------------------------------

CUT = uuid.UUID("66666666-6666-6666-6666-666666666660")
BEARD = uuid.UUID("66666666-6666-6666-6666-666666666661")
TOWEL = uuid.UUID("66666666-6666-6666-6666-666666666662")
LONG_HAIR = uuid.UUID("99999999-9999-9999-9999-999999999991")


def snap_item(service_id, variant_id=None, quantity=1):
    return {"serviceId": str(service_id), "variantId": str(variant_id) if variant_id else None,
            "quantity": quantity, "note": None}


def snap(items=None, **overrides):
    """A `published_snapshot`, in the platform's own shape (PackageSnapshot)."""
    return {
        "name": "The Full Groom",
        "nameAr": None,
        "description": "The complete reset: cut, beard, hot towel.",
        "priceMinor": 46000,
        "currency": "AED",
        "items": [snap_item(CUT), snap_item(BEARD), snap_item(TOWEL)] if items is None else items,
        "pricingMode": "FIXED",
        "discountBps": None,
        **overrides,
    }


def package_row(package_id=PACKAGE, **overrides):
    """A `service_package` row with `branch_row`, as the package selectors give it."""
    return types.SimpleNamespace(**{
        "id": package_id,
        "tenant_id": TENANT,
        # The draft: what the owner is editing, not what is live.
        "name": "The Full Groom (draft)",
        "description": "Draft text.",
        "price_minor": 50000,
        "status": "PUBLISHED",
        "deleted_at": None,
        "online_booking_enabled": True,
        "branch_scoped": False,
        "branch_row": None,
        "published_snapshot": snap(),
        "published_version": 1,
        "color_theme": "gold",
        "highlights": ["Cut", "Beard", "Hot towel"],
        "scheduling": "SEQUENTIAL",
        "cancellation_policy": "  Free to cancel up to 24 hours before.  ",
        **overrides,
    })


def service(service_id, name, price_minor, minutes, **overrides):
    """A row of `selectors.package_service_rows`."""
    return {
        "id": service_id,
        "name": name,
        "description": None,
        "price_minor": price_minor,
        "branch_price_minor": None,
        "duration_minutes": minutes,
        "status": "PUBLISHED",
        "deleted_at": None,
        "online_booking_enabled": True,
        **overrides,
    }


def services(**changes):
    """The three services, with `changes` ({name: {column: value}}) applied."""
    rows = {
        CUT: service(CUT, "The Gentleman's Cut", 19900, 30, description="Cut, wash and style."),
        BEARD: service(BEARD, "Beard Trim & Shape", 23000, 20),
        TOWEL: service(TOWEL, "Hot Towel Shave", 26000, 25, description="A straight razor."),
    }
    names = {"cut": CUT, "beard": BEARD, "towel": TOWEL}
    for name, columns in changes.items():
        rows[names[name]] = {**rows[names[name]], **columns}
    return rows


def variant(variant_id=LONG_HAIR, service_id=CUT, price_minor=30000, minutes=50):
    return {"id": variant_id, "service_id": service_id, "price_minor": price_minor,
            "duration_minutes": minutes}


def item(service_id, variant_id=None, quantity=1):
    return {"service_id": service_id, "variant_id": variant_id, "quantity": quantity}


def lines_for(items=None, svc=None, variants=None):
    items = [item(CUT), item(BEARD), item(TOWEL)] if items is None else items
    return packages.lines(items, services() if svc is None else svc, variants or {})


class SoldHereTests(SimpleTestCase):
    """packages.sold_here: the platform's branch rule (listForBranch)."""

    def test_the_rule(self):
        for scoped, row, sold in (
            (False, None, True),    # the default: sold everywhere
            (False, True, True),
            (False, False, False),  # only an explicit false hides it
            (True, None, False),    # branch-scoped: only where ticked
            (True, True, True),
            (True, False, False),
        ):
            with self.subTest(scoped=scoped, row=row):
                pkg = package_row(branch_scoped=scoped, branch_row=row)
                self.assertIs(packages.sold_here(pkg), sold)


class OnSaleTests(SimpleTestCase):
    def test_on_sale(self):
        self.assertTrue(packages.on_sale(package_row()))

    def test_each_rule_takes_it_off(self):
        for change in ({"status": "HIDDEN"}, {"status": "ARCHIVED"}, {"status": "DRAFT"},
                       {"deleted_at": "2026-10-01"}, {"online_booking_enabled": False},
                       {"branch_row": False}):
            with self.subTest(**change):
                self.assertFalse(packages.on_sale(package_row(**change)))


class LiveContentTests(SimpleTestCase):
    """packages.live_content: the snapshot first (F2), the row when there is none."""

    def test_the_snapshot_wins_over_the_draft(self):
        content = packages.live_content(package_row(), draft_items=[item(TOWEL)])
        self.assertEqual(content, {
            "name": "The Full Groom",
            "description": "The complete reset: cut, beard, hot towel.",
            "price_minor": 46000,
            "items": [item(CUT), item(BEARD), item(TOWEL)],
        })

    def test_no_snapshot_is_the_row_and_its_draft_items(self):
        drafts = [{"package_id": PACKAGE, **item(TOWEL, quantity=2)}]
        content = packages.live_content(package_row(published_snapshot=None), drafts)
        self.assertEqual(content, {
            "name": "The Full Groom (draft)",
            "description": "Draft text.",
            "price_minor": 50000,
            "items": [item(TOWEL, quantity=2)],
        })

    def test_the_snapshot_s_order_and_variant(self):
        items = [snap_item(TOWEL), snap_item(CUT, LONG_HAIR, 2)]
        content = packages.live_content(package_row(published_snapshot=snap(items)))
        self.assertEqual(content["items"], [item(TOWEL), item(CUT, LONG_HAIR, 2)])

    def test_an_item_that_cannot_be_read_is_kept_as_unknown(self):
        items = [snap_item(CUT), {"serviceId": "nope"}, "not an item"]
        content = packages.live_content(package_row(published_snapshot=snap(items)))
        self.assertEqual(content["items"], [
            item(CUT), item(None), item(None),
        ])

    def test_a_bad_quantity_is_one(self):
        for quantity in (0, -1, None, "2", True):
            with self.subTest(quantity=quantity):
                items = [{**snap_item(CUT), "quantity": quantity}]
                content = packages.live_content(package_row(published_snapshot=snap(items)))
                self.assertEqual(content["items"][0]["quantity"], 1)

    def test_blank_text_is_none_and_a_blank_name_is_the_row_s(self):
        content = packages.live_content(
            package_row(published_snapshot=snap(name="  ", description="  "))
        )
        self.assertEqual(content["name"], "The Full Groom (draft)")
        self.assertIsNone(content["description"])

    def test_a_price_that_is_not_a_number_is_the_row_s(self):
        for price in (None, "46000", 460.0, True):
            with self.subTest(price=price):
                content = packages.live_content(package_row(published_snapshot=snap(priceMinor=price)))
                self.assertEqual(content["price_minor"], 50000)

    def test_items_that_are_not_a_list_are_none(self):
        content = packages.live_content(package_row(published_snapshot=snap(items="x")))
        self.assertEqual(content["items"], [])


class LinesTests(SimpleTestCase):
    """packages.lines: each item's price and minutes here (F3), bookable (F4)."""

    def test_the_services_in_order(self):
        rows, complete = lines_for([item(TOWEL), item(CUT)])
        self.assertTrue(complete)
        self.assertEqual(rows, [
            {"service_id": TOWEL, "name": "Hot Towel Shave", "description": "A straight razor.",
             "unit_minor": 26000, "minutes": 25, "quantity": 1, "bookable": True,
             "offered_here": True},
            {"service_id": CUT, "name": "The Gentleman's Cut", "description": "Cut, wash and style.",
             "unit_minor": 19900, "minutes": 30, "quantity": 1, "bookable": True,
             "offered_here": True},
        ])

    def test_this_branch_s_price(self):
        for branch, unit in ((15000, 15000), (0, 0), (None, 19900)):
            with self.subTest(branch=branch):
                rows, _ = lines_for([item(CUT)], services(cut={"branch_price_minor": branch}))
                self.assertEqual(rows[0]["unit_minor"], unit)

    def test_a_variant_sets_price_and_minutes(self):
        rows, _ = lines_for([item(CUT, LONG_HAIR)], variants={LONG_HAIR: variant()})
        self.assertEqual((rows[0]["unit_minor"], rows[0]["minutes"]), (30000, 50))

    def test_a_variant_with_no_minutes_takes_the_service_s(self):
        rows, _ = lines_for([item(CUT, LONG_HAIR)], variants={LONG_HAIR: variant(minutes=None)})
        self.assertEqual((rows[0]["unit_minor"], rows[0]["minutes"]), (30000, 30))

    def test_a_variant_not_found_or_of_another_service_is_ignored(self):
        for variants in ({}, {LONG_HAIR: variant(service_id=TOWEL)}):
            with self.subTest(variants=variants):
                rows, _ = lines_for([item(CUT, LONG_HAIR)], variants=variants)
                self.assertEqual((rows[0]["unit_minor"], rows[0]["minutes"]), (19900, 30))

    def test_a_service_not_found_is_left_out_and_not_complete(self):
        for missing in (uuid.uuid4(), None):
            with self.subTest(missing=missing):
                rows, complete = lines_for([item(CUT), item(missing)])
                self.assertFalse(complete)
                self.assertEqual([r["service_id"] for r in rows], [CUT])

    def test_bookable_is_the_services_tab_s_rule(self):
        for change in ({"status": "HIDDEN"}, {"deleted_at": "x"}, {"online_booking_enabled": False}):
            with self.subTest(**change):
                rows, complete = lines_for([item(CUT)], services(cut=change))
                self.assertTrue(complete)
                self.assertFalse(rows[0]["bookable"])

    def test_offered_here_is_the_platform_s_branch_rule(self):
        for change, offered in (({}, True),
                                ({"branch_available": False}, False),
                                ({"branch_scoped": True}, False),
                                ({"branch_scoped": True, "branch_available": True}, True)):
            with self.subTest(**change):
                rows, _ = lines_for([item(CUT)], services(cut=change))
                self.assertIs(rows[0]["offered_here"], offered)
                # Still on the menu: the two rules are separate.
                self.assertTrue(rows[0]["bookable"])


class IsActiveTests(SimpleTestCase):
    def active(self, pkg=None, items=None, svc=None):
        rows, complete = lines_for(items, svc)
        return packages.is_active(pkg or package_row(), rows, complete)

    def test_on_sale_and_every_service_bookable(self):
        self.assertTrue(self.active())

    def test_off_sale(self):
        self.assertFalse(self.active(package_row(status="HIDDEN")))

    def test_a_service_off_the_menu(self):
        self.assertFalse(self.active(svc=services(towel={"status": "ARCHIVED"})))

    def test_a_service_not_found(self):
        self.assertFalse(self.active(items=[item(CUT), item(uuid.uuid4())]))

    def test_no_services(self):
        self.assertFalse(self.active(items=[]))

    def test_the_branch_rule_for_services_is_not_part_of_it(self):
        # is_active stays what it was; only can_book reads offered_here.
        self.assertTrue(self.active(svc=services(cut={"branch_available": False})))


class CanBookTests(SimpleTestCase):
    """
    packages.can_book: booking-api's rule for selling a package
    (platform-package-catalogue.ts), on the content the customer sees.
    """

    def can_book(self, pkg=None, svc=None, variants=None):
        pkg = pkg or package_row()
        content = packages.live_content(pkg)
        rows, complete = packages.lines(content["items"], svc or services(), variants or {})
        return packages.can_book(pkg, content, rows, complete)

    def test_on_sale_every_service_offered_here_no_variant_a_price(self):
        self.assertTrue(self.can_book())

    def test_not_when_it_is_not_active(self):
        for pkg, svc in ((package_row(status="HIDDEN"), None),
                         (package_row(branch_row=False), None),
                         (None, services(towel={"online_booking_enabled": False}))):
            with self.subTest(pkg=pkg, svc=svc):
                self.assertFalse(self.can_book(pkg, svc))

    def test_not_when_this_branch_does_not_offer_a_service(self):
        # Each is still active (the tab lists it); booking-api would not sell it.
        for change in ({"branch_available": False},
                       {"branch_scoped": True},
                       {"category_0_id": uuid.uuid4(), "category_branch_available": False}):
            with self.subTest(**change):
                self.assertFalse(self.can_book(svc=services(beard=change)))

    def test_a_service_s_own_row_here_turns_it_back_on(self):
        on = {"branch_scoped": True, "category_0_id": uuid.uuid4(),
              "category_branch_available": False, "branch_available": True}
        self.assertTrue(self.can_book(svc=services(beard=on)))

    def test_not_when_an_item_is_pinned_to_a_variant_live_or_deleted(self):
        pkg = package_row(published_snapshot=snap([snap_item(CUT, LONG_HAIR), snap_item(TOWEL)]))
        for variants in ({LONG_HAIR: variant()}, {}):
            with self.subTest(variants=variants):
                self.assertFalse(self.can_book(pkg, variants=variants))

    def test_not_at_a_price_of_0(self):
        self.assertFalse(self.can_book(package_row(published_snapshot=snap(priceMinor=0))))

    def test_not_when_a_service_in_it_is_priced_0_here(self):
        # booking-api's catalogue refuses the whole basket for one service at
        # nothing (409 BOOKING_STATE_INVALID). This branch's 0 is a 0, and so
        # is a service's own 0 with no branch price set.
        for change in ({"branch_price_minor": 0},
                       {"price_minor": 0, "branch_price_minor": None}):
            with self.subTest(**change):
                pkg = package_row()
                content = packages.live_content(pkg)
                rows, complete = packages.lines(content["items"], services(beard=change), {})
                # Still active and still listed: only booking it is refused.
                self.assertTrue(packages.is_active(pkg, rows, complete))
                self.assertFalse(packages.can_book(pkg, content, rows, complete))

    def test_a_branch_price_above_0_sells_a_service_whose_own_price_is_0(self):
        self.assertTrue(self.can_book(svc=services(beard={"price_minor": 0,
                                                          "branch_price_minor": 15000})))

    def test_a_package_dearer_than_its_services_is_still_sold_at_its_price(self):
        pkg = package_row(published_snapshot=snap(priceMinor=90000))
        content = packages.live_content(pkg)
        rows, _ = packages.lines(content["items"], services(), {})
        self.assertIsNone(packages.saving(content["price_minor"], rows)["save_amount"])
        self.assertTrue(self.can_book(pkg))

    def test_the_live_content_decides_not_the_draft(self):
        # The draft has a price of 0; the published price is what is sold.
        self.assertTrue(self.can_book(package_row(price_minor=0)))
        # Never published: the row and its items are the live content.
        pkg = package_row(published_snapshot=None, price_minor=0)
        rows, complete = packages.lines([item(CUT)], services(), {})
        self.assertFalse(packages.can_book(pkg, packages.live_content(pkg, [item(CUT)]), rows, complete))


class SavingTests(SimpleTestCase):
    """packages.saving: against the services booked separately here."""

    def saving(self, price, *units):
        rows = [{"unit_minor": u, "quantity": 1} for u in units]
        return packages.saving(price, rows)

    def test_their_example_s_rule(self):
        # 199 + 230 + 260 = 689 against 586: saves 103, 14.95% -> 15.
        self.assertEqual(self.saving(58600, 19900, 23000, 26000), {
            "price_before": Decimal("689"),
            "save_amount": Decimal("103"),
            "save_percent": 15,
        })

    def test_save_amount_is_price_before_minus_price(self):
        answer = self.saving(46000, 19900, 23000, 26000)
        self.assertEqual(answer["price_before"] - Decimal("460"), answer["save_amount"])

    def test_percent_rounds_half_up_and_is_never_0(self):
        for price, percent in ((99950, 1),   # 0.05%
                               (99500, 1),   # 0.5%
                               (98500, 2),   # 1.5%
                               (98600, 1),   # 1.4%
                               (0, 100)):    # free
            with self.subTest(price=price):
                self.assertEqual(self.saving(price, 100000)["save_percent"], percent)

    def test_quantity_counts(self):
        rows = [{"unit_minor": 10000, "quantity": 3}]
        self.assertEqual(packages.saving(25000, rows)["price_before"], Decimal("300"))

    def test_no_saving_is_all_none(self):
        nothing = {"price_before": None, "save_amount": None, "save_percent": None}
        for price, units in ((68900, (19900, 23000, 26000)),   # the same
                             (70000, (19900, 23000, 26000)),   # dearer
                             (46000, ())):                     # nothing to add up
            with self.subTest(price=price, units=units):
                self.assertEqual(self.saving(price, *units), nothing)


class DurationAndTimelineTests(SimpleTestCase):
    def test_duration_adds_up_with_quantity(self):
        rows, _ = lines_for([item(CUT, quantity=2), item(TOWEL)])
        self.assertEqual(packages.duration_minutes(rows), 30 * 2 + 25)

    def test_no_lines_is_none(self):
        self.assertIsNone(packages.duration_minutes([]))

    def test_a_sequential_visit(self):
        rows, _ = lines_for()
        self.assertEqual(packages.timeline(rows, "SEQUENTIAL"), [
            {"service_id": str(CUT), "name": "The Gentleman's Cut", "offset_minutes": 0, "duration_minutes": 30},
            {"service_id": str(BEARD), "name": "Beard Trim & Shape", "offset_minutes": 30, "duration_minutes": 20},
            {"service_id": str(TOWEL), "name": "Hot Towel Shave", "offset_minutes": 50, "duration_minutes": 25},
        ])

    def test_the_last_step_ends_at_the_hero_s_minutes(self):
        rows, _ = lines_for()
        last = packages.timeline(rows, "SEQUENTIAL")[-1]
        self.assertEqual(last["offset_minutes"] + last["duration_minutes"],
                         packages.duration_minutes(rows))

    def test_parallel_steps_all_start_at_0(self):
        rows, _ = lines_for()
        self.assertEqual([s["offset_minutes"] for s in packages.timeline(rows, "PARALLEL")], [0, 0, 0])

    def test_a_quantity_is_still_one_step_of_one_service_s_minutes(self):
        rows, _ = lines_for([item(CUT, quantity=2), item(TOWEL)])
        steps = packages.timeline(rows, "SEQUENTIAL")
        self.assertEqual([(s["offset_minutes"], s["duration_minutes"]) for s in steps],
                         [(0, 30), (30, 25)])

    def test_includes_is_one_unit_s_price_and_the_quantity(self):
        rows, _ = lines_for([item(CUT, quantity=2)])
        self.assertEqual(packages.includes(rows), [
            {"service_id": str(CUT), "name": "The Gentleman's Cut",
             "price": Decimal("199"), "quantity": 2},
        ])


class PackageSelectorTests(SimpleTestCase):
    """The package selectors' filters (their SQL was run on the local seed too)."""

    salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH)

    def test_live_salon_packages(self):
        with mock.patch.object(selectors, "ServicePackage") as package, \
                mock.patch.object(selectors, "_package_branch_row", return_value="row") as row:
            selectors.live_salon_packages(self.salon)
        package.objects.filter.assert_called_once_with(
            tenant_id=TENANT, status="PUBLISHED", deleted_at__isnull=True,
            online_booking_enabled=True,
        )
        row.assert_called_once_with(BRANCH)
        annotate = package.objects.filter.return_value.annotate
        annotate.assert_called_once_with(branch_row="row")
        # No branch filter in SQL: packages.is_active applies the rule.
        annotate.return_value.order_by.assert_called_once_with("name", "id")

    def test_package_for_salon(self):
        with mock.patch.object(selectors, "ServicePackage") as package, \
                mock.patch.object(selectors, "_package_branch_row", return_value="row") as row:
            chain = package.objects.filter.return_value.filter.return_value
            chain.annotate.return_value.first.return_value = "found"
            answer = selectors.package_for_salon(self.salon, PACKAGE)
        # This tenant only; no status, deleted or branch filter: a pulled
        # package still opens, as inactive.
        package.objects.filter.assert_called_once_with(id=PACKAGE, tenant_id=TENANT)
        package.objects.filter.return_value.filter.assert_called_once_with(
            Q(published_version__isnull=False) | Q(status__in=("PUBLISHED", "HIDDEN", "ARCHIVED"))
        )
        row.assert_called_once_with(BRANCH)
        chain.annotate.assert_called_once_with(branch_row="row")
        self.assertEqual(answer, "found")

    def test_the_branch_row_is_this_branch_s(self):
        with mock.patch.object(selectors, "ServicePackageBranchAvailability") as rows:
            selectors._package_branch_row(BRANCH)
        rows.objects.filter.assert_called_once_with(package_id=OuterRef("pk"), branch_id=BRANCH)
        rows.objects.filter.return_value.values.assert_called_once_with("available")

    def test_draft_items_in_each_package_s_order(self):
        with mock.patch.object(selectors, "ServicePackageItem") as items:
            selectors.package_item_rows([PACKAGE])
        items.objects.filter.assert_called_once_with(package_id__in=[PACKAGE])
        items.objects.filter.return_value.order_by.assert_called_once_with(
            "package_id", "sort_order", "id"
        )

    def test_services_this_tenant_s_any_state_this_branch_s_price(self):
        with mock.patch.object(selectors, "Service") as service, \
                mock.patch.object(selectors, "branch_price_minor", return_value="price") as price:
            service.objects.filter.return_value.annotate.return_value.values.return_value = [
                {"id": CUT, "name": "Cut"},
            ]
            answer = selectors.package_service_rows(self.salon, {CUT})
        service.objects.filter.assert_called_once_with(id__in={CUT}, tenant_id=TENANT)
        price.assert_called_once_with(OuterRef("pk"), BRANCH)
        self.assertEqual(answer, {CUT: {"id": CUT, "name": "Cut"}})

    def test_services_read_what_the_branch_rule_needs_in_the_same_query(self):
        with mock.patch.object(selectors, "Service") as service, \
                mock.patch.object(selectors, "service_branch_available", return_value="own") as own, \
                mock.patch.object(selectors, "category_branch_available", return_value="cat") as cat:
            selectors.package_service_rows(self.salon, {CUT})
        own.assert_called_once_with(OuterRef("pk"), BRANCH)
        cat.assert_called_once_with(OuterRef("category_0_id"), BRANCH)
        annotate = service.objects.filter.return_value.annotate
        annotate.assert_called_once()
        self.assertEqual(annotate.call_args.kwargs["branch_available"], "own")
        self.assertEqual(annotate.call_args.kwargs["category_branch_available"], "cat")
        fields = annotate.return_value.values.call_args.args
        for name in ("description", "branch_scoped", "category_0_id",
                     "branch_available", "category_branch_available"):
            self.assertIn(name, fields)

    def test_the_branch_rows_are_this_branch_s(self):
        with mock.patch.object(selectors, "ServiceBranchAvailability") as svc_rows, \
                mock.patch.object(selectors, "CategoryBranchAvailability") as cat_rows:
            selectors.service_branch_available(OuterRef("pk"), BRANCH)
            selectors.category_branch_available(OuterRef("category_0_id"), BRANCH)
        svc_rows.objects.filter.assert_called_once_with(service_id=OuterRef("pk"), branch_id=BRANCH)
        svc_rows.objects.filter.return_value.values.assert_called_once_with("available")
        cat_rows.objects.filter.assert_called_once_with(
            category_id=OuterRef("category_0_id"), branch_id=BRANCH)
        cat_rows.objects.filter.return_value.values.assert_called_once_with("available")

    def test_variants_live_only(self):
        with mock.patch.object(selectors, "ServiceVariant") as variants:
            variants.objects.filter.return_value.values.return_value = [variant()]
            answer = selectors.package_variant_rows({LONG_HAIR})
        variants.objects.filter.assert_called_once_with(id__in={LONG_HAIR}, deleted_at__isnull=True)
        self.assertEqual(answer, {LONG_HAIR: variant()})


class Reads:
    """selectors.package_lines with its three reads mocked."""

    def reads(self, stack, *, drafts=(), svc=None, variants=None):
        self.drafts_read = stack.enter_context(mock.patch.object(
            selectors, "package_item_rows", return_value=list(drafts)))
        self.services_read = stack.enter_context(mock.patch.object(
            selectors, "package_service_rows", return_value=services() if svc is None else svc))
        self.variants_read = stack.enter_context(mock.patch.object(
            selectors, "package_variant_rows", return_value=variants or {}))


class PackageLinesTests(Reads, SimpleTestCase):
    salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH)

    def test_published_packages_read_no_drafts_and_no_variants(self):
        with ExitStack() as stack:
            self.reads(stack)
            answer = selectors.package_lines(self.salon, [package_row()])
        self.drafts_read.assert_not_called()
        self.variants_read.assert_not_called()
        self.services_read.assert_called_once_with(self.salon, {CUT, BEARD, TOWEL})
        content, rows, complete = answer[PACKAGE]
        self.assertEqual(content["name"], "The Full Groom")
        self.assertEqual([r["service_id"] for r in rows], [CUT, BEARD, TOWEL])
        self.assertTrue(complete)

    def test_one_read_each_for_many_packages(self):
        draft = package_row(PACKAGE_2, published_snapshot=None)
        drafts = [{"package_id": PACKAGE_2, **item(TOWEL, LONG_HAIR)}]
        with ExitStack() as stack:
            self.reads(stack, drafts=drafts, variants={LONG_HAIR: variant(service_id=TOWEL)})
            answer = selectors.package_lines(self.salon, [package_row(), draft])
        self.drafts_read.assert_called_once_with([PACKAGE_2])
        self.services_read.assert_called_once_with(self.salon, {CUT, BEARD, TOWEL})
        self.variants_read.assert_called_once_with({LONG_HAIR})
        _, rows, _ = answer[PACKAGE_2]
        self.assertEqual([(r["service_id"], r["unit_minor"]) for r in rows], [(TOWEL, 30000)])

    def test_nothing_to_read_for_an_empty_package(self):
        with ExitStack() as stack:
            self.reads(stack)
            answer = selectors.package_lines(self.salon, [package_row(published_snapshot=snap(items=[]))])
        self.services_read.assert_not_called()
        self.assertEqual(answer[PACKAGE][1:], ([], True))


class LiveTabTests(Reads, SimpleTestCase):
    """The tab with PACKAGES_LIVE_RULES_V1 on: the platform's rules (F1 to F4)."""

    def tab(self, rows, **reads):
        found = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH)
        with ExitStack() as stack, override_settings(PACKAGES_LIVE_RULES_V1=True):
            stack.enter_context(mock.patch("apps.salons.views.salon_profile", return_value=found))
            self.old_read = stack.enter_context(mock.patch("apps.salons.views.salon_packages"))
            stack.enter_context(mock.patch(
                "apps.salons.views.live_salon_packages", return_value=list(rows)))
            # The lookup `services` used to make, once per bundle. Mocked only
            # so that a call to it is seen, not raised: it must not be made.
            self.per_bundle_lookup = stack.enter_context(
                mock.patch("apps.salons.views.services_by_ids"))
            self.reads(stack, **reads)
            return self.client.get(tab_url()).json()

    def test_the_whole_answer_from_the_published_content(self):
        body = self.tab([package_row()])
        self.old_read.assert_not_called()
        self.assertEqual(body, {
            "my_packages": [],
            "bundles": [{
                "id": str(PACKAGE),
                "name": "The Full Groom",
                "description": "The complete reset: cut, beard, hot towel.",
                "features": ["Cut", "Beard", "Hot towel"],
                "duration_minutes": 75,
                "price": 460.0,
                "price_before": 689.0,
                "save_amount": 229.0,
                "theme": "gold",
                "services": [
                    {"id": str(CUT), "name": "The Gentleman's Cut",
                     "description": "Cut, wash and style.", "price": 199.0,
                     "duration_min": 30, "duration_max": 30, "quantity": 1},
                    {"id": str(BEARD), "name": "Beard Trim & Shape", "description": None,
                     "price": 230.0, "duration_min": 20, "duration_max": 20, "quantity": 1},
                    {"id": str(TOWEL), "name": "Hot Towel Shave",
                     "description": "A straight razor.", "price": 260.0,
                     "duration_min": 25, "duration_max": 25, "quantity": 1},
                ],
            }],
        })

    def test_services_are_the_package_s_own_lines_in_its_order(self):
        # The snapshot's order (not the database's), this branch's price (the
        # Services tab's, not the base price), and each line's quantity.
        items = [snap_item(TOWEL), snap_item(CUT, quantity=2)]
        body = self.tab([package_row(published_snapshot=snap(items))],
                        svc=services(cut={"branch_price_minor": 15000}))
        self.assertEqual(
            [(s["id"], s["price"], s["quantity"]) for s in body["bundles"][0]["services"]],
            [(str(TOWEL), 260.0, 1), (str(CUT), 150.0, 2)],
        )

    def test_services_agree_with_the_detail_s_includes_and_timeline(self):
        # A variant line is the variant's price and minutes, as there.
        pkg = package_row(published_snapshot=snap([snap_item(CUT, LONG_HAIR), snap_item(TOWEL)]))
        reads = dict(variants={LONG_HAIR: variant()})
        line = self.tab([pkg], **reads)["bundles"][0]["services"][0]
        self.assertEqual((line["price"], line["duration_min"], line["duration_max"]), (300.0, 50, 50))
        with ExitStack() as stack:
            self.reads(stack, **reads)
            content, rows, complete = selectors.package_lines(
                types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH), [pkg])[pkg.id]
        detail = packages.detail(types.SimpleNamespace(id=SALON), pkg, content, rows, complete)
        self.assertEqual(detail["includes"][0]["price"], line["price"])
        self.assertEqual(detail["timeline"][0]["duration_minutes"], line["duration_min"])

    def test_one_services_read_for_every_bundle_and_no_lookup_per_bundle(self):
        rows = [package_row(PACKAGE), package_row(PACKAGE_2, published_snapshot=snap(name="Second"))]
        body = self.tab(rows)
        self.assertEqual([len(b["services"]) for b in body["bundles"]], [3, 3])
        self.services_read.assert_called_once()
        self.per_bundle_lookup.assert_not_called()

    def test_can_book_only_with_package_booking_v1(self):
        with override_settings(PACKAGE_BOOKING_V1=False):
            self.assertNotIn("can_book", self.tab([package_row()])["bundles"][0])
        with override_settings(PACKAGE_BOOKING_V1=True):
            self.assertIs(self.tab([package_row()])["bundles"][0]["can_book"], True)

    def test_a_bundle_booking_api_would_not_sell_is_listed_but_cannot_be_booked(self):
        third = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3")
        rows = [package_row(PACKAGE, published_snapshot=snap([snap_item(CUT, LONG_HAIR)], name="A")),
                package_row(PACKAGE_2, published_snapshot=snap([snap_item(CUT), snap_item(BEARD)], name="B")),
                package_row(third, published_snapshot=snap([snap_item(TOWEL)], name="C"))]
        with override_settings(PACKAGE_BOOKING_V1=True):
            body = self.tab(rows, svc=services(towel={"branch_available": False}),
                            variants={LONG_HAIR: variant()})
        # All three on the tab (is_active), only B for sale as one visit.
        self.assertEqual([(b["name"], b["can_book"]) for b in body["bundles"]],
                         [("A", False), ("B", True), ("C", False)])

    def test_the_tab_and_the_detail_say_the_same(self):
        for svc in (services(), services(cut={"branch_available": False}),
                    services(beard={"branch_scoped": True}),
                    services(towel={"branch_price_minor": 0})):
            with self.subTest(svc=svc), override_settings(PACKAGE_BOOKING_V1=True):
                tab = self.tab([package_row()], svc=svc)["bundles"][0]["can_book"]
                detail = DetailSeams.call(self, svc=svc).json()["can_book"]
                self.assertEqual(tab, detail)

    def test_f1_sold_everywhere_without_a_branch_row(self):
        self.assertEqual(len(self.tab([package_row(branch_row=None)])["bundles"]), 1)

    def test_the_branch_rule_hides(self):
        for change in ({"branch_row": False}, {"branch_scoped": True, "branch_row": None}):
            with self.subTest(**change):
                self.assertEqual(self.tab([package_row(**change)])["bundles"], [])

    def test_f4_a_service_off_the_menu_hides_it(self):
        body = self.tab([package_row()], svc=services(beard={"online_booking_enabled": False}))
        self.assertEqual(body["bundles"], [])

    def test_a_package_with_no_services_is_hidden(self):
        self.assertEqual(self.tab([package_row(published_snapshot=snap(items=[]))])["bundles"], [])

    def test_ordered_by_the_name_the_customer_sees(self):
        rows = [package_row(PACKAGE_2, published_snapshot=snap(name="beta")),
                package_row(PACKAGE, published_snapshot=snap(name="Alpha"))]
        self.assertEqual([b["name"] for b in self.tab(rows)["bundles"]], ["Alpha", "beta"])

    def test_off_the_tab_reads_as_before(self):
        with mock.patch("apps.salons.views.live_salon_packages") as live:
            response = OldTabSeams.tab(self, [old_tab_row()])
        live.assert_not_called()
        self.assertEqual(response.json()["bundles"][0]["name"], "Groom and Go")


class OldTabCanBookTests(OldTabSeams, Reads, SimpleTestCase):
    """
    PACKAGE_BOOKING_V1 with the tab still on its old rules
    (PACKAGES_LIVE_RULES_V1 off): the rows are as before, and `can_book` is
    the live rule's answer for each row's id, never a draft's.
    """

    def tab_with_live(self, rows, live, **reads):
        with ExitStack() as stack:
            self.live_read = stack.enter_context(mock.patch(
                "apps.salons.views.live_salon_packages", return_value=list(live)))
            self.reads(stack, **reads)
            return self.tab(rows).json()

    def test_off_no_key_and_no_live_read(self):
        with override_settings(PACKAGE_BOOKING_V1=False):
            body = self.tab_with_live([old_tab_row()], [package_row()])
        self.assertNotIn("can_book", body["bundles"][0])
        self.live_read.assert_not_called()

    def test_on_each_row_is_the_live_rule_for_its_id(self):
        live = [package_row(PACKAGE),
                package_row(PACKAGE_2, published_snapshot=snap([snap_item(CUT, LONG_HAIR)]))]
        old = [old_tab_row(PACKAGE), old_tab_row(PACKAGE_2, name="B"),
               old_tab_row(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3"), name="C")]
        with override_settings(PACKAGE_BOOKING_V1=True):
            body = self.tab_with_live(old, live, variants={LONG_HAIR: variant()})
        # Bookable live; pinned to a variant live; not live at all.
        self.assertEqual([b["can_book"] for b in body["bundles"]], [True, False, False])
        # The rows themselves are the old tab's, untouched.
        self.assertEqual(body["bundles"][0]["name"], "Groom and Go")
        self.assertNotIn("services", body["bundles"][0])
        self.services_read.assert_called_once()


class MenuForPackagesTests(SimpleTestCase):
    """menu.py, as packages.py reads it: dict rows, and the price in minor units."""

    def test_is_on_menu_takes_a_dict_with_the_same_three_rules(self):
        self.assertTrue(menu.is_on_menu(service(CUT, "Cut", 1, 1)))
        for change in ({"status": "HIDDEN"}, {"deleted_at": "x"}, {"online_booking_enabled": False}):
            with self.subTest(**change):
                self.assertFalse(menu.is_on_menu(service(CUT, "Cut", 1, 1, **change)))

    def test_service_price_is_service_price_minor_in_major(self):
        for branch in (None, 0, 15000):
            with self.subTest(branch=branch):
                row = service(CUT, "Cut", 19900, 30, branch_price_minor=branch)
                minor = menu.service_price_minor(row)
                self.assertEqual(minor, 19900 if branch is None else branch)
                self.assertEqual(menu.service_price(row), Decimal(minor) / 100)

    def test_offered_at_branch_is_the_platform_s_rule(self):
        # The cases of gostyle-platform's branch-visibility.spec.ts, in its order.
        cat = uuid.uuid4()
        for name, row, offered in (
            ("the service's own row wins", {"branch_scoped": True, "category_0_id": cat,
                                            "branch_available": True,
                                            "category_branch_available": False}, True),
            ("even against a yes from the category", {"category_0_id": cat,
                                                      "branch_available": False,
                                                      "category_branch_available": True}, False),
            ("branch-scoped, no row", {"branch_scoped": True, "category_0_id": cat}, False),
            ("branch-scoped, the category says yes", {"branch_scoped": True, "category_0_id": cat,
                                                      "category_branch_available": True}, False),
            ("the category says no", {"category_0_id": cat, "category_branch_available": False}, False),
            ("the category says yes", {"category_0_id": cat, "category_branch_available": True}, True),
            ("nothing said", {"category_0_id": cat}, True),
            ("no category", {}, True),
            ("a stray category row, no category", {"category_branch_available": False}, True),
        ):
            with self.subTest(name):
                row = service(CUT, "Cut", 1, 1, **row)
                self.assertIs(menu.offered_at_branch(row), offered)
                self.assertIs(menu.offered_at_branch(types.SimpleNamespace(**row)), offered)



# ---------------------------------------------------------------------------
# P3: GET /salon/<salon_id>/package/<package_id>
# ---------------------------------------------------------------------------


def url(salon=SALON, package=PACKAGE):
    return f"/api/v1/salon/{salon}/package/{package}"


class DetailSeams(Reads):
    """
    The view with its reads mocked: the salon, the package, and
    package_lines' three reads (package_lines itself runs for real).
    """

    def call(self, path=None, *, salon=True, package=True, drafts=(), svc=None,
             variants=None, **headers):
        found = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH) if salon else None
        if package is True:
            package = package_row()
        with ExitStack() as stack:
            self.salon_lookup = stack.enter_context(mock.patch(
                "apps.salons.package_detail_views.salon_profile", return_value=found))
            self.package_lookup = stack.enter_context(mock.patch(
                "apps.salons.package_detail_views.package_for_salon", return_value=package or None))
            self.reads(stack, drafts=drafts, svc=svc, variants=variants)
            return self.client.get(path or url(), **headers)


class DetailRouteTests(DetailSeams, SimpleTestCase):
    """What the endpoint answers, through the URL layer."""

    def assert_our_404(self, response, detail):
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {
            "detail": detail,
            "code": "not_found",
            "errors": [{"field": None, "code": "not_found", "message": detail}],
        })

    def test_a_package_of_this_salon_answers_200_without_a_token(self):
        response = self.call()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], str(PACKAGE))
        self.assertEqual(response.json()["salon_id"], str(SALON))

    def test_the_selectors_get_the_parsed_ids(self):
        self.call()
        self.salon_lookup.assert_called_once_with(SALON)
        (salon, package_id), _ = self.package_lookup.call_args
        self.assertEqual(salon.id, SALON)
        self.assertEqual(package_id, PACKAGE)

    def test_ids_that_are_not_uuids_are_our_json_404(self):
        self.assert_our_404(self.call(url(salon="not-a-uuid")), NO_SALON)
        self.salon_lookup.assert_not_called()
        self.assert_our_404(self.call(url(package="also-not")), NO_PACKAGE)
        self.package_lookup.assert_not_called()

    def test_other_uuid_spellings_are_404(self):
        for spelling in (PACKAGE.hex, "{%s}" % PACKAGE, "urn:uuid:%s" % PACKAGE, str(PACKAGE)[:-1]):
            with self.subTest(spelling=spelling):
                self.assert_our_404(self.call(url(package=spelling)), NO_PACKAGE)

    def test_an_uppercase_uuid_is_accepted(self):
        response = self.call(url(salon=str(SALON).upper(), package=str(PACKAGE).upper()))
        self.assertEqual(response.status_code, 200)

    def test_an_unknown_salon_is_404(self):
        self.assert_our_404(self.call(salon=False), NO_SALON)
        self.package_lookup.assert_not_called()

    def test_a_package_not_found_for_this_salon_is_404(self):
        # Another business's package, one never on sale, or no such id: the
        # selector answers None for all three (PackageSelectorTests).
        self.assert_our_404(self.call(package=False), NO_PACKAGE)

    def test_a_bad_token_is_401_even_on_this_public_route(self):
        response = self.call(HTTP_AUTHORIZATION="Bearer not.a.token")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "not_authenticated")
        self.salon_lookup.assert_not_called()

    def test_only_get(self):
        with mock.patch("apps.salons.package_detail_views.salon_profile"):
            response = self.client.post(url())
        self.assertEqual(response.status_code, 405)


class DetailUrlTests(SimpleTestCase):
    def test_the_route_has_a_name(self):
        self.assertEqual(
            reverse("salon-package-detail", kwargs={"salon_id": SALON, "package_id": PACKAGE}),
            url(),
        )

    def test_any_segment_reaches_the_view(self):
        match = resolve(url(salon="nope", package="nope"))
        self.assertIs(match.func.view_class, SalonPackageDetailView)
        self.assertEqual(match.kwargs, {"salon_id": "nope", "package_id": "nope"})

    def test_the_packages_tab_route_is_unchanged(self):
        match = resolve(tab_url())
        self.assertIs(match.func.view_class, SalonPackagesView)
        self.assertEqual(match.kwargs, {"salon_id": SALON})


class DetailFieldsTests(DetailSeams, SimpleTestCase):
    """Every field, from the published content (F2) and the services here."""

    def test_the_whole_answer(self):
        self.assertEqual(self.call().json(), {
            "id": str(PACKAGE),
            "salon_id": str(SALON),
            "name": "The Full Groom",
            "description": "The complete reset: cut, beard, hot towel.",
            "price": 460.0,
            "price_before": 689.0,
            "save_amount": 229.0,
            "save_percent": 33,
            "duration_minutes": 75,
            "is_active": True,
            "includes": [
                {"service_id": str(CUT), "name": "The Gentleman's Cut", "price": 199.0, "quantity": 1},
                {"service_id": str(BEARD), "name": "Beard Trim & Shape", "price": 230.0, "quantity": 1},
                {"service_id": str(TOWEL), "name": "Hot Towel Shave", "price": 260.0, "quantity": 1},
            ],
            "timeline": [
                {"service_id": str(CUT), "name": "The Gentleman's Cut",
                 "offset_minutes": 0, "duration_minutes": 30},
                {"service_id": str(BEARD), "name": "Beard Trim & Shape",
                 "offset_minutes": 30, "duration_minutes": 20},
                {"service_id": str(TOWEL), "name": "Hot Towel Shave",
                 "offset_minutes": 50, "duration_minutes": 25},
            ],
            "terms": "Free to cancel up to 24 hours before.",
            "theme": "gold",
        })

    def test_the_contract_s_keys_in_its_order_then_theme(self):
        contract = ["id", "salon_id", "name", "description", "price", "price_before",
                    "save_amount", "save_percent", "duration_minutes", "is_active",
                    "includes", "timeline", "terms"]
        self.assertEqual(list(self.call().json()), contract + ["theme"])

    def test_can_book_only_with_package_booking_v1_after_theme(self):
        with override_settings(PACKAGE_BOOKING_V1=False):
            off = self.call().json()
        with override_settings(PACKAGE_BOOKING_V1=True):
            on = self.call().json()
        self.assertNotIn("can_book", off)
        self.assertEqual(list(on), list(off) + ["can_book"])
        self.assertIs(on["can_book"], True)
        # Nothing else moves.
        self.assertEqual({k: v for k, v in on.items() if k != "can_book"}, off)

    def test_can_book_is_false_where_booking_api_would_not_sell_it(self):
        cases = {
            "pulled from sale": dict(package=package_row(status="HIDDEN")),
            "a service off at this branch": dict(svc=services(cut={"branch_available": False})),
            "pinned to a variant": dict(
                package=package_row(published_snapshot=snap([snap_item(CUT, LONG_HAIR)])),
                variants={LONG_HAIR: variant()}),
            "free": dict(package=package_row(published_snapshot=snap(priceMinor=0))),
        }
        for name, kwargs in cases.items():
            with self.subTest(name), override_settings(PACKAGE_BOOKING_V1=True):
                self.assertIs(self.call(**kwargs).json()["can_book"], False)
        # Only the first is inactive: the screen still says on sale for the rest.
        with override_settings(PACKAGE_BOOKING_V1=True):
            self.assertIs(self.call(**cases["a service off at this branch"]).json()["is_active"], True)

    def test_a_pulled_package_still_opens_inactive_with_every_field(self):
        for change in ({"status": "HIDDEN"}, {"status": "ARCHIVED"},
                       {"deleted_at": "2026-10-01"}, {"online_booking_enabled": False},
                       {"branch_row": False}):
            with self.subTest(**change):
                body = self.call(package=package_row(**change)).json()
                self.assertFalse(body["is_active"])
                self.assertEqual(body["price"], 460.0)
                self.assertEqual(len(body["includes"]), 3)

    def test_a_service_off_the_menu_makes_it_inactive(self):
        body = self.call(svc=services(towel={"deleted_at": "2026-10-01"})).json()
        self.assertFalse(body["is_active"])
        # Still listed: the customer sees what the package holds.
        self.assertEqual(len(body["includes"]), 3)

    def test_a_service_not_found_is_left_out_and_inactive(self):
        rows = services()
        del rows[BEARD]
        body = self.call(svc=rows).json()
        self.assertFalse(body["is_active"])
        self.assertEqual([i["service_id"] for i in body["includes"]], [str(CUT), str(TOWEL)])

    def test_no_saving_hides_all_three(self):
        body = self.call(package=package_row(published_snapshot=snap(priceMinor=70000))).json()
        self.assertEqual((body["price_before"], body["save_amount"], body["save_percent"]),
                         (None, None, None))
        self.assertEqual(body["price"], 700.0)

    def test_blank_text_is_null(self):
        body = self.call(package=package_row(cancellation_policy="  ",
                                             published_snapshot=snap(description=""))).json()
        self.assertIsNone(body["terms"])
        self.assertIsNone(body["description"])

    def test_never_published_reads_the_draft(self):
        drafts = [{"package_id": PACKAGE, **item(CUT, quantity=2)}]
        body = self.call(package=package_row(published_snapshot=None, published_version=None),
                         drafts=drafts).json()
        self.assertEqual(body["name"], "The Full Groom (draft)")
        self.assertEqual(body["price"], 500.0)
        self.assertEqual(body["includes"], [
            {"service_id": str(CUT), "name": "The Gentleman's Cut", "price": 199.0, "quantity": 2},
        ])
        self.assertIsNone(body["price_before"])  # 398 is less than 500
        self.assertEqual(body["duration_minutes"], 60)

    def test_a_parallel_package(self):
        body = self.call(package=package_row(scheduling="PARALLEL")).json()
        self.assertEqual([s["offset_minutes"] for s in body["timeline"]], [0, 0, 0])

    def test_reads(self):
        self.call()
        self.services_read.assert_called_once()
        self.drafts_read.assert_not_called()
        self.variants_read.assert_not_called()


class TabAndDetailAgreeTests(Reads, SimpleTestCase):
    """With PACKAGES_LIVE_RULES_V1 on, a tab row and its detail show the same numbers."""

    salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH)

    def test_same_numbers(self):
        cases = [
            (package_row(), services()),
            (package_row(published_snapshot=snap([snap_item(CUT, LONG_HAIR, 2)])), services()),
            (package_row(published_snapshot=snap(priceMinor=90000)), services()),
            (package_row(), services(cut={"branch_price_minor": 15000})),
        ]
        keys = ("name", "description", "price", "price_before", "save_amount",
                "duration_minutes", "theme")
        for pkg, svc in cases:
            with self.subTest(pkg=pkg.published_snapshot), ExitStack() as stack:
                self.reads(stack, svc=svc, variants={LONG_HAIR: variant()})
                content, rows, complete = selectors.package_lines(self.salon, [pkg])[pkg.id]
                tab = packages.tab_row(pkg, content, rows)
                detail = packages.detail(self.salon, pkg, content, rows, complete)
                self.assertEqual({k: tab[k] for k in keys}, {k: detail[k] for k in keys})
                self.assertTrue(detail["is_active"])


def example_rows():
    """The rows EXAMPLE was answered from (the seeded salon's package, published
    with three services), so a test can ask the view again."""
    scalp = uuid.UUID("66666666-6666-6666-6666-666666666663")
    items = [snap_item(CUT), snap_item(scalp), snap_item(TOWEL)]
    package = package_row(
        name="Groom and Go",
        description="Leave sharp, stay sharp.",
        price_minor=37000,
        branch_row=True,
        highlights=["Gentleman's Cut", "Hot Towel Shave", "Beard oil to take home"],
        cancellation_policy="Free to cancel up to 24 hours before your visit. After that, the deposit is kept.",
        published_snapshot=snap(
            items,
            name="Groom and Go",
            description="Leave sharp, stay sharp: a cut, a scalp treatment and a hot towel shave.",
            priceMinor=52000,
        ),
    )
    svc = {
        CUT: service(CUT, "The Gentleman's Cut", 19900, 30),
        scalp: service(scalp, "Scalp Treatment", 18000, 20, branch_price_minor=15000),
        TOWEL: service(TOWEL, "Hot Towel Shave", 26000, 35),
    }
    return dict(package=package, svc=svc)


class ExampleIsRealTests(DetailSeams, SimpleTestCase):
    """
    EXAMPLE (the OpenAPI example and docs/PACKAGE_DETAIL_API.md's) was taken
    from the running code. Asked again from the same rows, the view must
    answer it exactly, or the documents have drifted from the code.
    """

    def test_the_view_answers_the_example(self):
        response = self.call(**example_rows())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), EXAMPLE)


class DetailOpenApiTests(DetailSeams, SimpleTestCase):
    """The route's OpenAPI entry describes what the view answers."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def component(self, name):
        return self.schema["components"]["schemas"][name]

    def operation(self):
        return self.schema["paths"]["/api/v1/salon/{salon_id}/package/{package_id}"]["get"]

    def test_the_200_is_the_response_shape_with_the_example(self):
        content = self.operation()["responses"]["200"]["content"]["application/json"]
        self.assertEqual(content["schema"]["$ref"], "#/components/schemas/SalonPackageDetailResponse")
        self.assertEqual(content["examples"]["APackageOnSale"]["value"], EXAMPLE)

    def test_the_shape_has_exactly_the_fields_the_view_answers(self):
        shape = self.component("SalonPackageDetailResponse")
        with override_settings(PACKAGE_BOOKING_V1=True):
            on = list(self.call().json())
        self.assertEqual(list(shape["properties"]), on)
        # Off, every one of them but can_book, which the shape marks optional.
        with override_settings(PACKAGE_BOOKING_V1=False):
            self.assertEqual(list(self.call().json()), [k for k in on if k != "can_book"])
        self.assertEqual(sorted(shape["required"]), sorted(k for k in on if k != "can_book"))

    def test_the_nested_shapes_have_exactly_the_example_s_fields(self):
        for name, sample in (("SalonPackageDetailInclude", EXAMPLE["includes"][0]),
                             ("SalonPackageDetailStep", EXAMPLE["timeline"][0])):
            with self.subTest(name=name):
                self.assertEqual(list(self.component(name)["properties"]), list(sample))

    def test_the_404_example_is_the_real_body(self):
        example = self.operation()["responses"]["404"]["content"]["application/json"]
        self.assertEqual(example["examples"]["NoSuchPackage"]["value"], self.call(package=False).json())


FE_DOC = settings.BASE_DIR / "docs" / "PACKAGE_DETAIL_API.md"


@unittest.skipUnless(FE_DOC.exists(), "docs/ is not in the Docker image")
class FeDocTests(DetailSeams, SimpleTestCase):
    """docs/PACKAGE_DETAIL_API.md promises every example is a real answer."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        text = FE_DOC.read_text(encoding="utf-8")
        cls.blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", text, re.S)]

    def test_the_full_example_is_the_real_answer(self):
        self.assertEqual(self.blocks[0], EXAMPLE)

    def test_the_404_bodies_are_the_real_ones(self):
        self.assertIn(self.call(package=False).json(), self.blocks)
        self.assertIn(self.call(salon=False).json(), self.blocks)

    def test_no_other_json_blocks(self):
        self.assertEqual(len(self.blocks), 3)

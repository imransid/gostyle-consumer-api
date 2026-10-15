"""
GET /api/v1/salon/<salon_id>/package/<package_id>, the Package Details screen
(docs/package-detail-fe-contract.md, docs/PACKAGE_DETAIL_AUDIT.md section 7),
and the Packages tab it is opened from.

P1: the tab pinned as it answers today, before anything moves.

P2: the platform's package rules in one place (packages.py, the package
selectors), and the tab on them behind PACKAGES_LIVE_RULES_V1.

The selectors are mocked; no database.
"""

import types
import uuid
from contextlib import ExitStack
from decimal import Decimal
from unittest import mock

from django.db.models import OuterRef, Q
from django.test import SimpleTestCase, override_settings

from apps.salons import menu, packages, selectors

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
        CUT: service(CUT, "The Gentleman's Cut", 19900, 30),
        BEARD: service(BEARD, "Beard Trim & Shape", 23000, 20),
        TOWEL: service(TOWEL, "Hot Towel Shave", 26000, 25),
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
            {"service_id": TOWEL, "name": "Hot Towel Shave", "unit_minor": 26000,
             "minutes": 25, "quantity": 1, "bookable": True},
            {"service_id": CUT, "name": "The Gentleman's Cut", "unit_minor": 19900,
             "minutes": 30, "quantity": 1, "bookable": True},
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
            }],
        })

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

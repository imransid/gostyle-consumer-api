"""
Booking a package as one visit at its price (docs/BOOKING_CREATE_API.md,
"Booking a package"), the steps after the Package Details screen:

  * the slot search with a package (GET /booking/nearest-available,
    PACKAGE_BOOKING_V1): the package is searched as its parts, each as many
    times as it holds it, by one stylist for all of them.

The selectors are mocked; no database. The package fixtures are
test_package_detail's, so both modules read the same packages.
"""

import types
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.db.models import OuterRef, Q
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import packages, selectors
from apps.salons.test_group_booking import Customer
from apps.salons.test_package_detail import (
    BEARD,
    BRANCH,
    CUT,
    LONG_HAIR,
    PACKAGE,
    PACKAGE_2,
    SALON,
    TENANT,
    TOWEL,
    Reads,
    item,
    package_row,
    services,
    snap,
    snap_item,
    variant,
)
from apps.salons.views import PACKAGE_NOT_BOOKABLE, NearestAvailableView

MAYA = uuid.UUID("99999999-9999-9999-9999-999999999990")
ANYA = uuid.UUID("99999999-9999-9999-9999-999999999991")

# What each service takes in the diary: the stage sums service_timing_rows
# answers (stage_minutes), which win over the catalogue's duration.
TIMING = {
    CUT: {"id": CUT, "name": "The Gentleman's Cut", "price_minor": 19900, "branch_price_minor": None,
          "duration_minutes": 30, "stage_minutes": 35, "lead_time_minutes": None},
    BEARD: {"id": BEARD, "name": "Beard Trim & Shape", "price_minor": 23000, "branch_price_minor": None,
            "duration_minutes": 20, "stage_minutes": None, "lead_time_minutes": 60},
    TOWEL: {"id": TOWEL, "name": "Hot Towel Shave", "price_minor": 26000, "branch_price_minor": None,
            "duration_minutes": 25, "stage_minutes": 25, "lead_time_minutes": None},
}

# Who can do what: Maya everything, Anya the cut alone.
SKILLS = {MAYA: {CUT, BEARD, TOWEL}, ANYA: {CUT}}

# Far enough ahead that "not in the past" never cuts a start off.
WINDOW = "from=2030-01-15T10:00:00%2B04:00&to=2030-01-15T20:00:00%2B04:00"


def stylist(staff_id, first_name):
    return types.SimpleNamespace(id=staff_id, first_name=first_name, last_name=None,
                                 job_title=None, avatar_url=None)


def a_two_cut_package(package_id=PACKAGE):
    """A package of "2 x The Gentleman's Cut" and a hot towel shave."""
    return package_row(package_id, published_snapshot=snap([snap_item(CUT, quantity=2), snap_item(TOWEL)]))


class SlotSearchSeams(Reads):
    """
    NearestAvailableView with every read mocked: the salon, its packages
    (packages_for_salon), package_lines' three reads, the timing, and what
    _offers asks (hours, roster, skills, busy time). _offers itself runs.
    """

    def search(self, ids, *, packages=(), flag=True, **reads):
        salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH,
                                      branch_timezone="Asia/Dubai")
        query = f"{WINDOW}&service_ids={','.join(str(i) for i in ids)}"
        request = APIRequestFactory().get(f"/api/v1/booking/nearest-available/{SALON}?{query}")
        force_authenticate(request, user=Customer())
        with ExitStack() as stack, override_settings(PACKAGE_BOOKING_V1=flag):
            stack.enter_context(mock.patch("apps.salons.views.salon_profile", return_value=salon))
            self.packages_read = stack.enter_context(mock.patch(
                "apps.salons.views.packages_for_salon", return_value=list(packages)))
            self.reads(stack, **reads)
            self.timing_read = stack.enter_context(mock.patch(
                "apps.salons.views.service_timing_rows",
                side_effect=lambda _salon, asked: [TIMING[i] for i in asked if i in TIMING]))
            stack.enter_context(mock.patch(
                "apps.salons.views._open_span",
                side_effect=lambda _s, _tz, _day, start: (start + timedelta(hours=9),
                                                          start + timedelta(hours=22))))
            stack.enter_context(mock.patch(
                "apps.salons.views.salon_stylists",
                return_value=[stylist(MAYA, "Maya"), stylist(ANYA, "Anya")]))
            self.coverage = stack.enter_context(mock.patch(
                "apps.salons.views.stylist_service_coverage",
                side_effect=lambda _salon, asked, staff_ids: {
                    s: [i for i in asked if i in SKILLS[s]] for s in staff_ids
                    if any(i in SKILLS[s] for i in asked)
                }))
            stack.enter_context(mock.patch("apps.salons.views.shift_rows", return_value=[
                {"staff_member_id": s, "start_time": "09:00", "end_time": "22:00", "break_time": None}
                for s in (MAYA, ANYA)
            ]))
            self.busy = stack.enter_context(mock.patch(
                "apps.salons.views.busy_intervals", return_value=([], None)))
            return NearestAvailableView.as_view()(request, salon_id=SALON)

    def lengths(self, response):
        """Each offer's minutes, start to end."""
        return {
            (datetime.fromisoformat(o["end"]) - datetime.fromisoformat(o["start"])).seconds // 60
            for o in response.data["offers"]
        }


class SlotSearchWithAPackageTests(SlotSearchSeams, SimpleTestCase):
    """GET /booking/nearest-available with a package id, PACKAGE_BOOKING_V1 on."""

    def test_a_package_is_searched_as_its_parts_each_per_quantity(self):
        response = self.search([PACKAGE], packages=[a_two_cut_package()])
        self.assertEqual(response.status_code, 200)
        # Two cuts and a shave: 35 + 35 + 25, not one cut's 35 + 25.
        self.assertEqual(response.data["duration_min"], 95)
        self.assertEqual(self.lengths(response), {95})
        # Timing and skills are asked once per service.
        self.timing_read.assert_called_once_with(mock.ANY, [CUT, TOWEL])
        self.assertEqual(self.coverage.call_args.args[1], [CUT, TOWEL])

    def test_its_parts_are_the_live_content_the_details_screen_shows(self):
        # The snapshot's items, not the draft's (the draft has no items here).
        draft_items = [{"package_id": PACKAGE, **item(BEARD)}]
        self.search([PACKAGE], packages=[a_two_cut_package()], drafts=draft_items)
        self.drafts_read.assert_not_called()
        self.assertEqual(self.coverage.call_args.args[1], [CUT, TOWEL])

    def test_one_stylist_must_do_every_part(self):
        # Anya can do the cuts and not the shave: covering part of the visit
        # is covering none of it.
        response = self.search([PACKAGE], packages=[a_two_cut_package()])
        self.assertTrue(response.data["offers"])
        self.assertEqual({o["stylist"]["id"] for o in response.data["offers"]}, {str(MAYA)})

    def test_a_package_next_to_hand_picked_services_in_the_order_asked(self):
        response = self.search([BEARD, PACKAGE], packages=[a_two_cut_package()])
        self.assertEqual(response.data["duration_min"], 20 + 95)
        self.assertEqual(self.coverage.call_args.args[1], [BEARD, CUT, TOWEL])

    def test_a_hand_picked_service_that_is_also_a_part_counts_twice(self):
        # booking-api books the cut the customer picked and the package's two.
        response = self.search([CUT, PACKAGE], packages=[a_two_cut_package()])
        self.assertEqual(response.data["duration_min"], 35 * 3 + 25)
        self.assertEqual(self.coverage.call_args.args[1], [CUT, TOWEL])

    def test_the_longest_notice_of_any_part_applies(self):
        pkg = package_row(PACKAGE, published_snapshot=snap([snap_item(BEARD)]))
        with mock.patch("apps.salons.views._offers", return_value=[]) as offers:
            self.search([PACKAGE], packages=[pkg])
        self.assertEqual(offers.call_args.kwargs["lead_minutes"], 60)

    def test_ids_that_are_no_package_here_are_left_as_sent(self):
        # Asked about all of them in one read; nothing found, nothing changes.
        stranger = uuid.uuid4()
        response = self.search([CUT, stranger], packages=[])
        self.packages_read.assert_called_once_with(mock.ANY, [CUT, stranger])
        self.services_read.assert_not_called()
        self.assertEqual(response.data["duration_min"], 35)
        # An id this salon does not offer still matches nobody, as before.
        self.assertEqual(response.data["offers"], [])

    def test_two_packages_one_read(self):
        second = package_row(PACKAGE_2, published_snapshot=snap([snap_item(BEARD)]))
        response = self.search([PACKAGE, PACKAGE_2], packages=[a_two_cut_package(), second])
        self.assertEqual(response.data["duration_min"], 95 + 20)
        self.services_read.assert_called_once()

    def test_a_package_that_cannot_be_booked_is_422_package_not_bookable(self):
        cases = {
            "pulled from sale": dict(packages=[package_row(status="HIDDEN")]),
            "not sold at this branch": dict(packages=[package_row(branch_row=False)]),
            "a part off at this branch": dict(packages=[package_row()],
                                              svc=services(towel={"branch_available": False})),
            "pinned to a variant": dict(
                packages=[package_row(published_snapshot=snap([snap_item(CUT, LONG_HAIR)]))],
                variants={LONG_HAIR: variant()}),
            "free": dict(packages=[package_row(published_snapshot=snap(priceMinor=0))]),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                response = self.search([PACKAGE], **kwargs)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.data, {
                    "detail": "Please correct the highlighted fields.",
                    "code": "validation_error",
                    "errors": [{
                        "field": "service_ids",
                        "code": "package_not_bookable",
                        "message": PACKAGE_NOT_BOOKABLE,
                    }],
                })
                # Refused before anything is searched or anyone is asked.
                self.timing_read.assert_not_called()
                self.busy.assert_not_called()

    def test_the_refusal_is_the_same_rule_as_can_book(self):
        pkg = package_row(published_snapshot=snap([snap_item(CUT, LONG_HAIR)]))
        content = packages.live_content(pkg)
        rows, complete = packages.lines(content["items"], services(), {LONG_HAIR: variant()})
        self.assertFalse(packages.can_book(pkg, content, rows, complete))
        response = self.search([PACKAGE], packages=[pkg], variants={LONG_HAIR: variant()})
        self.assertEqual(response.status_code, 422)


class SlotSearchSwitchedOffTests(SlotSearchSeams, SimpleTestCase):
    """PACKAGE_BOOKING_V1 off: exactly what the endpoint answered before."""

    def test_a_package_id_is_sent_on_as_it_came(self):
        response = self.search([PACKAGE], packages=[a_two_cut_package()], flag=False)
        self.packages_read.assert_not_called()
        self.timing_read.assert_called_once_with(mock.ANY, [PACKAGE])
        # No service of this salon: no minutes and nobody to offer.
        self.assertEqual(response.data, {"duration_min": 0, "offers": []})

    def test_hand_picked_services_are_counted_as_before(self):
        response = self.search([CUT, BEARD], flag=False)
        self.assertEqual(response.data["duration_min"], 35 + 20)
        self.assertEqual(self.lengths(response), {55})


class PartsTests(SimpleTestCase):
    """packages.parts: the services one booking of the package performs."""

    def test_each_line_as_many_times_as_it_holds_it_in_order(self):
        rows, _ = packages.lines([item(TOWEL), item(CUT, quantity=2), item(BEARD)], services(), {})
        self.assertEqual(packages.parts(rows), [TOWEL, CUT, CUT, BEARD])

    def test_nothing_for_no_lines(self):
        self.assertEqual(packages.parts([]), [])


class PackagesForSalonTests(SimpleTestCase):
    """selectors.packages_for_salon: package_for_salon's rule, for several ids."""

    salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=BRANCH)

    def test_this_tenant_s_ever_on_sale_with_the_branch_row(self):
        with mock.patch.object(selectors, "ServicePackage") as package, \
                mock.patch.object(selectors, "_package_branch_row", return_value="row") as row:
            chain = package.objects.filter.return_value.filter.return_value
            chain.annotate.return_value = ["found"]
            answer = selectors.packages_for_salon(self.salon, [PACKAGE, CUT])
        package.objects.filter.assert_called_once_with(id__in=[PACKAGE, CUT], tenant_id=TENANT)
        package.objects.filter.return_value.filter.assert_called_once_with(
            Q(published_version__isnull=False) | Q(status__in=("PUBLISHED", "HIDDEN", "ARCHIVED"))
        )
        row.assert_called_once_with(BRANCH)
        chain.annotate.assert_called_once_with(branch_row="row")
        self.assertEqual(answer, ["found"])

    def test_no_ids_no_read(self):
        with mock.patch.object(selectors, "ServicePackage") as package:
            self.assertEqual(selectors.packages_for_salon(self.salon, []), [])
        package.objects.filter.assert_not_called()

    def test_the_branch_row_is_this_branch_s(self):
        with mock.patch.object(selectors, "ServicePackageBranchAvailability") as rows:
            selectors._package_branch_row(BRANCH)
        rows.objects.filter.assert_called_once_with(package_id=OuterRef("pk"), branch_id=BRANCH)

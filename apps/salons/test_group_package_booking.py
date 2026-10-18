"""
A party member may book a package (PACKAGE_BOOKING_V1): a package id among a
member's `service_ids` (POST /booking/group-availability), or as a line of a
member's `services` (POST /booking/group).

booking-api expands a package itself (party-member.ts), so it is sent the
package id as the app sent it. This service's own checks read each member's
visit, the package as its parts (views._bookable_packages, the slot search's
own expansion): foreign_id and package_not_bookable, the stylist who must do
every part, the party's length (each member's parts end to end, a quantity
counted) and its notice (the longest any part asks for).

Driven through test_group_booking's seams, with the package reads mocked as
test_package_booking mocks them: no database, no network.
"""

import json
import re
import unittest
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import booking_api
from apps.salons import tests as list_tests
from apps.salons.group_serializers import (
    PACKAGES_AS_A_LINE,
    PACKAGES_REFUSED,
    GroupBookingRequestSerializer,
)
from apps.salons.group_views import GroupAvailabilityView, GroupBookingCreateView
from apps.salons.test_group_booking import (
    ANYA,
    CUT,
    GROUP_ID,
    HOLD_ID,
    MAYA,
    NAILS,
    RANA,
    STAFF,
    USER_ID,
    Customer,
    ViewSeams,
    at,
    availability_body,
    booking_body,
    confirmed_answer,
    party,
    plan_entry,
)
from apps.salons.test_group_booking_v2 import ANSWER, ReadSeams
from apps.salons.test_package_detail import (
    LONG_HAIR,
    PACKAGE,
    PACKAGE_2,
    Reads,
    package_row,
    service,
    snap,
    snap_item,
    variant,
)
from apps.salons.views import PACKAGE_NOT_BOOKABLE, BookingListView

CUT_ID, NAILS_ID = uuid.UUID(CUT), uuid.UUID(NAILS)

# What the package reads find (selectors.package_service_rows): this salon's
# cut and nails, on the menu and offered at its branch.
PACKAGE_SERVICES = {
    CUT_ID: service(CUT_ID, "Cut", 12000, 45),
    NAILS_ID: service(NAILS_ID, "Nails", 17500, 50),
}


def a_package(items, package_id=PACKAGE, price_minor=37000, **overrides):
    """A package of this salon's business, on sale here, its snapshot holding `items`."""
    return package_row(
        package_id, published_snapshot=snap(items, priceMinor=price_minor), **overrides
    )


def groom(**overrides):
    """
    "2 x Cut" and the nails, AED 370: 45 + 45 + 60 minutes in the diary, and
    the nails' 30 minutes' notice.
    """
    return a_package([snap_item(CUT_ID, quantity=2), snap_item(NAILS_ID)], **overrides)


def cuts(quantity, package_id=PACKAGE_2):
    """`quantity` cuts, 45 minutes each, and no notice."""
    return a_package([snap_item(CUT_ID, quantity=quantity)], package_id=package_id,
                     price_minor=10000 * quantity)


def visit_of(members_by_ref, body):
    """availability_body / booking_body with members' ids replaced: {ref: [id, ...]}."""
    for m in body["members"]:
        if m["ref"] in members_by_ref:
            ids = [str(i) for i in members_by_ref[m["ref"]]]
            if "services" in m:
                m["services"] = [{"id": i, "amount": 120} for i in ids]
            else:
                m["service_ids"] = ids
    return body


def unbookable_cases():
    """packages.can_book's refusals, each as the seams take it."""
    return {
        "pulled from sale": dict(packages=[groom(status="HIDDEN")]),
        "not sold at this branch": dict(packages=[groom(branch_row=False)]),
        "a part off at this branch": dict(
            packages=[groom()],
            svc={**PACKAGE_SERVICES, NAILS_ID: {**PACKAGE_SERVICES[NAILS_ID], "branch_available": False}}),
        "pinned to a variant": dict(
            packages=[a_package([snap_item(CUT_ID, LONG_HAIR)])],
            variants={LONG_HAIR: variant(service_id=CUT_ID)}),
        "free": dict(packages=[a_package([snap_item(CUT_ID)], price_minor=0)]),
        # booking-api's catalogue refuses the whole basket for it (409
        # BOOKING_STATE_INVALID), on availability, hold and create alike.
        "a part priced 0 here": dict(
            packages=[groom()],
            svc={**PACKAGE_SERVICES, NAILS_ID: {**PACKAGE_SERVICES[NAILS_ID], "branch_price_minor": 0}}),
    }


class PackageSeams(Reads, ViewSeams):
    """
    test_group_booking's seams, plus the package reads: packages_for_salon
    (what the expansion asks first) and package_lines' three.
    """

    @contextmanager
    def with_packages(self, packages=(), *, flag=True, svc=None, variants=None, **over):
        with ExitStack() as stack:
            stack.enter_context(override_settings(PACKAGE_BOOKING_V1=flag))
            self.packages_read = stack.enter_context(mock.patch(
                "apps.salons.views.packages_for_salon", return_value=list(packages)))
            self.reads(stack, svc=PACKAGE_SERVICES if svc is None else svc, variants=variants)
            yield stack.enter_context(self.seams(**over))

    def assert_not_bookable(self, response, field):
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["errors"], [
            {"field": field, "code": "package_not_bookable", "message": PACKAGE_NOT_BOOKABLE},
        ])


# ------------------------------------------------------------ availability


class AvailabilityWithAPackageTests(PackageSeams, SimpleTestCase):
    """POST /booking/group-availability, PACKAGE_BOOKING_V1 on."""

    PATH = "/api/v1/booking/group-availability"

    def send(self, ids_by_ref, *, stylists=None):
        body = visit_of(ids_by_ref, availability_body())
        for m in body["members"]:
            if stylists and m["ref"] in stylists:
                m["stylist_id"] = stylists[m["ref"]]
        return self.post(GroupAvailabilityView, self.PATH, body)

    def slots(self, response):
        self.assertEqual(response.status_code, 200, response.data)
        return [slot for band in response.data["bands"] for slot in band["slots"]]

    def test_booking_api_is_asked_about_the_package_id_not_its_parts(self):
        with self.with_packages([groom()]) as s:
            self.slots(self.send({0: [PACKAGE]}))
        people = s.plan_group.call_args.args[0]["participants"]
        self.assertEqual([p["serviceIds"] for p in people], [[NAILS], [str(PACKAGE)], [CUT]])

    def test_the_party_is_as_long_as_the_package_s_parts_end_to_end(self):
        with self.with_packages([groom()]):
            slots = self.slots(self.send({0: [PACKAGE]}))
        # 45 + 45 + 60 minutes must end by 20:00, the engine's close: the last
        # start is 17:30, where nails alone (60) would start until 19:00.
        self.assertEqual(slots[-1]["start"], "2026-10-11T17:30:00+04:00")
        self.assertEqual(len(slots), 18)
        struck = next(s for s in slots if not s["available"])
        self.assertEqual((struck["start"], struck["end"]),
                         ("2026-10-11T09:30:00+04:00", "2026-10-11T12:00:00+04:00"))

    def test_a_package_s_quantity_counts_in_the_party_s_length(self):
        # No nails anywhere: the cuts decide. Two are 90 minutes, three 135.
        for quantity, last in ((2, "18:30"), (3, "17:30")):
            with self.subTest(quantity=quantity), self.with_packages([cuts(quantity)]):
                slots = self.slots(self.send({2: [CUT], 0: [PACKAGE_2]}))
                self.assertEqual(slots[-1]["start"], f"2026-10-11T{last}:00+04:00")

    def test_the_longest_notice_of_any_part_applies(self):
        # 13:50 now. The groom holds the nails (30 minutes' notice): the first
        # start asked is 14:30. Cuts alone need none: 14:00.
        self.NOW = at("13:50")
        for package, package_id, first in ((groom(), PACKAGE, 16 * 60 + 30),
                                           (cuts(2), PACKAGE_2, 16 * 60)):
            with self.subTest(package=package_id), self.with_packages([package]) as s:
                self.slots(self.send({2: [CUT], 0: [package_id]}))
                self.assertEqual(s.plan_group.call_args.args[0]["targetMins"][0], first)

    def test_the_chosen_stylist_must_do_every_part(self):
        # Anya cuts and does no nails: the groom's nails are hers to do.
        with self.with_packages([groom()]) as s:
            response = self.send({2: [CUT], 0: [PACKAGE]}, stylists={2: MAYA, 0: ANYA})
        self.assert_refused(response, "stylist_mismatch", "stylist_id")
        s.plan_group.assert_not_called()
        _, service_ids, staff_ids = s.stylist_service_coverage.call_args.args
        self.assertEqual(service_ids, [CUT_ID, NAILS_ID])
        self.assertEqual(staff_ids, [uuid.UUID(MAYA), uuid.UUID(ANYA)])

    def test_a_stylist_who_does_every_part_is_fine(self):
        with self.with_packages([groom()]):
            self.slots(self.send({2: [CUT], 0: [PACKAGE]}, stylists={2: ANYA, 0: MAYA}))

    def test_a_package_that_cannot_be_booked_is_422_package_not_bookable(self):
        for name, kwargs in unbookable_cases().items():
            with self.subTest(name), self.with_packages(**kwargs) as s:
                response = self.send({0: [PACKAGE]})
                self.assert_not_bookable(response, "service_ids")
                s.service_timing_rows.assert_not_called()
                s.engine_clock.assert_not_called()

    def test_a_package_that_is_not_this_salon_s_is_foreign(self):
        # Another business's package, or a draft never on sale: not found as
        # a package here, so it is the id of no service here.
        with self.with_packages([]) as s:
            response = self.send({0: [PACKAGE]})
        self.assert_refused(response, "foreign_id", "service_ids")
        s.engine_clock.assert_not_called()

    def test_the_whole_party_s_packages_in_one_read(self):
        with self.with_packages([groom(), cuts(2)]) as s:
            self.slots(self.send({2: [CUT], 0: [PACKAGE, CUT], 1: [PACKAGE_2, PACKAGE]}))
        self.packages_read.assert_called_once_with(
            mock.ANY, [CUT, str(PACKAGE), str(PACKAGE_2)])
        self.services_read.assert_called_once()
        # Timing asked once for every service performed.
        self.assertEqual(sorted(s.service_timing_rows.call_args.args[1]), [CUT, NAILS])


class AvailabilityPackageSwitchTests(PackageSeams, SimpleTestCase):
    """The switch: off, a package id is what it was; on, a party of services is too."""

    PATH = "/api/v1/booking/group-availability"

    def send(self, body):
        return self.post(GroupAvailabilityView, self.PATH, body)

    def test_off_a_package_id_is_foreign_and_nothing_is_read(self):
        with self.with_packages([groom()], flag=False) as s:
            response = self.send(visit_of({0: [PACKAGE]}, availability_body()))
        self.assert_refused(response, "foreign_id", "service_ids")
        self.assertEqual(response.data["errors"][0]["message"],
                         "One or more services are not offered by this salon.")
        self.packages_read.assert_not_called()
        s.engine_clock.assert_not_called()

    def test_a_party_of_services_is_answered_the_same_either_way(self):
        answers = {}
        for flag in (False, True):
            with self.with_packages([], flag=flag) as s:
                response = self.send(availability_body())
            answers[flag] = (response.status_code, response.data, s.plan_group.call_args)
        self.assertEqual(answers[True], answers[False])


# ------------------------------------------------------------ create


@override_settings(GROUP_BOOKING_V2=True)
class CreateWithAPackageTests(PackageSeams, SimpleTestCase):
    """POST /booking/group through booking-api's mobile route, PACKAGE_BOOKING_V1 on."""

    PATH = "/api/v1/booking/group"

    def with_packages(self, packages=(), **over):
        over.setdefault("create_group_booking", mock.Mock(return_value=(201, dict(ANSWER))))
        return super().with_packages(packages, **over)

    def send(self, body):
        return self.post(GroupBookingCreateView, self.PATH, body)

    def sent(self, s):
        return s.create_group_booking.call_args.args[0]

    def test_one_member_with_a_package_and_one_without(self):
        body = booking_body()
        body["members"][0]["services"] = [{"id": str(PACKAGE), "amount": 370}]
        with self.with_packages([groom()]) as s:
            response = self.send(body)
        self.assertEqual(response.status_code, 201, response.data)
        sent = self.sent(s)
        # The lines as the app sent them: the package as one line, the
        # hand-picked services as theirs.
        self.assertEqual([m["services"] for m in sent["members"]], [
            [{"id": str(PACKAGE), "amount": 370}],
            [{"id": NAILS, "amount": 120}],
            [{"id": CUT, "amount": 120}],
        ])
        # The checks read every part, each once: the cut and the nails.
        self.assertEqual(sorted(s.service_timing_rows.call_args.args[1]), [CUT, NAILS])

    def test_a_child_with_a_package(self):
        # booking-api halves the package's line for a child, once. Here the
        # line and the app's figures go on as they came, to be checked there.
        body = booking_body()
        body["members"][2]["services"] = [{"id": str(PACKAGE), "amount": 185}]
        with self.with_packages([groom()]) as s:
            response = self.send(body)
        self.assertEqual(response.status_code, 201, response.data)
        rana = self.sent(s)["members"][2]
        self.assertEqual((rana["id"], rana["age_group"], rana["services"]),
                         (RANA, "child", [{"id": str(PACKAGE), "amount": 185}]))
        for key in ("amount_without_tax", "tax_amount", "discount", "total", "due_amount"):
            self.assertEqual(self.sent(s)[key], body[key], key)

    def test_a_package_whose_part_the_chosen_stylist_cannot_do(self):
        body = booking_body()
        dana = body["members"][1]
        dana["services"] = [{"id": str(PACKAGE), "amount": 370}]
        dana["stylist_id"] = ANYA   # cuts, and does no nails
        with self.with_packages([groom()]) as s:
            response = self.send(body)
        self.assert_refused(response, "stylist_mismatch", "stylist_id")
        self.assertEqual(
            response.data["errors"][0]["message"],
            "Dana: that stylist cannot do all of these services. Choose another, "
            "or let the salon assign one.",
        )
        self.assertEqual(s.stylist_service_coverage.call_args.args[1], [CUT_ID, NAILS_ID])
        s.create_group_booking.assert_not_called()

    def test_a_package_s_quantity_counts_in_the_party_s_length(self):
        # At 19:00 the party must end by 20:00, the engine's close. One cut is
        # 45 minutes and fits beside the nails' hour; two are 90, and do not.
        for quantity, status in ((1, 201), (2, 422)):
            body = visit_of({0: [PACKAGE_2]}, booking_body(start_time="2026-10-11T19:00:00+04:00"))
            with self.subTest(quantity=quantity), self.with_packages([cuts(quantity)]) as s:
                response = self.send(body)
                self.assertEqual(response.status_code, status, response.data)
                if status == 422:
                    self.assert_refused(response, "outside_hours", "start_time")
                    s.create_group_booking.assert_not_called()

    def test_the_longest_notice_of_any_part_applies(self):
        # 14:50 now, 15:00 asked, and no nails picked by hand: only a package
        # holding the nails (30 minutes' notice) makes it too soon.
        self.NOW = at("14:50")
        for package, package_id, status in ((groom(), PACKAGE, 422), (cuts(2), PACKAGE_2, 201)):
            body = visit_of({1: [CUT], 0: [package_id]}, booking_body())
            with self.subTest(package=package_id), self.with_packages([package]):
                response = self.send(body)
                self.assertEqual(response.status_code, status, response.data)
                if status == 422:
                    self.assert_refused(response, "too_soon", "start_time")

    def test_a_package_that_cannot_be_booked_is_422_package_not_bookable(self):
        for name, kwargs in unbookable_cases().items():
            body = visit_of({0: [PACKAGE]}, booking_body())
            with self.subTest(name), self.with_packages(**kwargs) as s:
                response = self.send(body)
                self.assert_not_bookable(response, "services")
                s.service_timing_rows.assert_not_called()
                s.create_group_booking.assert_not_called()

    def test_a_package_that_is_not_this_salon_s_is_foreign(self):
        with self.with_packages([]) as s:
            response = self.send(visit_of({0: [PACKAGE]}, booking_body()))
        self.assert_refused(response, "foreign_id", "services")
        s.create_group_booking.assert_not_called()


class CreatePackageSwitchTests(PackageSeams, SimpleTestCase):
    """The switch: off, a package line is what it was, on either create path."""

    PATH = "/api/v1/booking/group"

    def test_off_a_package_line_is_foreign_and_nothing_is_read(self):
        for v2 in (True, False):
            create = mock.Mock(return_value=(201, dict(ANSWER)))
            with self.subTest(GROUP_BOOKING_V2=v2), override_settings(GROUP_BOOKING_V2=v2), \
                    self.with_packages([groom()], flag=False, create_group_booking=create) as s:
                response = self.post(GroupBookingCreateView, self.PATH,
                                     visit_of({0: [PACKAGE]}, booking_body()))
                self.assert_refused(response, "foreign_id", "services")
                self.packages_read.assert_not_called()
                create.assert_not_called()
                s.hold_group.assert_not_called()

    @override_settings(GROUP_BOOKING_V2=True)
    def test_a_party_of_services_is_sent_the_same_either_way(self):
        sent = {}
        for flag in (False, True):
            create = mock.Mock(return_value=(201, dict(ANSWER)))
            with self.with_packages([], flag=flag, create_group_booking=create):
                response = self.post(GroupBookingCreateView, self.PATH, booking_body())
            self.assertEqual(response.status_code, 201, response.data)
            sent[flag] = (create.call_args.args, response.data)
        self.assertEqual(sent[True], sent[False])


# ------------------------------------------------------------ the old path


# The minutes booking-api's hold reserves: a package's are its parts', end to end.
HELD_MINUTES = {CUT: 45, NAILS: 60, str(PACKAGE): 45 + 45 + 60}


def hhmm(minute):
    return f"{minute // 60:02d}:{minute % 60:02d}"


def held_with_packages(body):
    """booking-api's 201 to POST /v1/groups/holds, a package's lane its parts long."""
    minute = body["targetMin"]
    return {
        "groupId": GROUP_ID, "holdId": HOLD_ID, "mode": "TOGETHER",
        "expiresAt": "2026-10-11T09:15:00.000Z", "expiresInSeconds": 900,
        "lanes": [
            {"label": p["label"], "staffId": STAFF[i], "start": hhmm(minute),
             "end": hhmm(minute + sum(HELD_MINUTES.get(s, 0) for s in p["serviceIds"]))}
            for i, p in enumerate(body["participants"])
        ],
    }


@override_settings(CACHES={"default": {
    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    "LOCATION": "group-package-booking-tests",
}})
class OldPathWithAPackageTests(PackageSeams, SimpleTestCase):
    """
    POST /booking/group with GROUP_BOOKING_V2 off: hold, then confirm.
    booking-api's desk party expands a package too (party-member.ts), at its
    price, with no child price on this path, as for a service.
    """

    PATH = "/api/v1/booking/group"

    def setUp(self):
        cache.clear()

    def test_the_package_is_held_as_sent_and_comes_back_as_its_line(self):
        body = visit_of({2: [PACKAGE]}, booking_body())   # Rana, a child
        # Cut 120 + nails 175 + the package's 370: the booker carries it all.
        confirm = mock.Mock(side_effect=lambda gid, b, **kw: (201, confirmed_answer(
            b, shares=("AED 665.00", "AED 0.00", "AED 0.00"))))
        with self.with_packages([groom()], hold_group=mock.Mock(
                side_effect=lambda b, **kw: (201, held_with_packages(b))),
                confirm_group=confirm) as s, \
                self.assertNoLogs("apps.salons.group_views", level="ERROR"):
            response = self.post(GroupBookingCreateView, self.PATH, body)
        self.assertEqual(response.status_code, 201, response.data)

        held = s.hold_group.call_args.args[0]["participants"]
        confirmed = s.confirm_group.call_args.args[1]["participants"]
        self.assertEqual([p["serviceIds"] for p in held], [[NAILS], [CUT], [str(PACKAGE)]])
        self.assertEqual([p["serviceIds"] for p in confirmed], [[NAILS], [CUT], [str(PACKAGE)]])

        rana = response.data["members"][2]
        self.assertEqual(rana["services"], [
            {"id": str(PACKAGE), "name": "The Full Groom", "amount": Decimal("370.00")},
        ])
        self.assertEqual(rana["total"], Decimal("370.00"))
        self.assertEqual(rana["end_time"], "2026-10-11T17:30:00+04:00")
        self.assertEqual([m["total"] for m in response.data["members"]],
                         [Decimal("120.00"), Decimal("175.00"), Decimal("370.00")])
        self.assertEqual(response.data["total"], Decimal("665.00"))

    def test_its_length_holds_the_start_to_the_salon_s_hours(self):
        # 18:00 + the package's 150 minutes is past 20:00: refused before any hold.
        body = visit_of({2: [PACKAGE]}, booking_body(start_time="2026-10-11T18:00:00+04:00"))
        with self.with_packages([groom()]) as s:
            response = self.post(GroupBookingCreateView, self.PATH, body)
        self.assert_refused(response, "outside_hours", "start_time")
        s.hold_group.assert_not_called()

    def test_a_package_that_cannot_be_booked_is_refused_before_any_hold(self):
        with self.with_packages([groom(status="HIDDEN")]) as s:
            response = self.post(GroupBookingCreateView, self.PATH,
                                 visit_of({2: [PACKAGE]}, booking_body()))
        self.assert_not_bookable(response, "services")
        s.hold_group.assert_not_called()


# ------------------------------------------------------------ members[].packages


class MembersPackagesFieldTests(PackageSeams, SimpleTestCase):
    """
    `members[].packages` is refused with the switch on or off: a package has
    one shape, a line of `services`. On, the refusal says so.
    """

    PATH = "/api/v1/booking/group"

    def body(self):
        members = party()
        members[2]["packages"] = [{"id": str(PACKAGE), "amount": 370}]
        return booking_body(members=members)

    def refusal(self, flag):
        with override_settings(PACKAGE_BOOKING_V1=flag):
            s = GroupBookingRequestSerializer(data=self.body(), context={"booker_id": USER_ID})
            self.assertFalse(s.is_valid())
        return s.errors["members"][2]["packages"]

    def test_off_the_words_it_always_had(self):
        self.assertEqual(PACKAGES_REFUSED,
                         "Group bookings cannot include packages yet. Choose the services instead.")
        [error] = self.refusal(False)
        self.assertEqual((error.code, str(error)), ("packages_not_supported", PACKAGES_REFUSED))

    def test_on_still_refused_and_says_to_send_a_services_line(self):
        [error] = self.refusal(True)
        self.assertEqual((error.code, str(error)), ("packages_not_supported", PACKAGES_AS_A_LINE))
        self.assertIn("one of this member's services", PACKAGES_AS_A_LINE)

    def test_empty_is_fine_either_way(self):
        for flag in (False, True):
            members = party()
            members[2]["packages"] = []
            with self.subTest(flag=flag), override_settings(PACKAGE_BOOKING_V1=flag):
                s = GroupBookingRequestSerializer(data=booking_body(members=members),
                                                  context={"booker_id": USER_ID})
                self.assertTrue(s.is_valid(), s.errors)

    def test_the_view_refuses_it_before_anything_is_read_or_asked(self):
        for flag, message in ((False, PACKAGES_REFUSED), (True, PACKAGES_AS_A_LINE)):
            with self.subTest(flag=flag), self.with_packages([groom()], flag=flag) as s:
                response = self.post(GroupBookingCreateView, self.PATH, self.body())
                self.assert_refused(response, "packages_not_supported", "packages")
                self.assertEqual(response.data["errors"][0]["message"], message)
                self.packages_read.assert_not_called()
                s.hold_group.assert_not_called()


# ------------------------------------------------------------ what booking-api is sent


def upstream(body, status=201):
    """urlopen's answer: `body` as booking-api's JSON."""
    response = mock.MagicMock(status=status)
    response.read.return_value = json.dumps(body).encode()
    response.__enter__.return_value = response
    return response


def compact(body):
    return json.dumps(body, separators=(",", ":")).encode()


@override_settings(BOOKING_API_URL="http://booking/")
class PackageLineOnTheWireTests(PackageSeams, SimpleTestCase):
    """
    The bytes booking-api receives, through the real client (only urlopen is
    replaced). booking-api expands a package itself, so a package line must
    reach it as the app sent it: gt.mobile_group_body copies each line's `id`
    and `amount` and nothing else (booking-api refuses any other key), and
    booking_api encodes them compactly, so a line the app wrote compactly
    crosses as the same bytes. Not swapped for its parts, not dropped, not
    moved, not repriced.
    """

    PATH = "/api/v1/booking/group"

    @override_settings(GROUP_BOOKING_V2=True)
    def test_create_carries_the_package_line_byte_for_byte(self):
        body = booking_body()
        body["members"][1]["services"] = [{"id": str(PACKAGE), "amount": 370},
                                          {"id": NAILS, "amount": 187.5}]
        body["members"][2]["services"] = [{"id": str(PACKAGE_2), "amount": 100}]
        sent = compact(body)
        dana = compact(body["members"][1]["services"])
        rana = compact(body["members"][2]["services"])
        self.assertIn(dana, sent)

        with self.with_packages([groom(), cuts(1)],
                                create_group_booking=booking_api.create_group_booking), \
                mock.patch.object(booking_api.urllib.request, "urlopen",
                                  return_value=upstream(ANSWER)) as urlopen:
            response = self.post(GroupBookingCreateView, self.PATH, sent)
        self.assertEqual(response.status_code, 201, response.data)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_full_url(), "http://booking/v1/mobile-booking/group")
        self.assertIn(b'"services":' + dana, request.data)
        self.assertIn(b'"services":' + rana, request.data)
        members = json.loads(request.data)["members"]
        self.assertEqual([m["services"] for m in members],
                         [[{"id": CUT, "amount": 120}],
                          body["members"][1]["services"],
                          body["members"][2]["services"]])

    @override_settings(GROUP_BOOKING_V2=True)
    def test_an_id_in_capitals_crosses_in_its_one_spelling(self):
        # The one thing done to an id, a service's or a package's alike: the
        # uuid's own spelling. booking-api matches a package id either way.
        body = booking_body()
        body["members"][1]["services"] = [{"id": str(PACKAGE).upper(), "amount": 370}]
        with self.with_packages([groom()],
                                create_group_booking=booking_api.create_group_booking), \
                mock.patch.object(booking_api.urllib.request, "urlopen",
                                  return_value=upstream(ANSWER)) as urlopen:
            response = self.post(GroupBookingCreateView, self.PATH, compact(body))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIn(b'"services":[{"id":"%s","amount":370}]' % str(PACKAGE).encode(),
                      urlopen.call_args.args[0].data)

    def test_availability_asks_about_the_package_ids_as_sent(self):
        body = visit_of({0: [PACKAGE, CUT], 1: [PACKAGE_2]}, availability_body())

        def answer(request, timeout=None):
            if "/v1/bookings/settings" in request.get_full_url():
                return upstream({"branch": {"utcOffsetMinutes": 360},
                                 "tradingWindow": {"fromMin": 600, "toMin": 1320}}, 200)
            asked = json.loads(request.data)
            return upstream({"starts": [plan_entry(m, asked["participants"], fits=False)
                                        for m in asked["targetMins"]]}, 200)

        with self.with_packages([groom(), cuts(2)], engine_clock=booking_api.engine_clock,
                                plan_group=booking_api.plan_group), \
                mock.patch.object(booking_api.urllib.request, "urlopen", side_effect=answer) as urlopen:
            response = self.post(GroupAvailabilityView, "/api/v1/booking/group-availability",
                                 compact(body))
        self.assertEqual(response.status_code, 200, response.data)

        plans = [c.args[0] for c in urlopen.call_args_list
                 if c.args[0].get_full_url().endswith("/v1/bookings/availability/group")]
        self.assertTrue(plans)
        for request in plans:
            self.assertIn(b'"serviceIds":' + compact([str(PACKAGE), CUT]), request.data)
            self.assertIn(b'"serviceIds":' + compact([str(PACKAGE_2)]), request.data)
            self.assertIn(b'"serviceIds":' + compact([NAILS]), request.data)


# ------------------------------------------------------------ what the reads carry


@override_settings(GROUP_BOOKING_V2=True)
class PartyReadPackagesTests(ReadSeams, SimpleTestCase):
    """
    GET /booking/<group id>: booking-api's members[].packages, present only
    on a member whose lane holds one, comes back as booking-api sent it.
    """

    HELD = {"id": str(PACKAGE), "name": "The Full Groom", "amount": 185,
            "service_ids": [CUT, CUT, NAILS]}

    def test_a_member_s_packages_come_back_untouched_and_nobody_gains_the_key(self):
        answer = json.loads(json.dumps(ANSWER))
        answer["members"][2]["packages"] = [self.HELD]
        response, _ = self.run_with(self.get, **{
            "apps.salons.group_views.read_group_booking": mock.Mock(return_value=(200, answer))})
        self.assertEqual(response.status_code, 200, response.data)
        members = response.data["members"]
        self.assertEqual(members[2].get("packages"), [self.HELD])
        self.assertEqual(["packages" in m for m in members], [False, False, True])


class MyBookingsPackagesTests(SimpleTestCase):
    """
    GET /bookings: a GROUP row's `packages` ([{id, name}], only when a member
    holds one) comes back as booking-api sent it.
    """

    # The list's own fixtures, borrowed: the module, not the class, is
    # imported, or the runner would collect the class here a second time.
    NOW = list_tests.BookingListViewTests.NOW
    CARD = list_tests.BookingListViewTests.CARD
    row = list_tests.BookingListViewTests.row
    page = list_tests.BookingListViewTests.page

    def results(self, rows):
        request = APIRequestFactory().get("/api/v1/bookings", HTTP_AUTHORIZATION="Bearer cus")
        force_authenticate(request, user=Customer())
        with mock.patch("apps.salons.views.list_bookings", return_value=(200, self.page(rows))), \
                mock.patch("apps.salons.views.salon_cards_for_refs",
                           return_value={"marina-walk": self.CARD}), \
                mock.patch("apps.salons.views.datetime") as clock:
            clock.now.return_value = self.NOW
            clock.fromisoformat = datetime.fromisoformat
            response = BookingListView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        return response.data["results"]

    def test_a_group_row_s_packages_come_back_untouched(self):
        names = [{"id": str(PACKAGE), "name": "The Full Groom"}]
        rows = self.results([
            self.row(id=GROUP_ID, booking_type="GROUP", member_count=3, packages=names),
            self.row(id="b2c6b0a9-0000-4000-8000-000000000002", booking_type="GROUP",
                     member_count=2),
        ])
        self.assertEqual(rows[0].get("packages"), names)
        self.assertNotIn("packages", rows[1])


# ------------------------------------------------------------ the FE guide

FE_GUIDE = settings.BASE_DIR / "docs" / "MOBILE_GROUP_BOOKING_FE.md"


def round_half_up(fils, percent):
    """booking-api's Money.percent: Math.round(fils * percent / 100), for whole fils."""
    return (fils * percent + 50) // 100


@unittest.skipUnless(FE_GUIDE.exists(), "docs/ is not in the Docker image")
class PackageMoneyDocTests(SimpleTestCase):
    """MOBILE_GROUP_BOOKING_FE.md, Packages: its example follows booking-api's party rules."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        text = FE_GUIDE.read_text(encoding="utf-8")
        cls.section = text.split("\n## Packages\n", 1)[1].split("\n## ", 1)[0]

    def test_the_worked_example_is_the_rules(self):
        # A package is one line at its price; a child pays half of the line,
        # rounded once; VAT 5% once on the party; the deposit 20% of the total.
        package, cut = 52000, 19900
        child = round_half_up(package, 50)
        net = package + child + cut
        vat = round_half_up(net, 5)
        total = net + vat
        deposit = round_half_up(total, 20)
        for fils in (package, child, cut, net, vat, total, deposit):
            self.assertIn(f"{fils // 100}.{fils % 100:02d}", self.section)

    def test_the_child_s_services_carry_the_package_s_charge_between_them(self):
        # The whole objects; the section also shows a `"services": [...]` fragment.
        blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", self.section, re.S)
                  if b.startswith("{")]
        [member] = [b for b in blocks if isinstance(b, dict) and "packages" in b]
        [held] = member["packages"]
        self.assertEqual(member["age_group"], "child")
        self.assertEqual(round(sum(s["amount"] for s in member["services"]), 2), held["amount"])
        self.assertEqual([s["id"] for s in member["services"]], held["service_ids"])

"""
Group self check-in (GROUP_SELF_CHECK_IN_V1): a member of an app party checks
in on their own lane with the single check-in route,
POST /api/v1/booking/{id}/check-in, and the lane's booking_id from the party
read. Off by default.

On the party's read (GET /api/v1/booking/<group_id>), the caller's own member
row gets can_check_in and check_in_opens_at, from the SAME rule a SINGLE
booking follows (_check_in_fields), read from that member's own lane. Every
other member row gets false and null. booking-api says which row is the
caller's (`is_me`) and gives its lane id (`booking_id`); this service never
guesses either.
"""

import contextlib
import copy
import importlib
import os
import uuid
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from unittest import mock

import environ
from django.conf import settings
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons.test_group_booking import GROUP_ID, RANA, USER_ID, Customer
from apps.salons.test_group_booking_v2 import ANSWER, ReadSeams
from apps.salons.views import BookingDetailView, _check_in_fields, _member_check_in_fields
from config.settings import base

FLAG = "GROUP_SELF_CHECK_IN_V1"


def setting_in(name, environment):
    """
    The setting `name` as config/settings/base.py works it out in
    `environment`: the module is run again with only that environment (and
    .env not read), and the setting is read off it. So a name misspelled in
    base.py, on either side of the `=`, fails the test that reads it.
    django.conf.settings cannot be asked instead: it was built once, at start.

    base.py is run again afterwards in the real environment, so nothing
    after this sees the patched one.
    """
    try:
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(environ.Env, "read_env"):
            return getattr(importlib.reload(base), name)
    finally:
        with mock.patch.object(environ.Env, "read_env"):
            importlib.reload(base)


def without(name):
    """The real environment, less `name`."""
    return {k: v for k, v in os.environ.items() if k != name}


class SwitchTests(SimpleTestCase):

    def test_the_flag_is_off_by_default(self):
        self.assertIn(f"{FLAG}=false", Path(".env.example").read_text())
        self.assertIn(f"{FLAG}: ${{{FLAG}:-False}}", Path("docker-compose.yml").read_text())
        source = Path("config/settings/base.py").read_text()
        self.assertIn(f'{FLAG} = env.bool("{FLAG}", default=False)', source)

    def test_unset_means_off(self):
        self.assertTrue(hasattr(settings, FLAG))
        self.assertIs(setting_in(FLAG, without(FLAG)), False)

    def test_true_turns_it_on_and_a_typo_does_not(self):
        self.assertIs(setting_in(FLAG, {**without(FLAG), FLAG: "true"}), True)
        self.assertIs(setting_in(FLAG, {**without(FLAG), FLAG: "ture"}), False)


# 12:00 UTC: 18:00 on booking-api's +06:00 clock, 16:00 at the salon (+04:00).
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=dt_timezone.utc)
# Starts 12:10 UTC: "I am here" opened at 11:40 UTC, and it is open now.
IN_WINDOW = ("2026-10-11T18:10:00+06:00", "2026-10-11T18:55:00+06:00")
# Starts 13:00 UTC: opens at 12:30 UTC, not yet.
LATER = ("2026-10-11T19:00:00+06:00", "2026-10-11T19:45:00+06:00")
# Ended 10:45 UTC. It opened at 09:30 UTC, a fact that stays true.
OVER = ("2026-10-11T16:00:00+06:00", "2026-10-11T16:45:00+06:00")

RANA_LANE = "c2d7a4e9-1b6f-4e3c-9a8d-5f0e2b7c4a16"
BOOKER_LANE = "5b8e1f2a-7c3d-4a9e-b6f0-2d4c8e1a3b57"
KEYS = ("can_check_in", "check_in_opens_at")
# Every member row carries these three (the chair too, since 4.2 of the app's asks).
ALL_KEYS = KEYS + ("can_scan_chair",)


def member(ref, kind, user_id, *, times=IN_WINDOW, status="CONFIRMED_BY_SALON", **over):
    """A member row as booking-api answers it with MOBILE_GROUP_SELF_CHECK_IN on."""
    start, end = times
    row = {
        "ref": ref, "id": f"p{ref}", "booking_code": f"GS-{1046 + ref}", "user_id": user_id,
        "name": None if user_id else "Amal", "kind": kind, "status": status,
        "start_time": start, "end_time": end, "is_me": False, "booking_id": None,
    }
    row.update(over)
    return row


def party(rana=None, booker=None, guest=None, **over):
    """The booker, Rana (the caller, `is_me`) and a guest; all in the window unless told."""
    members = [
        booker or member(0, "self", str(USER_ID)),
        rana or member(1, "registered", RANA, is_me=True, booking_id=RANA_LANE),
        guest or member(2, "guest", None),
    ]
    return dict(ANSWER, members=members, **over)


ALL_ON = dict(GROUP_BOOKING_V2=True, ROUTINE_CONTRACT_V1=False,
              SELF_CHECK_IN_V1=True, GROUP_SELF_CHECK_IN_V1=True)


class PartyReadSeams(ReadSeams):
    """GET /booking/<group_id> as Rana (or `user`), with booking-api answering `answer`, at NOW."""

    def read(self, answer, user=None, **over):
        fake = mock.Mock(return_value=(200, copy.deepcopy(answer)))
        caller = user or Customer(id=uuid.UUID(RANA), full_name="Rana")
        response, _ = self.run_with(
            lambda: self.as_caller(caller),
            **{"apps.salons.group_views.read_group_booking": fake, **over})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["members"]

    def as_caller(self, user):
        request = APIRequestFactory().get(f"/api/v1/booking/{GROUP_ID}",
                                          HTTP_AUTHORIZATION="Bearer customer-token")
        force_authenticate(request, user=user)
        with contextlib.ExitStack() as stack:
            clock = stack.enter_context(mock.patch("apps.salons.group_views.datetime"))
            clock.now.return_value = NOW
            clock.fromisoformat = datetime.fromisoformat
            return BookingDetailView.as_view()(request, booking_id=uuid.UUID(GROUP_ID))

    @staticmethod
    def fields(row):
        return {key: row[key] for key in KEYS if key in row}

    def mine(self, members):
        [row] = [m for m in members if m.get("is_me") is True]
        return self.fields(row)


@override_settings(**ALL_ON)
class MemberRowRuleTests(PartyReadSeams, SimpleTestCase):
    """The caller's own row, by the SINGLE rule on their own lane."""

    def test_in_the_window_true(self):
        members = self.read(party())
        self.assertEqual(self.mine(members),
                         {"can_check_in": True, "check_in_opens_at": "2026-10-11T11:40:00.000Z"})

    def test_before_the_window_false_with_the_time_it_opens(self):
        rana = member(1, "registered", RANA, is_me=True, booking_id=RANA_LANE, times=LATER)
        self.assertEqual(self.mine(self.read(party(rana=rana))),
                         {"can_check_in": False, "check_in_opens_at": "2026-10-11T12:30:00.000Z"})

    def test_after_the_window_false(self):
        rana = member(1, "registered", RANA, is_me=True, booking_id=RANA_LANE, times=OVER)
        # The time it opened is still a fact, as on a single booking's row.
        self.assertEqual(self.mine(self.read(party(rana=rana))),
                         {"can_check_in": False, "check_in_opens_at": "2026-10-11T09:30:00.000Z"})

    def test_not_confirmed_by_salon_false_and_null(self):
        for status in ("CHECKED_IN", "BOOKED", "CANCELLED", "COMPLETED", None):
            with self.subTest(status=status):
                rana = member(1, "registered", RANA, is_me=True, booking_id=RANA_LANE, status=status)
                self.assertEqual(self.mine(self.read(party(rana=rana))),
                                 {"can_check_in": False, "check_in_opens_at": None})

    def test_rows_that_are_not_me_false_and_null_even_in_their_window(self):
        members = self.read(party())
        others = [self.fields(m) for m in members if m["is_me"] is not True]
        self.assertEqual(others, [{"can_check_in": False, "check_in_opens_at": None}] * 2)

    def test_the_members_own_lane_never_the_partys_or_the_bookers(self):
        # The booker is checked in, so the party reads CHECKED_IN; Rana's own
        # lane is still confirmed and in its window.
        booker = member(0, "self", str(USER_ID), status="CHECKED_IN")
        members = self.read(party(booker=booker, status="CHECKED_IN"))
        self.assertIs(self.mine(members)["can_check_in"], True)

        # The party and the booker are in the window; Rana's own lane is later.
        rana = member(1, "registered", RANA, is_me=True, booking_id=RANA_LANE, times=LATER)
        members = self.read(party(rana=rana))
        self.assertEqual(self.mine(members),
                         {"can_check_in": False, "check_in_opens_at": "2026-10-11T12:30:00.000Z"})

    def test_one_rule_the_singles_own_function_answers(self):
        # Swap the single booking's rule out: the member row answers whatever it says.
        with mock.patch("apps.salons.views._can_check_in", return_value="the rule's answer"):
            self.assertEqual(self.mine(self.read(party()))["can_check_in"], "the rule's answer")

    @override_settings(CHAIR_SCAN_V1=True)
    def test_my_row_may_scan_a_chair_in_my_window_and_no_other_row_may(self):
        members = self.read(party())
        self.assertEqual([m["can_scan_chair"] for m in members], [False, True, False])

    @override_settings(CHAIR_SCAN_V1=True)
    def test_no_chair_outside_my_window_or_once_i_am_in(self):
        for rana in (member(1, "registered", RANA, is_me=True, booking_id=RANA_LANE, times=LATER),
                     member(1, "registered", RANA, is_me=True, booking_id=RANA_LANE, times=OVER),
                     member(1, "registered", RANA, is_me=True, booking_id=RANA_LANE,
                            status="CHECKED_IN")):
            with self.subTest(rana=(rana["start_time"], rana["status"])):
                self.assertIs(self.read(party(rana=rana))[1]["can_scan_chair"], False)

    @override_settings(CHAIR_SCAN_V1=False)
    def test_chair_scanning_off_means_false_on_every_row_even_in_my_window(self):
        members = self.read(party())
        self.assertIs(members[1]["can_check_in"], True)
        self.assertEqual([m["can_scan_chair"] for m in members], [False, False, False])


@override_settings(**ALL_ON)
class MemberRowShapeTests(PartyReadSeams, SimpleTestCase):
    """One shape for every row, and only when all three gates hold."""

    def test_every_member_row_carries_all_three_keys(self):
        for m in self.read(party()):
            self.assertEqual(set(ALL_KEYS) - set(m), set(), m["ref"])

    def test_booking_id_passes_through_untouched(self):
        members = self.read(party())
        self.assertEqual([m["booking_id"] for m in members], [None, RANA_LANE, None])
        with override_settings(GROUP_SELF_CHECK_IN_V1=False):
            members = self.read(party())
        self.assertEqual([m["booking_id"] for m in members], [None, RANA_LANE, None])

    def test_is_me_is_read_strictly(self):
        for loose in ("true", 1, "True"):
            with self.subTest(is_me=loose):
                rana = member(1, "registered", RANA, is_me=loose, booking_id=RANA_LANE)
                members = self.read(party(rana=rana))
                self.assertEqual([self.fields(m) for m in members],
                                 [{"can_check_in": False, "check_in_opens_at": None}] * 3)

    def test_each_switch_off_means_no_keys(self):
        for switch in ("SELF_CHECK_IN_V1", "GROUP_SELF_CHECK_IN_V1"):
            with override_settings(**{switch: False}), self.subTest(off=switch):
                for m in self.read(party()):
                    self.assertEqual(self.fields(m), {}, m["ref"])
                    self.assertNotIn("can_scan_chair", m)

    def test_is_me_missing_means_no_keys(self):
        # booking-api's own switch off: no row carries is_me.
        answer = party()
        for m in answer["members"]:
            del m["is_me"]
            del m["booking_id"]
        for m in self.read(answer):
            self.assertEqual(self.fields(m), {}, m["ref"])

    def test_is_me_on_some_rows_only_means_no_keys(self):
        answer = party()
        del answer["members"][2]["is_me"]
        for m in self.read(answer):
            self.assertEqual(self.fields(m), {}, m["ref"])

    def test_the_cancels_answer_gets_no_keys(self):
        cancelled = mock.Mock(return_value=(200, copy.deepcopy(party())))
        response, _ = self.run_with(
            self.cancel, **{"apps.salons.group_views.cancel_group_booking": cancelled})
        self.assertEqual(response.status_code, 200, response.data)
        for m in response.data["members"]:
            self.assertEqual(self.fields(m), {}, m["ref"])


def as_the_booker_sees_it():
    """The same party, read by the booker: their own row is `is_me`, with their own lane."""
    return party(
        booker=member(0, "self", str(USER_ID), is_me=True, booking_id=BOOKER_LANE),
        rana=member(1, "registered", RANA),
    )


@override_settings(**ALL_ON)
class BookerRowTests(PartyReadSeams, SimpleTestCase):
    """The booker's own row never gets the member button: the booker's way is We are here."""

    def test_the_booker_in_the_window_gets_false_and_null_on_their_own_row(self):
        members = self.read(as_the_booker_sees_it(), user=Customer())
        booker = members[0]
        # booking-api's facts about the caller, left as they came.
        self.assertEqual((booker["kind"], booker["is_me"], booker["booking_id"]),
                         ("self", True, BOOKER_LANE))
        # In the window, and still no button, and no chair (chair scanning on).
        self.assertEqual(self.fields(booker), {"can_check_in": False, "check_in_opens_at": None})
        with override_settings(CHAIR_SCAN_V1=True):
            booker = self.read(as_the_booker_sees_it(), user=Customer())[0]
        self.assertIs(booker["can_scan_chair"], False)

    def test_every_row_still_carries_both_keys_when_the_booker_reads(self):
        members = self.read(as_the_booker_sees_it(), user=Customer())
        self.assertEqual([self.fields(m) for m in members],
                         [{"can_check_in": False, "check_in_opens_at": None}] * 3)

    def test_a_member_reading_the_same_party_still_gets_true_and_their_lane(self):
        rana = self.read(party())[1]
        self.assertEqual((rana["kind"], rana["is_me"], rana["booking_id"]),
                         ("registered", True, RANA_LANE))
        self.assertEqual(self.fields(rana),
                         {"can_check_in": True, "check_in_opens_at": "2026-10-11T11:40:00.000Z"})


@override_settings(SELF_CHECK_IN_V1=True, CHAIR_SCAN_V1=True)
class MemberShimTests(SimpleTestCase):
    """_member_check_in_fields: the SINGLE rule's two keys, fed the member's own lane."""

    def test_it_is_the_single_rule_on_the_members_own_fields(self):
        for times in (IN_WINDOW, LATER, OVER):
            for status in ("CONFIRMED_BY_SALON", "CHECKED_IN"):
                with self.subTest(times=times, status=status):
                    m = member(1, "registered", RANA, times=times, status=status)
                    single = _check_in_fields(
                        {"booking_type": "SINGLE", "status": status,
                         "start_time": times[0], "end_time": times[1]}, NOW)
                    self.assertEqual(_member_check_in_fields(m, NOW),
                                     {key: single[key] for key in ALL_KEYS})

    def test_only_the_lanes_own_status_and_times_are_read(self):
        # Anything else on the row (a party's type, another status word) is ignored.
        m = member(1, "registered", RANA, booking_type="GROUP", is_booker=False,
                   payment_status="PAY_AFTER_CHECK_IN")
        self.assertEqual(_member_check_in_fields(m, NOW),
                         {"can_check_in": True, "check_in_opens_at": "2026-10-11T11:40:00.000Z",
                          "can_scan_chair": True})

    def test_the_shim_is_unchanged_it_knows_nothing_of_kind(self):
        # The booker rule is the party read's (group_views._member_check_in),
        # not the single booking's: fed a "self" row, the shim answers as ever.
        expected = {"can_check_in": True, "check_in_opens_at": "2026-10-11T11:40:00.000Z",
                    "can_scan_chair": True}
        for kind, user_id in (("self", str(USER_ID)), ("registered", RANA)):
            with self.subTest(kind=kind):
                self.assertEqual(_member_check_in_fields(member(0, kind, user_id), NOW), expected)

    @override_settings(SELF_CHECK_IN_V1=False)
    def test_nothing_with_self_check_in_off(self):
        self.assertEqual(_member_check_in_fields(member(1, "registered", RANA), NOW), {})

"""
Group check-in at the top of the party's read (GROUP_CHECK_IN_V1): the
booker's We are here, on GET /api/v1/booking/<group_id> itself, so the party
screen draws the button however it was opened (a push, a deep link), not only
from My Bookings.

`is_booker`, and can_check_in, check_in_opens_at and can_scan_chair from the
SAME function the My Bookings GROUP row uses (views._check_in_fields), fed
the party's status (the booker's lane), start and end.
"""

import copy
import uuid
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.salons.test_group_booking import RANA, USER_ID, Customer
from apps.salons.test_group_self_check_in import (
    IN_WINDOW,
    LATER,
    OVER,
    PartyReadSeams,
    as_the_booker_sees_it,
    member,
    party,
)

PARTY_KEYS = ("is_booker", "can_check_in", "check_in_opens_at", "can_scan_chair")

# Every switch on, as on production (the member rows' GROUP_SELF_CHECK_IN_V1 too).
ALL_ON = dict(GROUP_BOOKING_V2=True, ROUTINE_CONTRACT_V1=False,
              SELF_CHECK_IN_V1=True, GROUP_CHECK_IN_V1=True, GROUP_SELF_CHECK_IN_V1=True,
              CHAIR_SCAN_V1=True)


def timed(times, status="CONFIRMED_BY_SALON", **over):
    """The party, its own start, end and status (the booker's lane) set to these."""
    start, end = times
    return party(start_time=start, end_time=end, status=status, **over)


class PartyTopSeams(PartyReadSeams):
    """The whole read, not only its member rows."""

    def top(self, answer, user=None):
        fake = mock.Mock(return_value=(200, copy.deepcopy(answer)))
        caller = user or Customer()  # the booker, USER_ID, unless told
        response, _ = self.run_with(
            lambda: self.as_caller(caller),
            **{"apps.salons.group_views.read_group_booking": fake})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    @staticmethod
    def keys(data):
        return {key: data[key] for key in PARTY_KEYS if key in data}


@override_settings(**ALL_ON)
class BookerAtTheTopTests(PartyTopSeams, SimpleTestCase):
    """The booker's button, at the party level."""

    def test_the_booker_in_the_window_gets_the_button(self):
        self.assertEqual(self.keys(self.top(timed(IN_WINDOW))), {
            "is_booker": True,
            "can_check_in": True,
            "check_in_opens_at": "2026-10-11T11:40:00.000Z",
            "can_scan_chair": False,
        })

    def test_before_the_window_false_with_the_time_it_opens(self):
        self.assertEqual(self.keys(self.top(timed(LATER))), {
            "is_booker": True,
            "can_check_in": False,
            "check_in_opens_at": "2026-10-11T12:30:00.000Z",
            "can_scan_chair": False,
        })

    def test_after_the_window_false_and_the_time_it_opened(self):
        data = self.top(timed(OVER))
        self.assertIs(data["can_check_in"], False)
        self.assertEqual(data["check_in_opens_at"], "2026-10-11T09:30:00.000Z")

    def test_once_the_party_is_in_false_and_null(self):
        # The party's status is the booker's own lane, as on the GROUP row.
        data = self.top(timed(IN_WINDOW, status="CHECKED_IN"))
        self.assertEqual((data["is_booker"], data["can_check_in"], data["check_in_opens_at"]),
                         (True, False, None))

    def test_a_member_reading_is_not_the_booker_and_has_no_party_button(self):
        data = self.top(timed(IN_WINDOW), user=Customer(id=uuid.UUID(RANA), full_name="Rana"))
        self.assertEqual(self.keys(data), {
            "is_booker": False,
            "can_check_in": False,
            "check_in_opens_at": None,
            "can_scan_chair": False,
        })

    def test_never_a_chair_for_the_party(self):
        self.assertIs(self.top(timed(IN_WINDOW))["can_scan_chair"], False)

    def test_one_rule_the_rows_own_function_answers(self):
        # Swap the row's rule out: the party's top answers whatever it says.
        with mock.patch("apps.salons.views._can_check_in", return_value="the rule's answer"):
            self.assertEqual(self.top(timed(IN_WINDOW))["can_check_in"], "the rule's answer")

    def test_it_does_not_need_booking_apis_is_me(self):
        answer = timed(IN_WINDOW)
        for m in answer["members"]:
            del m["is_me"]
            del m["booking_id"]
        self.assertIs(self.top(answer)["can_check_in"], True)

    def test_is_booker_is_read_strictly(self):
        # No self row, or two of them: nobody is the booker.
        no_self, two = timed(IN_WINDOW), timed(IN_WINDOW)
        no_self["members"] = [member(1, "registered", str(USER_ID))]
        two["members"] = [member(0, "self", str(USER_ID)), member(1, "self", str(USER_ID))]
        for answer in (no_self, two):
            with self.subTest(kinds=[m["kind"] for m in answer["members"]]):
                data = self.top(answer)
                self.assertEqual((data["is_booker"], data["can_check_in"]), (False, False))

    def test_the_member_rows_are_unchanged_by_it(self):
        members = self.top(as_the_booker_sees_it())["members"]
        self.assertEqual([m["can_check_in"] for m in members], [False, False, False])


@override_settings(**ALL_ON)
class PartyTopSwitchTests(PartyTopSeams, SimpleTestCase):
    """Off, the read is exactly what it was: no key at all."""

    def test_each_switch_off_means_no_party_keys(self):
        for switch in ("SELF_CHECK_IN_V1", "GROUP_CHECK_IN_V1"):
            with override_settings(**{switch: False}), self.subTest(off=switch):
                self.assertEqual(self.keys(self.top(timed(IN_WINDOW))), {})

    def test_the_cancels_answer_gets_no_party_keys(self):
        cancelled = mock.Mock(return_value=(200, copy.deepcopy(timed(IN_WINDOW))))
        response, _ = self.run_with(
            self.cancel, **{"apps.salons.group_views.cancel_group_booking": cancelled})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.keys(response.data), {})

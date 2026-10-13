"""
The GROUP row's check-in fields in My Bookings (GROUP_CHECK_IN_V1): the
booker's row offers "We are here" in the same window as a single booking, a
member's row never does, and a party never offers Scan the chair. While the
switch is off, a GROUP row is what it was.
"""
from datetime import datetime, timezone as dt_timezone

from django.test import SimpleTestCase, override_settings

from apps.salons.views import _check_in_fields

# 10:00 at the salon (+04:00) is 06:00 UTC, so "I am here" opens at 05:30 UTC.
START = "2026-10-13T10:00:00+04:00"
END = "2026-10-13T11:00:00+04:00"
OPENS = "2026-10-13T05:30:00.000Z"
INSIDE = datetime(2026, 10, 13, 5, 50, tzinfo=dt_timezone.utc)
BEFORE = datetime(2026, 10, 13, 5, 0, tzinfo=dt_timezone.utc)

NO_BUTTON = {"can_check_in": False, "check_in_opens_at": None, "can_scan_chair": False}


def row(booking_type="GROUP", **extra):
    return {
        "booking_type": booking_type,
        "status": "CONFIRMED_BY_SALON",
        "start_time": START,
        "end_time": END,
        **extra,
    }


@override_settings(SELF_CHECK_IN_V1=True, GROUP_CHECK_IN_V1=True, CHAIR_SCAN_V1=True)
class BookerRowTests(SimpleTestCase):
    def test_the_booker_in_the_window_has_the_button_but_no_chair(self):
        self.assertEqual(
            _check_in_fields(row(is_booker=True), INSIDE),
            {"can_check_in": True, "check_in_opens_at": OPENS, "can_scan_chair": False},
        )

    def test_the_booker_before_the_window_sees_when_it_opens(self):
        self.assertEqual(
            _check_in_fields(row(is_booker=True), BEFORE),
            {"can_check_in": False, "check_in_opens_at": OPENS, "can_scan_chair": False},
        )

    def test_a_member_never_even_with_a_truthy_lookalike(self):
        for extra in ({"is_booker": False}, {}, {"is_booker": "true"}, {"is_booker": 1}):
            self.assertEqual(_check_in_fields(row(**extra), INSIDE), NO_BUTTON, extra)

    def test_a_single_row_is_what_it_was(self):
        self.assertEqual(
            _check_in_fields(row("SINGLE"), INSIDE),
            {"can_check_in": True, "check_in_opens_at": OPENS, "can_scan_chair": True},
        )


@override_settings(SELF_CHECK_IN_V1=True, GROUP_CHECK_IN_V1=False, CHAIR_SCAN_V1=True)
class SwitchOffRowTests(SimpleTestCase):
    def test_the_booker_row_is_what_it_was(self):
        self.assertEqual(_check_in_fields(row(is_booker=True), INSIDE), NO_BUTTON)

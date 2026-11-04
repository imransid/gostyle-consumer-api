"""
A shut salon's card names its NEXT opening (CARD_NEXT_OPENING_V1,
docs/SEARCH_DISCOVERY_AUDIT.md X4, D10).

Off, two things were wrong, both kept as they were:
- after closing time, status_line said today's opening, already past
  ("Opens at 9:00 AM" at 11 PM);
- next_opening always started from tomorrow, so at 07:00 before a 09:00
  opening it named tomorrow.

The pure rules run for real; the card is read with its clock frozen.
"""

import types
import uuid
import zoneinfo
from datetime import datetime
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.salons import serializers
from apps.salons.hours import next_opening_from_now, opening_at, opening_line
from apps.salons.serializers import SalonCardSerializer

DUBAI = zoneinfo.ZoneInfo("Asia/Dubai")
SALON = uuid.UUID("33333333-3333-3333-3333-3333333333a1")

# Mon 09:00-22:00, Tue 10:00-20:00, Wed to Sat closed, Sun 10:00-18:00.
WEEK = [
    {"day": "mon", "closed": False, "open": "09:00", "close": "22:00"},
    {"day": "tue", "closed": False, "open": "10:00", "close": "20:00"},
    {"day": "wed", "closed": True},
    {"day": "thu", "closed": True},
    {"day": "fri", "closed": True},
    {"day": "sat", "closed": True},
    {"day": "sun", "closed": False, "open": "10:00", "close": "18:00"},
]
SHUT_ALL_WEEK = [{"day": d, "closed": True} for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")]
MON, TUE, WED = 0, 1, 2


def dubai(day, hour, minute=0):
    """2 Nov 2026 is a Monday."""
    return datetime(2026, 11, day, hour, minute, tzinfo=DUBAI)


class NextOpeningFromNowTests(SimpleTestCase):
    """hours.next_opening_from_now: from now, later today included."""

    def test_before_today_s_opening_it_is_today(self):
        self.assertEqual(next_opening_from_now(WEEK, MON, "07:00"), (0, "09:00"))

    def test_at_the_opening_minute_it_is_no_longer_ahead(self):
        # Open from 09:00: at 09:00 the salon is open, so this is never asked;
        # the rule still never names a time that is not ahead.
        self.assertEqual(next_opening_from_now(WEEK, MON, "09:00"), (1, "10:00"))

    def test_after_today_s_closing_it_is_the_next_open_day(self):
        self.assertEqual(next_opening_from_now(WEEK, MON, "23:00"), (1, "10:00"))

    def test_closed_days_are_skipped(self):
        self.assertEqual(next_opening_from_now(WEEK, TUE, "21:00"), (5, "10:00"))
        self.assertEqual(next_opening_from_now(WEEK, WED, "08:00"), (4, "10:00"))

    def test_shut_by_hand_today_skips_today(self):
        self.assertEqual(next_opening_from_now(WEEK, MON, "07:00", shut_today=True), (1, "10:00"))

    def test_an_overnight_day_still_ahead_is_today(self):
        late = [{"day": "mon", "closed": False, "open": "18:00", "close": "02:00"}]
        self.assertEqual(next_opening_from_now(late, MON, "03:00"), (0, "18:00"))

    def test_open_one_day_a_week_is_that_day_next_week(self):
        mondays = [{"day": "mon", "closed": False, "open": "09:00", "close": "22:00"}]
        self.assertEqual(next_opening_from_now(mondays, MON, "23:00"), (7, "09:00"))

    def test_closed_all_week_or_no_hours_is_none(self):
        self.assertIsNone(next_opening_from_now(SHUT_ALL_WEEK, MON, "07:00"))
        self.assertIsNone(next_opening_from_now(None, MON, "07:00"))


class OpeningLineTests(SimpleTestCase):
    """hours.opening_line and opening_at."""

    def test_the_three_sentences(self):
        self.assertEqual(opening_line((0, "09:00"), MON), "Opens at 9:00 AM")
        self.assertEqual(opening_line((1, "10:00"), MON), "Opens tomorrow at 10:00 AM")
        self.assertEqual(opening_line((5, "10:00"), TUE), "Opens Sunday at 10:00 AM")
        self.assertEqual(opening_line((7, "09:00"), MON), "Opens Monday at 9:00 AM")

    def test_nothing_or_an_unreadable_time_is_none(self):
        self.assertIsNone(opening_line(None, MON))
        self.assertIsNone(opening_line((1, "9am"), MON))
        self.assertIsNone(opening_at(None, dubai(2, 7)))
        self.assertIsNone(opening_at((1, "9am"), dubai(2, 7)))

    def test_opening_at_is_the_salon_s_clock_with_its_offset(self):
        self.assertEqual(opening_at((0, "09:00"), dubai(2, 7)).isoformat(), "2026-11-02T09:00:00+04:00")
        self.assertEqual(opening_at((5, "10:00"), dubai(3, 21)).isoformat(), "2026-11-08T10:00:00+04:00")


class _Frozen(datetime):
    """serializers.datetime with now() fixed at `at`, in the zone asked for."""

    at = None

    @classmethod
    def now(cls, tz=None):
        return cls.at.astimezone(tz)


def card(at, weekly=WEEK, manual_state=None):
    """status_line, next_opening, open, opens_at, hours_today of one card at `at`."""
    salon = types.SimpleNamespace(
        pk=SALON, branch_timezone="Asia/Dubai", published_hours={"weekly": weekly},
        manual_state=manual_state,
    )
    _Frozen.at = at
    reader = SalonCardSerializer()
    with mock.patch.object(serializers, "datetime", _Frozen):
        return {
            "status_line": reader.get_status_line(salon),
            "next_opening": reader.get_next_opening(salon),
            "open": reader.get_open(salon),
            "opens_at": reader.get_opens_at(salon),
            "hours_today": reader.get_hours_today(salon),
        }


class CardOffTests(SimpleTestCase):
    """Off (the default): the card says exactly what it said before."""

    def test_after_closing_the_old_line_names_today_s_past_opening(self):
        found = card(dubai(2, 23))
        self.assertEqual(found["status_line"], "Opens at 9:00 AM")
        self.assertEqual(found["next_opening"], "2026-11-03T10:00:00+04:00")

    def test_before_opening_next_opening_was_tomorrow(self):
        found = card(dubai(2, 7))
        self.assertEqual(found["status_line"], "Opens at 9:00 AM")
        self.assertEqual(found["next_opening"], "2026-11-03T10:00:00+04:00")

    def test_a_closed_day_had_no_line(self):
        self.assertIsNone(card(dubai(4, 12))["status_line"])


@override_settings(CARD_NEXT_OPENING_V1=True)
class CardOnTests(SimpleTestCase):
    """On: a shut salon names its next opening, never a past one."""

    def test_before_opening_it_is_today(self):
        found = card(dubai(2, 7))
        self.assertEqual(found["status_line"], "Opens at 9:00 AM")
        self.assertEqual(found["next_opening"], "2026-11-02T09:00:00+04:00")

    def test_after_closing_it_is_tomorrow(self):
        found = card(dubai(2, 23))
        self.assertEqual(found["status_line"], "Opens tomorrow at 10:00 AM")
        self.assertEqual(found["next_opening"], "2026-11-03T10:00:00+04:00")

    def test_after_closing_before_closed_days_it_names_the_day(self):
        found = card(dubai(3, 21))
        self.assertEqual(found["status_line"], "Opens Sunday at 10:00 AM")
        self.assertEqual(found["next_opening"], "2026-11-08T10:00:00+04:00")

    def test_a_closed_day_now_has_a_line(self):
        found = card(dubai(4, 12))
        self.assertEqual(found["status_line"], "Opens Sunday at 10:00 AM")
        self.assertEqual(found["next_opening"], "2026-11-08T10:00:00+04:00")

    def test_shut_by_hand_before_opening_it_is_tomorrow(self):
        found = card(dubai(2, 7), manual_state="CLOSED")
        self.assertEqual(found["status_line"], "Opens tomorrow at 10:00 AM")
        self.assertEqual(found["next_opening"], "2026-11-03T10:00:00+04:00")

    def test_an_open_salon_is_unchanged(self):
        found = card(dubai(2, 12))
        self.assertEqual(found["status_line"], "Closes at 10:00 PM")
        self.assertIsNone(found["next_opening"])

    def test_closed_all_week_or_no_hours_say_nothing(self):
        for weekly in (SHUT_ALL_WEEK, None):
            with self.subTest(weekly=weekly):
                found = card(dubai(2, 7), weekly=weekly)
                self.assertIsNone(found["status_line"])
                self.assertIsNone(found["next_opening"])

    def test_the_other_hours_fields_are_the_same_on_and_off(self):
        for at in (dubai(2, 7), dubai(2, 12), dubai(2, 23), dubai(4, 12)):
            on = card(at)
            with self.settings(CARD_NEXT_OPENING_V1=False):
                off = card(at)
            for key in ("open", "opens_at", "hours_today"):
                with self.subTest(at=at, key=key):
                    self.assertEqual(on[key], off[key])

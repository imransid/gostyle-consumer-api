import json
import math
import types
import urllib.error
import uuid
import zoneinfo
from unittest import mock
from decimal import Decimal
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import parse_qsl, urlencode
from django.http import QueryDict
from django.test import SimpleTestCase, override_settings

from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons import booking_api, skills, slots, timezones, translate
from apps.salons.booking_api import BookingApiUnavailable
from apps.salons.geo import bounding_box, format_distance, radius_box
from apps.salons.hours import is_within, next_opening, next_opening_at, resolve
from apps.salons.money import bps_to_percent, major
from apps.salons.snapshot import field, items, normalize

from apps.salons.selectors import tenant_for_salon
from apps.salons.params import (
    MAX_SERVICE_IDS,
    ParamError,
    parse_discovery,
    parse_map,
    parse_nearest_available,
    parse_service_ids,
    parse_window,
)
from apps.salons.views import (
    BookingCreateView,
    BookingDetailView,
    BookingListView,
    ServiceDetailsView,
    _stylist_order,
)


# SimpleTestCase, not TestCase: it refuses database access outright. If someone
# later adds a model import to one of these modules, these tests fail loudly
# rather than quietly opening a connection.
class SnapshotTests(SimpleTestCase):
    def test_none_yields_every_section(self):
        """An unpublished salon is an ordinary state, not an error."""
        snap = normalize(None)
        self.assertEqual(len(snap), 13)
        self.assertEqual(snap["IDENTITY"], {})

    def test_garbage_yields_every_section(self):
        """JSONB can hold a string or a list. Neither may crash a read."""
        for junk in ("a string", [1, 2], 42, True):
            self.assertEqual(normalize(junk)["IDENTITY"], {})

    def test_missing_section_is_empty_not_absent(self):
        """
        A version published before AMENITIES existed has no such key. The
        reader must still hand back a dict, because every caller does
        snap["AMENITIES"].get(...) without checking first.
        """
        snap = normalize({"IDENTITY": {"nameEn": "Iron Razor"}})
        self.assertEqual(snap["AMENITIES"], {})
        self.assertEqual(field(snap, "IDENTITY", "nameEn"), "Iron Razor")

    def test_unknown_section_is_dropped(self):
        """
        A future platform release may write a section this reader does not
        know. Passing it through would leak an unrecognised shape into the
        response; the reader iterates ITS key list, not the payload's.
        """
        snap = normalize({"IDENTITY": {}, "TIKTOK_LIVE": {"x": 1}})
        self.assertNotIn("TIKTOK_LIVE", snap)

    def test_section_of_wrong_type_is_empty(self):
        """IDENTITY as a list is malformed, not a section."""
        self.assertEqual(normalize({"IDENTITY": ["nope"]})["IDENTITY"], {})

    def test_blank_string_collapses_to_default(self):
        """
        A cleared text box stores as null today, but older snapshots hold ""
        or "   ". All three mean the same thing to a caller.
        """
        snap = {"IDENTITY": {"a": "", "b": "   ", "c": None}}
        for key in ("a", "b", "c"):
            self.assertEqual(field(snap, "IDENTITY", key, "fallback"), "fallback")

    def test_zero_and_false_survive(self):
        """
        The blank-string rule must not swallow falsy values that are real.
        A depositPercent of 0 is a decision the salon made.
        """
        snap = {"POLICY": {"depositPercent": 0, "autoChargeNoShow": False}}
        self.assertEqual(field(snap, "POLICY", "depositPercent", 99), 0)
        self.assertIs(field(snap, "POLICY", "autoChargeNoShow", True), False)

    def test_items_always_returns_a_list(self):
        self.assertEqual(items({"AMENITIES": {"items": ["WIFI"]}}, "AMENITIES", "items"), ["WIFI"])
        self.assertEqual(items({"AMENITIES": {}}, "AMENITIES", "items"), [])
        self.assertEqual(items({"AMENITIES": {"items": "WIFI"}}, "AMENITIES", "items"), [])

    def test_no_shared_state_between_reads(self):
        """
        empty_snapshot() is a function so each caller gets a fresh dict. If it
        were a module constant, one caller mutating a section would change it
        for every other caller for the life of the process.
        """
        a, b = normalize(None), normalize(None)
        a["IDENTITY"]["nameEn"] = "mutated"
        self.assertEqual(b["IDENTITY"], {})


class HoursTests(SimpleTestCase):
    MON = [{"day": "mon", "closed": False, "open": "09:00", "close": "22:00"}]

    def test_open_and_closed_within_normal_day(self):
        self.assertTrue(resolve(self.MON, None, None, 0, "14:00")["is_open"])
        self.assertFalse(resolve(self.MON, None, None, 0, "08:00")["is_open"])
        self.assertFalse(resolve(self.MON, None, None, 0, "23:00")["is_open"])

    def test_boundaries_are_inclusive_open_exclusive_close(self):
        """At exactly closing time the salon is shut, not open."""
        self.assertTrue(is_within("09:00", "22:00", "09:00"))
        self.assertFalse(is_within("09:00", "22:00", "22:00"))

    def test_overnight_window(self):
        """
        The bug this replaced compared strings, so "18:00" <= "20:00"
        "02:00" was False and a bar open till 2am reported shut all evening.
        """
        self.assertTrue(is_within("18:00", "02:00", "20:00"))
        self.assertTrue(is_within("18:00", "02:00", "01:00"))
        self.assertFalse(is_within("18:00", "02:00", "10:00"))
        self.assertFalse(is_within("18:00", "02:00", "02:00"))

    def test_malformed_times_yield_none_not_false(self):
        """
        None means "we do not know", which the app hides. False would be a
        claim that the salon is shut, which is a different and wrong message.
        """
        for bad in ("25:00", "9:00", "", None, "ab:cd", "09:60"):
            self.assertIsNone(is_within(bad, "22:00", "14:00"))

    def test_missing_day_row_is_unknown(self):
        """A grid with no Tuesday row says nothing about Tuesday."""
        result = resolve(self.MON, None, None, 1, "14:00")
        self.assertIsNone(result["is_open"])
        self.assertIsNone(result["hours_today"])

    def test_closed_day(self):
        weekly = [{"day": "sun", "closed": True}]
        result = resolve(weekly, None, None, 6, "14:00")
        self.assertFalse(result["is_open"])
        self.assertEqual(result["hours_today"], "Closed")

    def test_exception_replaces_the_weekly_row(self):
        """
        An exception changes the HOURS; open/closed is still computed from it.
        14:00 falls inside the weekly 09:00-22:00 and outside the exception's
        16:00-20:00, so the exception must win.
        """
        exc = {"closed": False, "open": "16:00", "close": "20:00"}
        self.assertFalse(resolve(self.MON, exc, None, 0, "14:00")["is_open"])
        self.assertTrue(resolve(self.MON, exc, None, 0, "17:00")["is_open"])

    def test_exception_can_close_an_open_day(self):
        result = resolve(self.MON, {"closed": True}, None, 0, "14:00")
        self.assertFalse(result["is_open"])
        self.assertEqual(result["hours_today"], "Closed")

    def test_manual_state_overrules_the_computed_answer(self):
        """
        A state REPLACES the answer rather than feeding it. BUSY is not a time
        and no grid can produce it, which is the whole reason it exists.
        """
        busy = resolve(self.MON, None, "BUSY", 0, "14:00")
        self.assertTrue(busy["is_open"])
        self.assertEqual(busy["status"], "BUSY")

        shut = resolve(self.MON, None, "CLOSED", 0, "14:00")
        self.assertFalse(shut["is_open"])
        self.assertEqual(shut["hours_today"], "Closed")

    def test_manual_state_can_open_outside_grid_hours(self):
        """08:00 is before opening, but the salon says it is open."""
        result = resolve(self.MON, None, "WALK_INS", 0, "08:00")
        self.assertTrue(result["is_open"])

    def test_unknown_state_is_ignored(self):
        """A value from a newer platform release must not break the badge."""
        result = resolve(self.MON, None, "PARTY_MODE", 0, "14:00")
        self.assertTrue(result["is_open"])
        self.assertEqual(result["status"], "OPEN")

    def test_twelve_hour_formatting(self):
        result = resolve(self.MON, None, None, 0, "14:00")
        self.assertEqual(result["hours_today"], "9:00 AM - 10:00 PM")
        self.assertEqual(result["opens_at"], "9:00 AM")
        self.assertEqual(result["closes_at"], "10:00 PM")
        self.assertEqual(result["status_line"], "Closes at 10:00 PM")

    def test_closed_right_now_still_returns_both_times(self):
        """
        The bug this locks down: `closes_at` used to be derived from the live
        answer, so it held "Closes at 10:00 PM" when open and "Opens at 9:00
        AM" when shut — one key meaning two things, and never both. A salon
        shut at 8am therefore reported no closing time at all.

        Both fields now come off today's grid row, so a shut salon still says
        when it closes and an open one still says when it opened.
        """
        shut = resolve(self.MON, None, None, 0, "08:00")
        self.assertFalse(shut["is_open"])
        self.assertEqual(shut["opens_at"], "9:00 AM")
        self.assertEqual(shut["closes_at"], "10:00 PM")

        # The other half of the same bug, from the open side.
        openn = resolve(self.MON, None, None, 0, "14:00")
        self.assertTrue(openn["is_open"])
        self.assertEqual(openn["opens_at"], "9:00 AM")
        self.assertEqual(openn["closes_at"], "10:00 PM")

        # Unchanged behaviour, asserted so a later edit cannot quietly fold
        # the prose back into closes_at.
        self.assertEqual(shut["status_line"], "Opens at 9:00 AM")
        self.assertEqual(openn["status_line"], "Closes at 10:00 PM")
        self.assertEqual(openn["hours_today"], "9:00 AM - 10:00 PM")

    def test_overnight_window_fills_both_times(self):
        """
        A salon open 18:00 to 02:00 closes on the NEXT calendar day.

        Reading the two times straight off the row is what makes the wrap a
        non-event for them: only is_open has to reason about midnight, and it
        is checked at all four interesting moments — before opening, in the
        evening, after midnight while still open, and after closing.

        Worth pinning because the obvious "next opening time" implementation
        gets this wrong: at 01:00 the salon is open, and a field that tried to
        report the next opening would have to say 6:00 PM *today*, which is
        both in the future and not when this shift started.
        """
        night = [{"day": "fri", "closed": False, "open": "18:00", "close": "02:00"}]

        for now, is_open in (("10:00", False), ("19:00", True), ("01:00", True), ("03:00", False)):
            result = resolve(night, None, None, 4, now)
            self.assertIs(result["is_open"], is_open, msg=now)
            self.assertEqual(result["opens_at"], "6:00 PM", msg=now)
            self.assertEqual(result["closes_at"], "2:00 AM", msg=now)
            self.assertEqual(result["hours_today"], "6:00 PM - 2:00 AM", msg=now)

    def test_unknown_hours_leave_every_time_field_empty(self):
        """
        No row for today means no times, and no sentence about times either.
        A status_line here would be prose invented out of nothing.
        """
        result = resolve(self.MON, None, None, 1, "14:00")
        self.assertIsNone(result["opens_at"])
        self.assertIsNone(result["closes_at"])
        self.assertIsNone(result["status_line"])

    def test_manual_close_clears_todays_times(self):
        """
        Shut by hand, so the published window is not what happens today.
        Leaving 10:00 PM in closes_at would print "Closes at 10:00 PM" beside
        a CLOSED badge — the exact contradiction the manual state resolves.
        """
        result = resolve(self.MON, None, "CLOSED", 0, "14:00")
        self.assertEqual(result["hours_today"], "Closed")
        self.assertIsNone(result["opens_at"])
        self.assertIsNone(result["closes_at"])
        self.assertIsNone(result["status_line"])

    def test_closed_day_has_no_times(self):
        result = resolve([{"day": "sun", "closed": True}], None, None, 6, "14:00")
        self.assertIsNone(result["opens_at"])
        self.assertIsNone(result["closes_at"])

    def test_midnight_and_noon_format_as_twelve(self):
        weekly = [{"day": "mon", "closed": False, "open": "00:00", "close": "12:00"}]
        self.assertEqual(
            resolve(weekly, None, None, 0, "06:00")["hours_today"],
            "12:00 AM - 12:00 PM",
        )


class MoneyTests(SimpleTestCase):
    def test_minor_to_major(self):
        self.assertEqual(major(19900), Decimal("199"))
        self.assertEqual(major(1), Decimal("0.01"))
        self.assertEqual(major(0), Decimal(0))

    def test_returns_decimal_not_float(self):
        """
        Float money accumulates error: 0.1 + 0.2 is not 0.3 in IEEE 754.
        Storing minor units and dividing with Decimal is what avoids it.
        """
        self.assertIsInstance(major(19900), Decimal)

    def test_bad_input_is_none_not_zero(self):
        """Zero is a price. Unknown is not, and must not render as free."""
        for bad in (None, "abc", [], {}):
            self.assertIsNone(major(bad))

    def test_bps_to_percent(self):
        self.assertEqual(bps_to_percent(2500), 25)
        self.assertEqual(bps_to_percent(0), 0)
        self.assertIsNone(bps_to_percent(None))


class TranslateTests(SimpleTestCase):
    def test_known_amenities_map_and_unknown_drop(self):
        """
        The eight keys are a closed set. A ninth value from a future release
        must be dropped, not passed through: the app has no icon for it.
        """
        self.assertEqual(
            translate.amenities(["WIFI", "PARKING", "ROOFTOP"]),
            ["wifi", "parking"],
        )

    def test_amenities_tolerates_non_lists(self):
        for junk in (None, "WIFI", {}, 5):
            self.assertEqual(translate.amenities(junk), [])

    def test_handles_become_urls(self):
        links = translate.social_links({"instagram": "theironrazor"})
        self.assertEqual(links[0]["url"], "https://instagram.com/theironrazor")

    def test_leading_at_is_stripped(self):
        """A pasted "@name" must not produce tiktok.com/@@name."""
        links = translate.social_links({"tiktok": "@theironrazor"})
        self.assertEqual(links[0]["url"], "https://tiktok.com/@theironrazor")

    def test_hidden_network_is_omitted_even_with_a_handle(self):
        """
        The platform stores what is HIDDEN, not what is shown, precisely so a
        switched-off network with a stored handle is unambiguous. Publishing
        it would put a link on the page the salon deliberately turned off.
        """
        links = translate.social_links({
            "instagram": "keepme",
            "youtube": "hideme",
            "hidden": ["YOUTUBE"],
        })
        platforms = [link["platform"] for link in links]
        self.assertIn("instagram", platforms)
        self.assertNotIn("youtube", platforms)

    def test_all_hidden_yields_nothing(self):
        links = translate.social_links({
            "instagram": "a",
            "website": "https://x.ae",
            "hidden": ["INSTAGRAM", "WEBSITE"],
        })
        self.assertEqual(links, [])

    def test_whatsapp_is_stripped_to_digits(self):
        """wa.me takes digits only; a pasted +971 50 123 4567 is a dead link."""
        links = translate.social_links({"whatsapp": "+971 50 123 4567"})
        self.assertEqual(links[0]["url"], "https://wa.me/971501234567")

    def test_whatsapp_with_no_digits_is_dropped(self):
        self.assertEqual(translate.social_links({"whatsapp": "call me"}), [])

    def test_website_is_emitted_as_is(self):
        """Every other field is a handle; website is already a URL."""
        links = translate.social_links({"website": "https://ironrazor.ae"})
        self.assertEqual(links[0]["url"], "https://ironrazor.ae")

    def test_blank_handles_are_skipped(self):
        self.assertEqual(translate.social_links({"instagram": "   "}), [])

    def test_socials_tolerates_non_dicts(self):
        for junk in (None, [], "instagram", 7):
            self.assertEqual(translate.social_links(junk), [])

    def test_price_tier_collapses_four_into_three(self):
        """
        The platform has four tiers and the app draws three signs. UPSCALE and
        PREMIUM both land on 3, which is lossy and accepted.
        """
        self.assertEqual(translate.price_level("BUDGET"), 1)
        self.assertEqual(translate.price_level("MID_RANGE"), 2)
        self.assertEqual(translate.price_level("UPSCALE"), 3)
        self.assertEqual(translate.price_level("PREMIUM"), 3)
        self.assertIsNone(translate.price_level("LUXURY"))
        self.assertIsNone(translate.price_level(None))

    def test_audience_mode(self):
        self.assertEqual(translate.category("GENTS"), "gents")
        self.assertIsNone(translate.category("MALE"))

    def test_deposit_policy(self):
        policy = translate.booking_policy("PERCENT", 2000, 24)
        self.assertTrue(policy["deposit_required"])
        self.assertEqual(policy["deposit_percentage"], 20)
        self.assertTrue(policy["free_cancellation"])

    def test_no_deposit_zeroes_the_percentage(self):
        policy = translate.booking_policy("NONE", 2000, 0)
        self.assertFalse(policy["deposit_required"])
        self.assertEqual(policy["deposit_percentage"], 0)
        self.assertFalse(policy["free_cancellation"])


class BoundingBoxTests(SimpleTestCase):
    """
    What DiscoverMapViewTests used to cover, minus the lie.

    Those three tests created `salons_salon` rows and asserted the map endpoint
    returned them. It did — but only because map_venues() swallowed the failing
    `storefront` query (that table does not exist in the test database) and fell
    back to the demo model. They were green proof of a bug. The fallback is gone,
    so the real arithmetic is tested here instead, where no database is needed.
    """

    def test_delta_is_a_full_span_not_a_radius(self):
        # 0.12 of span around 24.55 is ±0.06, not ±0.12. Getting this backwards
        # doubles the viewport and is invisible without an assertion.
        sw_lat, sw_lng, ne_lat, ne_lng = bounding_box(24.55, 90.40, 0.12, 0.12)
        self.assertAlmostEqual(sw_lat, 24.49)
        self.assertAlmostEqual(ne_lat, 24.61)
        self.assertAlmostEqual(sw_lng, 90.34)
        self.assertAlmostEqual(ne_lng, 90.46)

    def test_a_point_inside_and_a_point_outside(self):
        sw_lat, sw_lng, ne_lat, ne_lng = bounding_box(24.5547702, 90.4080668, 0.12, 0.12)
        self.assertTrue(sw_lat <= 24.554 <= ne_lat and sw_lng <= 90.408 <= ne_lng)
        self.assertFalse(sw_lat <= 25.554 <= ne_lat and sw_lng <= 91.408 <= ne_lng)

    def test_missing_component_means_no_box_at_all(self):
        # No box means an unfiltered query, which is what "no region params"
        # has to mean. Defaulting a missing delta to zero would instead return
        # only salons standing exactly on the centre point.
        self.assertIsNone(bounding_box(24.55, 90.40, None, 0.12))
        self.assertIsNone(bounding_box(None, None, None, None))

    def test_negative_span_is_not_an_inverted_box(self):
        self.assertEqual(
            bounding_box(24.55, 90.40, -0.12, -0.12),
            bounding_box(24.55, 90.40, 0.12, 0.12),
        )


class RadiusBoxTests(SimpleTestCase):
    """
    The prefilter behind ?radius=. Same reasoning as BoundingBoxTests above:
    pure arithmetic, no platform tables, so it is the one part of the radius
    filter a test can actually reach.
    """

    # Dubai. Far enough from the equator that the cos(latitude) correction is
    # visible — about 10% — and close enough that a bug there is easy to miss.
    DUBAI = (25.19, 55.26)

    def test_longitude_span_is_wider_than_latitude_span(self):
        """
        THE ONE THING THIS FUNCTION EXISTS TO GET RIGHT. A degree of longitude
        is shorter than a degree of latitude everywhere but the equator, so
        covering the same distance east-west takes MORE degrees. A version
        that reused the latitude span for both would fail here.
        """
        sw_lat, sw_lng, ne_lat, ne_lng = radius_box(*self.DUBAI, 10.0)
        lat_span = ne_lat - sw_lat
        lng_span = ne_lng - sw_lng
        self.assertGreater(lng_span, lat_span)
        # 1/cos(25.19 deg) is about 1.105.
        self.assertAlmostEqual(lng_span / lat_span, 1.105, places=2)

    @staticmethod
    def _haversine_km(lat1, lng1, lat2, lng2):
        """
        Distance between two points, written out longhand.

        Deliberately the asin form while radius_box and with_distance use the
        acos one. A test that reproduces the implementation's own arithmetic
        proves only that the code equals itself; this is a second opinion.
        """
        radius = 6371.0
        p1, p2 = math.radians(lat1), math.radians(lat2)
        a = (
            math.sin((p2 - p1) / 2) ** 2
            + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
        )
        return 2 * radius * math.asin(math.sqrt(a))

    def test_the_northern_edge_sits_exactly_the_radius_away(self):
        lat, lng = self.DUBAI
        _, _, ne_lat, _ = radius_box(lat, lng, 10.0)
        self.assertAlmostEqual(self._haversine_km(lat, lng, ne_lat, lng), 10.0, places=6)

    def test_the_box_contains_the_circle(self):
        """
        A prefilter that cuts INSIDE the radius drops real results, and does
        it silently: the haversine behind it never sees the row, so a salon
        4.9 km away simply is not in a 5 km search. Every edge has to sit at
        or beyond the radius, east and west included — that is the edge the
        cos(latitude) term is there to push out far enough.
        """
        lat, lng = self.DUBAI
        sw_lat, sw_lng, ne_lat, ne_lng = radius_box(lat, lng, 5.0)
        for point in ((ne_lat, lng), (sw_lat, lng), (lat, ne_lng), (lat, sw_lng)):
            self.assertGreaterEqual(
                self._haversine_km(lat, lng, *point), 5.0 - 1e-6, msg=f"edge {point}"
            )

    def test_missing_component_means_no_box(self):
        """Read by the caller as "no radius filter", not "a radius of zero"."""
        self.assertIsNone(radius_box(None, 55.26, 5.0))
        self.assertIsNone(radius_box(25.19, None, 5.0))
        self.assertIsNone(radius_box(25.19, 55.26, None))

    def test_non_positive_radius_is_not_a_radius(self):
        self.assertIsNone(radius_box(*self.DUBAI, 0))
        self.assertIsNone(radius_box(*self.DUBAI, -5))

    def test_a_pole_does_not_divide_by_zero(self):
        """cos(90 deg) is 0. Every meridian is within any radius up there."""
        _, sw_lng, _, ne_lng = radius_box(90.0, 0.0, 5.0)
        self.assertEqual((sw_lng, ne_lng), (-180.0, 180.0))

    def test_spans_stay_on_the_globe(self):
        """A radius larger than the planet clamps rather than overflowing."""
        sw_lat, sw_lng, ne_lat, ne_lng = radius_box(0.0, 0.0, 40000.0)
        self.assertEqual((sw_lat, ne_lat), (-90.0, 90.0))
        self.assertEqual((sw_lng, ne_lng), (-180.0, 180.0))


class FormatDistanceTests(SimpleTestCase):
    def test_kilometres_get_one_decimal(self):
        self.assertEqual(format_distance(2.04), "2.0 km")
        self.assertEqual(format_distance(12.35), "12.3 km")

    def test_under_a_kilometre_reads_in_metres(self):
        """"0.4 km" is not how anyone describes a four-minute walk."""
        self.assertEqual(format_distance(0.4), "400 m")
        self.assertEqual(format_distance(0.085), "85 m")

    def test_just_under_a_kilometre_does_not_print_1000_m(self):
        """0.9996 km rounds to 1000 metres, which has to read as 1.0 km."""
        self.assertEqual(format_distance(0.9996), "1.0 km")

    def test_absent_or_nonsense_stays_none(self):
        """
        Null means "no location was sent". "0 km" would put every salon on the
        customer's doorstep.
        """
        self.assertIsNone(format_distance(None))
        self.assertIsNone(format_distance("nearby"))
        self.assertIsNone(format_distance(-3))


class DiscoveryParamsTests(SimpleTestCase):
    """
    The /discover query string. The rule under test throughout: ABSENT is not
    INVALID. A parameter nobody sent is no filter; a parameter somebody sent
    and got wrong is an error naming it, never a filter that quietly did
    nothing.
    """

    def test_empty_query_is_all_filters_off(self):
        p = parse_discovery({})
        self.assertIsNone(p["latitude"])
        self.assertIsNone(p["category"])
        self.assertIsNone(p["search"])
        self.assertFalse(p["is_top_rated"])
        self.assertFalse(p["is_open_now"])

    def test_coordinates_are_read_from_the_long_names(self):
        self.assertEqual(parse_discovery({"latitude": "25.19", "longitude": "55.26"})["longitude"], 55.26)

    def test_renamed_params_are_an_error_naming_the_new_name(self):
        """
        lat, lng, lon and open_now were removed on 2026-09-10. Ignoring them
        would hand an old build the national list with a 200 and a filter
        that looked applied.
        """
        for old, new in (
            ("lat", "latitude"),
            ("lng", "longitude"),
            ("lon", "longitude"),
            ("open_now", "is_open_now"),
        ):
            with self.subTest(old):
                with self.assertRaises(ParamError) as ctx:
                    parse_discovery({old: "1"})
                self.assertEqual(ctx.exception.param, old)
                self.assertEqual(ctx.exception.message, f"{old} was renamed to {new}.")

    def test_a_renamed_param_sent_empty_is_still_absent(self):
        """
        `lat=` is what an old build sends when location is denied. It asked
        for no filter, and an empty value is absent everywhere else here.
        """
        p = parse_discovery({"lat": "", "lng": "", "open_now": ""})
        self.assertIsNone(p["latitude"])
        self.assertFalse(p["is_open_now"])

    def test_empty_string_is_absent_not_broken(self):
        """
        The app sends `latitude=` with nothing after it when the customer
        denies location permission. That is a list request, not a 422.
        """
        p = parse_discovery({"latitude": "", "longitude": "", "search": "  "})
        self.assertIsNone(p["latitude"])
        self.assertIsNone(p["search"])

    def test_unreadable_number_is_an_error_naming_it(self):
        """
        The old view caught ValueError and carried on, so a comma decimal from
        a European locale dropped the location and returned the national list
        with a 200.
        """
        with self.assertRaises(ParamError) as ctx:
            parse_discovery({"latitude": "25,19", "longitude": "55.26"})
        self.assertEqual(ctx.exception.param, "latitude")

    def test_nan_and_infinity_are_rejected(self):
        """float() accepts both, and both poison every comparison downstream."""
        for bad in ("nan", "inf", "-inf"):
            with self.assertRaises(ParamError):
                parse_discovery({"latitude": bad, "longitude": "55.26"})

    def test_impossible_coordinates_are_rejected(self):
        with self.assertRaises(ParamError):
            parse_discovery({"latitude": "500", "longitude": "55.26"})

    def test_half_a_coordinate_is_not_a_location(self):
        """
        Defaulting the missing half to zero would sort the whole country by
        its distance from a point in the Atlantic.
        """
        with self.assertRaises(ParamError) as ctx:
            parse_discovery({"latitude": "25.19"})
        self.assertEqual(ctx.exception.param, "longitude")

    def test_radius_is_kilometres_and_must_be_positive(self):
        p = parse_discovery({"latitude": "25.19", "longitude": "55.26", "radius": "7.5"})
        self.assertEqual(p["radius_km"], 7.5)
        with self.assertRaises(ParamError):
            parse_discovery({"latitude": "25.19", "longitude": "55.26", "radius": "0"})

    def test_radius_without_a_centre_is_an_error(self):
        """"Within 5 km" of nowhere is not a question with an answer."""
        with self.assertRaises(ParamError) as ctx:
            parse_discovery({"radius": "5"})
        self.assertEqual(ctx.exception.param, "radius")

    def test_all_is_the_absence_of_the_category_filter(self):
        self.assertIsNone(parse_discovery({"category": "all"})["category"])
        self.assertEqual(parse_discovery({"category": "GENTS"})["category"], "gents")

    def test_unknown_category_is_rejected_not_ignored(self):
        """
        Ignoring it returns every salon under a chip the customer tapped,
        which looks exactly like a filter that works and finds a lot.
        """
        with self.assertRaises(ParamError) as ctx:
            parse_discovery({"category": "girls"})
        self.assertEqual(ctx.exception.param, "category")

    def test_booleans_accept_the_spellings_an_api_actually_receives(self):
        for raw in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(parse_discovery({"is_open_now": raw})["is_open_now"])
        for raw in ("0", "false", "no", "off"):
            self.assertFalse(parse_discovery({"is_open_now": raw})["is_open_now"])

    def test_a_boolean_that_is_neither_is_an_error(self):
        """
        Reading `maybe` as false would hide every open salon and look like
        empty inventory rather than a bad request.
        """
        with self.assertRaises(ParamError):
            parse_discovery({"is_top_rated": "maybe"})

    def test_hijab_mode_sets_hijab_only(self):
        self.assertTrue(parse_discovery({"hijab_mode": "1"})["hijab_only"])

    def test_sorting_by_distance_needs_a_location(self):
        """
        Falling back to rating order is the trap: the list comes back looking
        sorted, just not by what was asked for, and says nothing about it.
        """
        with self.assertRaises(ParamError) as ctx:
            parse_discovery({"sort": "distance"})
        self.assertEqual(ctx.exception.param, "sort")

    def test_search_is_trimmed_and_length_capped(self):
        self.assertEqual(parse_discovery({"search": "  iron razor "})["search"], "iron razor")
        with self.assertRaises(ParamError):
            parse_discovery({"search": "x" * 101})


class BranchTimezoneTests(SimpleTestCase):
    """
    The fallback that used to be invisible.

    These pin the LOG LINE, not the timezone. The timezone behaviour is
    deliberately unchanged — a test that only asserted the returned ZoneInfo
    would have passed against the `or "Asia/Dubai"` this replaced, and so
    would have proved nothing about the change.
    """

    LOGGER = "apps.salons.timezones"

    def test_a_set_timezone_is_used_and_says_nothing(self):
        """
        The quiet path has to stay quiet. A warning per salon per request on a
        fifteen-card page is affordable only while it means something.
        """
        with self.assertNoLogs(self.LOGGER, level="WARNING"):
            tz = timezones.resolve("Europe/London", "salon-1")
        self.assertEqual(str(tz), "Europe/London")

    def test_a_null_timezone_warns_and_names_the_salon(self):
        """
        Naming the salon is the whole point: "some branch somewhere has no
        timezone" is not actionable, and the id is what turns the warning into
        a row somebody can go and fix.
        """
        with self.assertLogs(self.LOGGER, level="WARNING") as caught:
            tz = timezones.resolve(None, "3333-abc")

        self.assertEqual(str(tz), timezones.DEFAULT_TIMEZONE)
        self.assertEqual(len(caught.records), 1)
        self.assertIn("3333-abc", caught.output[0])
        self.assertIn(timezones.DEFAULT_TIMEZONE, caught.output[0])
        # Also a structured field, so Loki can count these without grepping
        # the message. See config/observability.py.
        self.assertEqual(caught.records[0].salon_id, "3333-abc")

    def test_an_empty_string_falls_back_the_same_way(self):
        """
        `"" or DEFAULT` fell back before this helper existed, so `""` has to
        keep falling back now. A version that checked `is None` would leave
        empty-string rows constructing ZoneInfo("") and raising.
        """
        with self.assertLogs(self.LOGGER, level="WARNING"):
            tz = timezones.resolve("", "salon-2")
        self.assertEqual(str(tz), timezones.DEFAULT_TIMEZONE)

    def test_a_broken_timezone_still_raises(self):
        """
        A branch whose timezone reads "GMT+4" is a broken row, not a missing
        one. Rounding it to Dubai here would hide a data error behind the same
        fallback that hides an absent value, and the two want different fixes.
        """
        with self.assertRaises(zoneinfo.ZoneInfoNotFoundError):
            timezones.resolve("Definitely/Not_A_Zone", "salon-3")


class MapParamsTests(SimpleTestCase):
    def test_radius_metres_becomes_kilometres(self):
        result = parse_map({"latitude": "25.2", "longitude": "55.27", "radius": "5000"})
        self.assertEqual(result["radius_km"], 5.0)

    def test_broken_latitude_is_an_error(self):
        with self.assertRaises(ParamError):
            parse_map({"latitude": "abc", "longitude": "55.27"})

    def test_comma_decimal_is_an_error(self):
        with self.assertRaises(ParamError):
            parse_map({"latitude": "25,19", "longitude": "55.27"})

    def test_latitude_out_of_range_is_an_error(self):
        with self.assertRaises(ParamError):
            parse_map({"latitude": "500", "longitude": "55.27"})

    def test_half_a_coordinate_is_an_error(self):
        with self.assertRaises(ParamError):
            parse_map({"latitude": "25.2"})

    def test_radius_without_coordinates_is_an_error(self):
        with self.assertRaises(ParamError):
            parse_map({"radius": "5000"})

    def test_empty_request_is_fine(self):
        result = parse_map({})
        self.assertIsNone(result["latitude"])
        self.assertIsNone(result["radius_km"])

    def test_renamed_coordinates_are_an_error(self):
        """Removed 2026-09-10, same as on /discover."""
        for old in ("lat", "lng", "lon"):
            with self.subTest(old):
                with self.assertRaises(ParamError) as ctx:
                    parse_map({old: "25.2"})
                self.assertEqual(ctx.exception.param, old)


class RenamedParamResponseTests(SimpleTestCase):
    """
    The 422 itself, through the real views and the project's error envelope.

    Still no database: both views parse the query string before they run a
    query, and a renamed param stops them there. SimpleTestCase refuses a
    connection outright, so if either view ever starts querying before it
    parses, these fail rather than quietly passing.
    """

    def assert_renamed(self, url, old, new):
        response = self.client.get(url, {old: "25.2"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["errors"],
            [{"field": old, "code": "invalid", "message": f"{old} was renamed to {new}."}],
        )

    def test_discover(self):
        for old, new in (
            ("lat", "latitude"),
            ("lng", "longitude"),
            ("lon", "longitude"),
            ("open_now", "is_open_now"),
        ):
            with self.subTest(old):
                self.assert_renamed("/api/v1/discover", old, new)

    def test_discover_map(self):
        for old, new in (("lat", "latitude"), ("lng", "longitude"), ("lon", "longitude")):
            with self.subTest(old):
                self.assert_renamed("/api/v1/discover/map", old, new)


class NextOpeningTests(SimpleTestCase):
    """
    When does this salon open next?

    Only asked when the salon is shut. The answer walks FORWARD from tomorrow,
    never today: today is already known to be closed, so including it would
    hand back a time that has passed or never happens.
    """

    WEEK = [
        {"day": "mon", "closed": True},
        {"day": "tue", "closed": False, "open": "09:00", "close": "21:00"},
        {"day": "wed", "closed": False, "open": "10:00", "close": "20:00"},
        {"day": "thu", "closed": True},
        {"day": "fri", "closed": True},
        {"day": "sat", "closed": True},
        {"day": "sun", "closed": True},
    ]

    def test_opens_tomorrow(self):
        # Monday, closed. Tuesday is open.
        self.assertEqual(next_opening(self.WEEK, 0), (1, "09:00"))

    def test_skips_closed_days(self):
        # Thursday closed. Fri, Sat, Sun, Mon closed too. Next is Tuesday,
        # five days out, which is what makes the week wrap worth a test: a
        # version that stopped at Sunday would return nothing at all.
        self.assertEqual(next_opening(self.WEEK, 3), (5, "09:00"))

    def test_closed_all_week(self):
        """
        None, not a guess. A salon shut every day has no next opening, and
        inventing one would print a time nobody can turn up at.
        """
        shut = [{"day": d, "closed": True} for d in
                ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]]
        self.assertIsNone(next_opening(shut, 0))

    def test_next_opening_at_gives_a_real_datetime(self):
        # Thursday 10 Sept 2026, 2pm Dubai. Thu, Fri, Sat, Sun, Mon are all
        # closed in WEEK, so the next opening is Tuesday the 15th at 9am.
        dubai = zoneinfo.ZoneInfo("Asia/Dubai")
        now = datetime(2026, 9, 10, 14, 0, tzinfo=dubai)

        result = next_opening_at(self.WEEK, now)
        self.assertEqual(result.isoformat(), "2026-09-15T09:00:00+04:00")

class ServiceIdsParamTests(SimpleTestCase):
    """
    The `service_ids` filter, parsed.

    Three clients spell a list three different ways and all three are already
    in the wild, so the parser takes all three and everything downstream sees
    one list of UUIDs.
    """

    A = uuid.UUID("66666666-6666-6666-6666-666666666660")
    B = uuid.UUID("66666666-6666-6666-6666-666666666661")

    def test_comma_separated(self):
        params = QueryDict(f"service_ids={self.A},{self.B}")
        self.assertEqual(parse_service_ids(params), [self.A, self.B])

    def test_repeated_parameter(self):
        params = QueryDict(f"service_ids={self.A}&service_ids={self.B}")
        self.assertEqual(parse_service_ids(params), [self.A, self.B])

    def test_axios_bracket_spelling(self):
        """What axios 1.x sends for an array, and what a plain getlist misses."""
        params = QueryDict(f"service_ids[]={self.A}&service_ids[]={self.B}")
        self.assertEqual(parse_service_ids(params), [self.A, self.B])

    def test_absent_and_empty_mean_no_filter(self):
        # Not an error: the Stylists tab sends neither, and a booking flow
        # whose basket has not loaded yet sends an empty one. Both want the
        # whole roster.
        for query in ("", "service_ids=", "service_ids=,,"):
            with self.subTest(query):
                self.assertEqual(parse_service_ids(QueryDict(query)), [])

    def test_whitespace_is_tolerated(self):
        params = QueryDict(f"service_ids= {self.A} , {self.B} ")
        self.assertEqual(parse_service_ids(params), [self.A, self.B])

    def test_duplicates_collapse_and_order_holds(self):
        """
        The response lists each stylist's covered services in the order the
        customer picked, so the parsed order is part of the contract.
        """
        params = QueryDict(f"service_ids={self.B},{self.A},{self.B}")
        self.assertEqual(parse_service_ids(params), [self.B, self.A])

    def test_garbage_is_rejected(self):
        with self.assertRaises(ParamError) as ctx:
            parse_service_ids(QueryDict("service_ids=not-a-uuid"))
        self.assertEqual(ctx.exception.param, "service_ids")

    def test_too_many_is_rejected(self):
        """
        A basket cannot be 51 services long. The cap keeps a hand-written query
        string from turning into an unbounded IN list.
        """
        ids = ",".join(str(uuid.uuid4()) for _ in range(MAX_SERVICE_IDS + 1))
        with self.assertRaises(ParamError):
            parse_service_ids(QueryDict(f"service_ids={ids}"))


class SkillLadderTests(SimpleTestCase):
    """The two scales, and the mapping between them."""

    def test_stage_level_maps_one_to_five_onto_four_rungs(self):
        self.assertEqual(
            [skills.stage_level(n) for n in (1, 2, 3, 4, 5)],
            ["TRAINEE", "JUNIOR", "SENIOR", "MASTER", "MASTER"],
        )

    def test_out_of_range_clamps_rather_than_crashes(self):
        # A stage saved as 0 or 9 by some future build must still answer the
        # question. 0 asks for the least, 9 for the most.
        self.assertEqual(skills.stage_level(0), "TRAINEE")
        self.assertEqual(skills.stage_level(99), "MASTER")
        self.assertEqual(skills.stage_level(None), "TRAINEE")

    def test_unknown_level_never_qualifies(self):
        """
        A rung this build has not heard of ranks below every real one, so a
        holder of it is not silently treated as a MASTER.
        """
        self.assertLess(skills.rank("GRANDMASTER"), skills.rank("TRAINEE"))


class SkillBridgeTests(SimpleTestCase):
    """
    catalog_skill ↔ the tenant's own skill catalogue, matched on `code`.

    Nothing joins these two tables; the platform's own eligibility handler
    bridges them by code, and this has to agree with it.
    """

    def test_matches_ignoring_case_and_padding(self):
        bridged = skills.bridge(
            [{"id": "cs1", "code": "HAIRCUT"}],
            [{"id": "sk1", "code": "  haircut "}],
        )
        self.assertEqual(bridged, {"cs1": "sk1"})

    def test_unmatched_catalog_skill_is_present_as_none(self):
        """
        None, not absent. A skill the tenant never added is a requirement
        nobody can meet; dropping the key would leave the service looking
        like it required nothing at all.
        """
        bridged = skills.bridge([{"id": "cs1", "code": "COLOUR"}], [])
        self.assertEqual(bridged, {"cs1": None})

    def test_codeless_rows_never_match(self):
        # `skill.code` is nullable and `catalog_skill.code` can be blank. Two
        # rows with no code are not the same skill.
        bridged = skills.bridge(
            [{"id": "cs1", "code": None}, {"id": "cs2", "code": ""}],
            [{"id": "sk1", "code": None}],
        )
        self.assertEqual(bridged, {"cs1": None, "cs2": None})

    def test_duplicate_tenant_codes_resolve_stably(self):
        """
        `skill.code` is not unique per tenant. First row read wins, so the
        answer does not depend on the order Postgres happened to return.
        """
        bridged = skills.bridge(
            [{"id": "cs1", "code": "CUT"}],
            [{"id": "first", "code": "cut"}, {"id": "second", "code": "CUT"}],
        )
        self.assertEqual(bridged, {"cs1": "first"})


class ServiceRequirementTests(SimpleTestCase):
    """What one person has to hold to perform a whole service."""

    BRIDGE = {"cs_cut": "sk_cut", "cs_colour": "sk_colour", "cs_nails": None}

    def test_stages_of_one_skill_fold_to_the_highest_level(self):
        """
        A colour service with a SENIOR step and a TRAINEE step needs a SENIOR
        colourist, not two people.
        """
        required = skills.requirements(
            [
                {"service_id": "svc", "skill_id": "cs_colour", "min_level": 3},
                {"service_id": "svc", "skill_id": "cs_colour", "min_level": 1},
            ],
            self.BRIDGE,
        )
        self.assertEqual(required, {"svc": {"sk_colour": "SENIOR"}})

    def test_different_skills_stay_separate(self):
        required = skills.requirements(
            [
                {"service_id": "svc", "skill_id": "cs_cut", "min_level": 2},
                {"service_id": "svc", "skill_id": "cs_colour", "min_level": 4},
            ],
            self.BRIDGE,
        )
        self.assertEqual(
            required, {"svc": {"sk_cut": "JUNIOR", "sk_colour": "MASTER"}}
        )

    def test_service_without_stages_is_absent(self):
        """
        Absent, not empty. The view turns this into a 422: a service requiring
        no skill would otherwise qualify every stylist in the building.
        """
        self.assertEqual(skills.requirements([], self.BRIDGE), {})

    def test_unbridged_skill_becomes_a_requirement_nobody_holds(self):
        required = skills.requirements(
            [{"service_id": "svc", "skill_id": "cs_nails", "min_level": 1}],
            self.BRIDGE,
        )
        self.assertEqual(required, {"svc": {("catalog", "cs_nails"): "TRAINEE"}})
        self.assertFalse(skills.can_perform(required["svc"], {"sk_cut": "MASTER"}))


class CoverageTests(SimpleTestCase):
    """Which of the picked services each stylist can actually take."""

    REQUIRED = {
        "cut": {"sk_cut": "JUNIOR"},
        "colour": {"sk_cut": "JUNIOR", "sk_colour": "SENIOR"},
    }

    def levels(self, **held):
        return {"darius": held}

    def test_every_skill_or_nothing(self):
        """
        Partial coverage is not coverage. Holding the cut skill does not make
        someone bookable for a service that also needs colour.
        """
        covered = skills.coverage(
            ["cut", "colour"], self.REQUIRED, self.levels(sk_cut="MASTER")
        )
        self.assertEqual(covered, {"darius": ["cut"]})

    def test_level_is_a_floor_not_a_match(self):
        # SENIOR clears a JUNIOR requirement; TRAINEE does not.
        self.assertEqual(
            skills.coverage(["cut"], self.REQUIRED, self.levels(sk_cut="SENIOR")),
            {"darius": ["cut"]},
        )
        self.assertEqual(
            skills.coverage(["cut"], self.REQUIRED, self.levels(sk_cut="TRAINEE")),
            {},
        )

    def test_nobody_qualified_is_an_empty_dict(self):
        """
        Empty, never an exception. "No one here can do this" is a screen the
        app draws, not an error it apologises for.
        """
        self.assertEqual(skills.coverage(["colour"], self.REQUIRED, {}), {})

    def test_one_stylist_appears_once_with_every_service(self):
        covered = skills.coverage(
            ["cut", "colour"],
            self.REQUIRED,
            self.levels(sk_cut="MASTER", sk_colour="MASTER"),
        )
        self.assertEqual(covered, {"darius": ["cut", "colour"]})

    def test_order_follows_the_request(self):
        """The app groups the list by what the customer picked, in that order."""
        covered = skills.coverage(
            ["colour", "cut"],
            self.REQUIRED,
            self.levels(sk_cut="MASTER", sk_colour="MASTER"),
        )
        self.assertEqual(covered["darius"], ["colour", "cut"])

    def test_unknown_service_is_skipped_not_guessed(self):
        covered = skills.coverage(
            ["cut", "ghost"], self.REQUIRED, self.levels(sk_cut="MASTER")
        )
        self.assertEqual(covered, {"darius": ["cut"]})


class StylistOrderTests(SimpleTestCase):
    """Rating descending, then name — stable between refreshes either way."""

    def test_rating_first_then_name(self):
        rows = [
            {"name": "Zed", "rating": 4.9},
            {"name": "Amy", "rating": 4.9},
            {"name": "Bo", "rating": 5.0},
        ]
        rows.sort(key=_stylist_order)
        self.assertEqual([r["name"] for r in rows], ["Bo", "Amy", "Zed"])

    def test_unrated_sink_below_rated_and_sort_by_name(self):
        """
        Every rating is null today, so this is the ordering the app actually
        sees: pure name order, and nothing jumps when reviews start landing.
        """
        rows = [
            {"name": "Liam", "rating": None},
            {"name": "Darius", "rating": None},
            {"name": "Zed", "rating": 3.0},
        ]
        rows.sort(key=_stylist_order)
        self.assertEqual([r["name"] for r in rows], ["Zed", "Darius", "Liam"])

    def test_nameless_row_does_not_crash_the_sort(self):
        # `name` is null when a staff row has no user account behind it.
        rows = [{"name": None, "rating": None}, {"name": "Amy", "rating": None}]
        rows.sort(key=_stylist_order)
        self.assertEqual([r["name"] for r in rows], [None, "Amy"])


DUBAI = zoneinfo.ZoneInfo("Asia/Dubai")
DAY = datetime(2026, 9, 19, tzinfo=DUBAI)          # a Saturday, salon open 10-22


def at(hour, minute=0):
    """A moment on the test day, in the salon's own timezone."""
    return DAY + timedelta(hours=hour, minutes=minute)


class SpanTests(SimpleTestCase):
    """'HH:MM'-'HH:MM' as a real interval on one salon-local day."""

    def test_ordinary_day(self):
        self.assertEqual(slots.span(DAY, "10:00", "22:00"), (at(10), at(22)))

    def test_overnight_runs_into_the_next_day(self):
        """
        18:00 to 02:00 is eight hours, not minus sixteen. A salon open past
        midnight books past midnight, and the naive subtraction would hand
        back an interval that ends before it starts — silently bookable never.
        """
        self.assertEqual(slots.span(DAY, "18:00", "02:00"), (at(18), at(26)))

    def test_malformed_is_none_not_all_day(self):
        # The hours snapshot is JSONB and a shift time is free text. "No hours
        # known" must never widen into "open all day".
        for opens, closes in (("", "22:00"), (None, "22:00"), ("10:00", "ten"), ("25:00", "26:00")):
            with self.subTest(opens=opens, closes=closes):
                self.assertIsNone(slots.span(DAY, opens, closes))

    def test_break_is_an_hour_from_its_start(self):
        self.assertEqual(slots.break_span(DAY, "13:00"), (at(13), at(14)))
        self.assertIsNone(slots.break_span(DAY, None))


class IntervalMathTests(SimpleTestCase):
    """Merging and subtracting the intervals a day is made of."""

    def test_touching_intervals_merge(self):
        self.assertEqual(
            slots.merge([(at(10), at(12)), (at(12), at(14))]), [(at(10), at(14))]
        )

    def test_zero_length_intervals_vanish(self):
        self.assertEqual(slots.merge([(at(10), at(10))]), [])

    def test_a_break_splits_a_shift_in_two(self):
        """
        The whole reason this is interval arithmetic: an appointment may not
        straddle lunch, so a 10-18 shift with a 13:00 break is two stretches,
        not one long one with a note attached.
        """
        self.assertEqual(
            slots.subtract([(at(10), at(18))], [(at(13), at(14))]),
            [(at(10), at(13)), (at(14), at(18))],
        )

    def test_blocks_outside_the_shift_change_nothing(self):
        self.assertEqual(
            slots.subtract([(at(10), at(12))], [(at(14), at(15))]),
            [(at(10), at(12))],
        )

    def test_a_block_covering_everything_leaves_nothing(self):
        self.assertEqual(slots.subtract([(at(10), at(12))], [(at(9), at(13))]), [])

    def test_overlapping_blocks_do_not_double_cut(self):
        # Two bookings back to back, the second starting before the first ends
        # (which the platform allows for two different services on one row).
        self.assertEqual(
            slots.subtract(
                [(at(10), at(14))], [(at(11), at(12, 30)), (at(12), at(13))]
            ),
            [(at(10), at(11)), (at(13), at(14))],
        )


class AlignUpTests(SimpleTestCase):
    """Every start sits on the grid, counted from salon-local midnight."""

    def test_a_grid_point_is_left_alone(self):
        self.assertEqual(slots.align_up(at(10, 15), DAY), at(10, 15))

    def test_rounds_forward_never_back(self):
        # Backwards would offer a start before the customer's window began.
        self.assertEqual(slots.align_up(at(10, 1), DAY), at(10, 15))
        self.assertEqual(slots.align_up(at(10, 14, ), DAY), at(10, 15))

    def test_anchored_on_midnight_not_on_opening(self):
        """
        A salon opening at 09:30 still offers :00/:15/:30/:45, not :30/:45/:00
        shifted by its own opening time.
        """
        self.assertEqual(slots.align_up(at(9, 31), DAY), at(9, 45))


class StartsTests(SimpleTestCase):
    """Walking a free stretch on the grid."""

    FREE = [(at(10), at(12))]

    def starts(self, free=None, duration=30, window=(10, 12), earliest=None):
        return slots.starts(
            free if free is not None else self.FREE,
            duration,
            at(window[0]),
            at(window[1]),
            earliest if earliest is not None else at(0),
            DAY,
        )

    def test_walks_the_grid(self):
        self.assertEqual(
            self.starts(window=(10, 11)), [at(10), at(10, 15), at(10, 30), at(10, 45)]
        )

    def test_the_window_bounds_the_start_not_the_end(self):
        """
        A 45-minute service starting at 11:45 in a 10:00-12:00 window is a
        real offer as long as the stylist is free until 12:30. Bounding the
        END instead would hide the last slot before noon on every screen.
        """
        found = slots.starts(
            [(at(10), at(13))], 45, at(10), at(12), at(0), DAY
        )
        self.assertEqual(found[-1], at(11, 45))

    def test_an_appointment_must_fit_before_the_stretch_ends(self):
        # 11:45 + 30 = 12:15, past a shift ending at 12:00.
        self.assertEqual(self.starts()[-1], at(11, 30))

    def test_nothing_starts_before_now_plus_lead_time(self):
        self.assertEqual(
            self.starts(earliest=at(11, 5))[0], at(11, 15)
        )

    def test_a_window_entirely_in_the_past_is_empty_not_an_error(self):
        self.assertEqual(self.starts(earliest=at(23)), [])

    def test_each_free_stretch_is_walked_in_turn(self):
        found = slots.starts(
            [(at(10), at(11)), (at(14), at(15))], 60, at(9), at(18), at(0), DAY
        )
        self.assertEqual(found, [at(10), at(14)])

    def test_offers_come_back_in_time_order(self):
        # Two stylists' stretches can arrive in any order; the response is
        # sorted by start, so the walk is too.
        found = slots.starts(
            [(at(14), at(15)), (at(10), at(11))], 60, at(9), at(18), at(0), DAY
        )
        self.assertEqual(found, [at(10), at(14)])


class WindowParamTests(SimpleTestCase):
    """`from` and `to`, settled against the salon's own clock."""

    def parse(self, start="2026-09-19T10:00:00+04:00", end="2026-09-19T12:00:00+04:00"):
        return parse_window(QueryDict(urlencode({"from": start, "to": end})), DUBAI)

    def test_an_ordinary_window(self):
        start, end = self.parse()
        self.assertEqual(start, at(10))
        self.assertEqual(end, at(12))

    def test_a_missing_side_is_rejected(self):
        for query in ("", "from=2026-09-19T10:00:00%2B04:00"):
            with self.subTest(query):
                with self.assertRaises(ParamError) as ctx:
                    parse_window(QueryDict(query), DUBAI)
                self.assertEqual(ctx.exception.code, "invalid_window")

    def test_a_naive_time_is_rejected(self):
        """
        "2026-09-19T10:00:00" is a moment in an unnamed timezone. Reading it as
        the salon's own would answer a different question than the caller
        asked, and say so nowhere.
        """
        with self.assertRaises(ParamError) as ctx:
            self.parse(start="2026-09-19T10:00:00")
        self.assertEqual(ctx.exception.param, "from")
        self.assertEqual(ctx.exception.code, "invalid_window")

    def test_garbage_is_rejected(self):
        with self.assertRaises(ParamError) as ctx:
            self.parse(end="tomorrow lunchtime")
        self.assertEqual(ctx.exception.code, "invalid_window")

    def test_an_unencoded_plus_still_reads_as_an_offset(self):
        """
        A raw `+` in a query string decodes to a space, so a hand-built URL
        arrives as "2026-09-19T10:00:00 04:00". That is the caller's mistake,
        but it has exactly one sensible reading and a 422 nobody can decipher
        from the URL they typed is the worse answer.
        """
        params = QueryDict("from=2026-09-19T10:00:00 04:00&to=2026-09-19T12:00:00 04:00")
        self.assertEqual(parse_window(params, DUBAI), (at(10), at(12)))

    def test_the_end_must_come_after_the_start(self):
        with self.assertRaises(ParamError) as ctx:
            self.parse(start="2026-09-19T12:00:00+04:00", end="2026-09-19T10:00:00+04:00")
        self.assertEqual(ctx.exception.param, "to")
        self.assertEqual(ctx.exception.code, "invalid_window")

    def test_a_window_crossing_midnight_is_rejected(self):
        with self.assertRaises(ParamError) as ctx:
            self.parse(start="2026-09-19T20:00:00+04:00", end="2026-09-20T02:00:00+04:00")
        self.assertEqual(ctx.exception.code, "invalid_window")

    def test_a_window_ending_at_midnight_is_one_day(self):
        """
        `to` is exclusive, so 20:00 to 00:00 is one evening. Rejecting it
        would reject the most obvious "rest of today" the app can ask for.
        """
        start, end = self.parse(
            start="2026-09-19T20:00:00+04:00", end="2026-09-20T00:00:00+04:00"
        )
        self.assertEqual((start, end), (at(20), at(24)))

    def test_the_day_is_the_salons_day_not_the_callers(self):
        """
        A customer in London asking a Dubai salon about 22:00-23:00 their time
        is asking about tomorrow morning there — one salon day, not two.
        """
        start, end = self.parse(
            start="2026-09-19T22:00:00+01:00", end="2026-09-19T23:00:00+01:00"
        )
        self.assertEqual(start.astimezone(DUBAI), at(25))   # 01:00 next day
        self.assertEqual(end.astimezone(DUBAI), at(26))


class NearestAvailableParamTests(SimpleTestCase):
    """What the endpoint needs before it can search anything."""

    WINDOW = "from=2026-09-19T10:00:00%2B04:00&to=2026-09-19T12:00:00%2B04:00"
    SERVICE = "66666666-6666-6666-6666-666666666660"
    STYLIST = "99999999-9999-9999-9999-999999999990"

    def parse(self, extra=""):
        return parse_nearest_available(QueryDict(f"{self.WINDOW}&{extra}"), DUBAI)

    def test_services_alone(self):
        params = self.parse(f"service_ids={self.SERVICE}")
        self.assertEqual(params["service_ids"], [uuid.UUID(self.SERVICE)])
        self.assertIsNone(params["stylist_id"])

    def test_one_stylist_alone(self):
        params = self.parse(f"stylist_id={self.STYLIST}")
        self.assertEqual(params["stylist_id"], uuid.UUID(self.STYLIST))
        self.assertEqual(params["service_ids"], [])

    def test_both_together_narrow_rather_than_conflict(self):
        params = self.parse(f"stylist_id={self.STYLIST}&service_ids={self.SERVICE}")
        self.assertEqual(params["stylist_id"], uuid.UUID(self.STYLIST))
        self.assertEqual(params["service_ids"], [uuid.UUID(self.SERVICE)])

    def test_neither_is_the_one_thing_this_cannot_answer(self):
        with self.assertRaises(ParamError) as ctx:
            self.parse()
        self.assertEqual(ctx.exception.code, "missing_filter")


class BookingApiClientTests(SimpleTestCase):
    """
    The client, with the network replaced.

    The one rule worth testing here: a refusal is an answer. `urllib` raises
    on 4xx, and a version of this that let the exception through would turn
    "that slot just went" into a 500.
    """

    URL = "http://booking/v1/booking"

    def urlopen(self, *, status=201, body=b'{"id":"bkg_1"}'):
        """A stand-in for urlopen returning one response."""
        response = mock.MagicMock()
        response.status = status
        response.read.return_value = body
        response.__enter__.return_value = response
        return mock.patch.object(booking_api.urllib.request, "urlopen", return_value=response)

    def http_error(self, status, body):
        error = urllib.error.HTTPError(self.URL, status, "", {}, None)
        error.read = lambda: body
        return mock.patch.object(booking_api.urllib.request, "urlopen", side_effect=error)

    def create(self, **kwargs):
        return booking_api.create_booking(
            b'{"branch_id":"b1"}', authorization="Bearer t", **kwargs
        )

    def test_a_created_booking_comes_back_whole(self):
        with self.urlopen():
            self.assertEqual(self.create(), (201, {"id": "bkg_1"}))

    def test_a_refusal_is_returned_not_raised(self):
        """409 and 422 are screens in the app, not errors in this service."""
        for status_code, body in (
            (409, b'{"code":"slot_taken"}'),
            (422, b'{"code":"validation_error"}'),
        ):
            with self.subTest(status_code):
                with self.http_error(status_code, body):
                    self.assertEqual(self.create(), (status_code, json.loads(body)))

    def test_an_unreachable_service_raises(self):
        with mock.patch.object(
            booking_api.urllib.request, "urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with self.assertRaises(booking_api.BookingApiUnavailable):
                self.create()

    def test_a_timeout_raises(self):
        # TimeoutError is not a URLError; both are OSError, which is what the
        # client catches.
        with mock.patch.object(
            booking_api.urllib.request, "urlopen", side_effect=TimeoutError("timed out")
        ):
            with self.assertRaises(booking_api.BookingApiUnavailable):
                self.create()

    def test_an_html_error_page_is_not_passed_through(self):
        """
        A proxy's 502 page is not a booking. Forwarding it would put markup
        where the app expects JSON.
        """
        with self.http_error(502, b"<html>Bad Gateway</html>"):
            with self.assertRaises(booking_api.BookingApiUnavailable):
                self.create()

    def test_an_empty_body_is_none_not_a_crash(self):
        with self.urlopen(status=204, body=b""):
            self.assertEqual(self.create(), (204, None))

    @override_settings(BOOKING_API_URL="http://booking/")
    def test_only_the_headers_we_control_are_sent(self):
        with self.urlopen() as urlopen:
            self.create(idempotency_key="key-1", tenant_id="ten-1")
        request = urlopen.call_args.args[0]
        # The trailing slash on the setting must not double up in the path.
        self.assertEqual(request.get_full_url(), "http://booking/v1/mobile-booking")
        self.assertEqual(request.data, b'{"branch_id":"b1"}')
        self.assertEqual(
            {k.lower(): v for k, v in request.headers.items()},
            {
                "content-type": "application/json",
                "authorization": "Bearer t",
                "idempotency-key": "key-1",
                "x-tenant-id": "ten-1",
            },
        )

    def test_absent_optional_headers_are_absent(self):
        """
        An invented Idempotency-Key would make every retry a new booking, and
        an invented tenant would stamp someone else's rows.
        """
        with self.urlopen() as urlopen:
            self.create()
        headers = {k.lower() for k in urlopen.call_args.args[0].headers}
        self.assertNotIn("idempotency-key", headers)
        self.assertNotIn("x-tenant-id", headers)


class Customer:
    """The authenticated caller, without a database to put one in."""

    is_authenticated = True


class BookingCreateViewTests(SimpleTestCase):
    """
    The endpoint, with the client replaced.

    SimpleTestCase and a request factory: this view touches no model, and the
    only thing worth asserting is what crosses the boundary in each direction.
    """

    PAYLOAD = b'{"branch_id":"b1","services":[]}'

    def post(self, *, body=None, authenticated=True, content_type="application/json", **headers):
        request = APIRequestFactory().post(
            "/api/v1/booking",
            data=self.PAYLOAD if body is None else body,
            content_type=content_type,
            **headers,
        )
        if authenticated:
            force_authenticate(request, user=Customer())
        return BookingCreateView.as_view()(request)

    def call(self, result):
        return mock.patch("apps.salons.views.create_booking", return_value=result)

    def test_a_created_booking_passes_through(self):
        with self.call((201, {"id": "bkg_1", "code": "GS-BKG-1"})):
            response = self.post()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data, {"id": "bkg_1", "code": "GS-BKG-1"})

    def test_a_refusal_passes_through_with_its_own_shape(self):
        """
        409 and 422 reach the app as booking-api wrote them. This endpoint
        deliberately does NOT rewrap them in this project's envelope: the app
        branches on that service's codes, and a translation layer here would
        be one more thing to keep in step.
        """
        for status_code, body in (
            (409, {"code": "slot_taken", "message": "That time just went."}),
            (422, {"code": "validation_error", "errors": [{"field": "services"}]}),
        ):
            with self.subTest(status_code):
                with self.call((status_code, body)):
                    response = self.post()
                self.assertEqual(response.status_code, status_code)
                self.assertEqual(response.data, body)

    def test_the_body_crosses_untouched(self):
        with self.call((201, {})) as create:
            self.post()
        self.assertEqual(create.call_args.args[0], self.PAYLOAD)

    def test_the_callers_token_is_forwarded(self):
        with self.call((201, {})) as create:
            self.post(HTTP_AUTHORIZATION="Bearer customer-token")
        self.assertEqual(create.call_args.kwargs["authorization"], "Bearer customer-token")

    def test_idempotency_key_is_forwarded_when_sent(self):
        with self.call((201, {})) as create:
            self.post(HTTP_IDEMPOTENCY_KEY="key-1")
        self.assertEqual(create.call_args.kwargs["idempotency_key"], "key-1")

    def test_an_idempotency_key_is_always_sent_now(self):
        """
        CHANGED DELIBERATELY. This used to forward None when the app sent no
        header, which left a double tap on a flaky connection creating two
        bookings and two charges. The key is now derived from the request
        itself, so a retry is replayed whether or not the client thought
        about it. See BookingRoutingTests.
        """
        with self.call((201, {})) as create:
            self.post()
        self.assertTrue(create.call_args.kwargs["idempotency_key"])

    def test_no_tenant_is_sent_for_a_salon_that_resolves_to_nothing(self):
        # This payload's salon_id is not a real one, so nothing is derived
        # and nothing is guessed.
        with self.call((201, {})) as create:
            self.post()
        self.assertIsNone(create.call_args.kwargs["tenant_id"])

    def test_tenant_header_is_forwarded_when_sent(self):
        with self.call((201, {})) as create:
            self.post(HTTP_X_TENANT_ID="ten-1")
        self.assertEqual(create.call_args.kwargs["tenant_id"], "ten-1")

    def test_an_unreachable_booking_service_is_503_in_our_envelope(self):
        with mock.patch(
            "apps.salons.views.create_booking",
            side_effect=BookingApiUnavailable("connection refused"),
        ):
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "service_unavailable")
        self.assertEqual(response.data["errors"][0]["code"], "booking_api_unavailable")

    def test_a_form_post_is_refused_before_the_network(self):
        with self.call((201, {})) as create:
            response = self.post(body="branch_id=b1", content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 415)
        create.assert_not_called()

    def test_anonymous_is_refused_before_the_network(self):
        with self.call((201, {})) as create:
            response = self.post(authenticated=False)
        self.assertEqual(response.status_code, 401)
        create.assert_not_called()


class BookingRoutingTests(SimpleTestCase):
    """
    What the app has to send, and what this service works out for itself.

    THE PROBLEM THIS SOLVES. booking-api's payload calls the field `salon_id`
    and reads it straight into `branchId`, while every other endpoint in this
    service returns STOREFRONT uuids. An app that booked with the id it had
    just browsed with sent the wrong one — and the refusal named the stylist
    ("that stylist does not work at this salon"), because a wrong branch
    first shows up as an empty roster. Two uuids, one field, and an error
    pointing at neither.

    Now the app sends `salon_id` and nothing else: no `X-Tenant-Id`, no
    `Idempotency-Key`, no knowing that "salon" secretly means "branch".
    """

    TENANT = "11111111-1111-1111-1111-111111111111"
    OTHER_TENANT = "22222222-2222-2222-2222-222222222222"
    STOREFRONT = "55555555-5555-5555-5555-555555555555"
    BRANCH = "66666666-6666-6666-6666-666666666666"

    def send(self, body, *, resolves="default", **headers):
        request = APIRequestFactory().post(
            "/api/v1/booking",
            data=body,
            content_type="application/json",
            **headers,
        )
        force_authenticate(request, user=Customer())
        if resolves == "default":
            resolves = {"tenant_id": self.TENANT, "branch_id": self.BRANCH}
        with mock.patch(
            "apps.salons.views.booking_route",
            side_effect=lambda ref: resolves if isinstance(ref, str) and ref.strip() else None,
        ) as lookup, mock.patch(
            "apps.salons.views.create_booking", return_value=(201, {})
        ) as create:
            BookingCreateView.as_view()(request)
        self.lookup = lookup
        return create.call_args

    def sent_body(self, call):
        return json.loads(call.args[0])

    # ---------------------------------------------------------- the rewrite

    def test_a_storefront_id_is_rewritten_to_the_branch_booking_api_means(self):
        # THE WHOLE POINT. The app sends what it browsed with; booking-api
        # receives what it actually reads into branchId.
        call = self.send(('{"salon_id":"%s"}' % self.STOREFRONT).encode())
        self.assertEqual(self.sent_body(call)["salon_id"], self.BRANCH)

    def test_a_branch_id_is_left_exactly_as_it_arrived(self):
        """
        An app already sending the branch uuid resolves to that same branch,
        so there is nothing to rewrite — and the bytes are not re-encoded at
        all, which keeps the common case away from the JSON writer.
        """
        body = ('{"salon_id":"%s","total":120.00}' % self.BRANCH).encode()
        call = self.send(body)
        self.assertEqual(call.args[0], body)

    def test_every_figure_survives_the_rewrite(self):
        """
        MONEY IS THE RISK IN RE-ENCODING. Python parses a JSON number into
        the same double a JavaScript client does and writes back the shortest
        string that reads as that identical double, so nothing is rounded and
        a third decimal — which booking-api refuses — stays a third decimal.
        """
        call = self.send(
            ('{"salon_id":"%s","total":367.5,"tax":17.25,"qty":2,"bad":12.005}'
             % self.STOREFRONT).encode()
        )
        body = self.sent_body(call)
        self.assertEqual(body["total"], 367.5)
        self.assertEqual(body["tax"], 17.25)
        self.assertEqual(body["bad"], 12.005)
        # An integer does not sprout a .0 on the way through.
        self.assertIsInstance(body["qty"], int)

    def test_nothing_else_in_the_payload_is_touched(self):
        call = self.send(
            ('{"salon_id":"%s","services":[{"id":"svc","amount":350}],'
             '"status":"BOOKED"}' % self.STOREFRONT).encode()
        )
        body = self.sent_body(call)
        self.assertEqual(body["services"], [{"id": "svc", "amount": 350}])
        self.assertEqual(body["status"], "BOOKED")

    # ---------------------------------------------------------- the tenant

    def test_the_tenant_is_derived_from_the_same_lookup(self):
        call = self.send(('{"salon_id":"%s"}' % self.STOREFRONT).encode())
        self.assertEqual(call.kwargs["tenant_id"], self.TENANT)

    def test_the_callers_own_tenant_header_still_wins(self):
        # An app that knows its tenant is the better source and is not
        # second-guessed.
        call = self.send(
            ('{"salon_id":"%s"}' % self.STOREFRONT).encode(),
            HTTP_X_TENANT_ID=self.OTHER_TENANT,
        )
        self.assertEqual(call.kwargs["tenant_id"], self.OTHER_TENANT)
        # ...but the salon is still resolved, because the BRANCH is needed
        # whatever the tenant header said.
        self.assertEqual(self.sent_body(call)["salon_id"], self.BRANCH)

    # ----------------------------------------------------- nothing guessed

    def test_a_salon_that_resolves_to_nothing_changes_nothing(self):
        """
        Not a guess, and not a refusal either: the booking goes on exactly as
        it arrived and booking-api answers, which is where the refusal was
        before this existed.
        """
        body = b'{"salon_id":"marina-walk"}'
        call = self.send(body, resolves=None)
        self.assertEqual(call.args[0], body)
        self.assertIsNone(call.kwargs["tenant_id"])

    def test_a_payload_with_no_salon_is_forwarded_untouched(self):
        call = self.send(b'{"services":[]}')
        self.assertEqual(call.args[0], b'{"services":[]}')
        self.assertIsNone(call.kwargs["tenant_id"])
        self.lookup.assert_called_once_with(None)

    def test_an_unreadable_body_is_still_booking_apis_422_to_give(self):
        for body in (b"", b"not json", b"[]", b'"a string"'):
            with self.subTest(body):
                call = self.send(body)
                self.assertEqual(call.args[0], body)
                self.assertIsNone(call.kwargs["tenant_id"])

    # ------------------------------------------------------- idempotency

    def test_a_key_is_derived_when_the_app_sends_none(self):
        """
        booking-api replays a repeat under the same key rather than booking
        twice. Leaving that to the client meant a client that forgot got two
        bookings and two charges from one flaky tap, with no warning.
        """
        call = self.send(('{"salon_id":"%s"}' % self.STOREFRONT).encode())
        self.assertTrue(call.kwargs["idempotency_key"])

    def test_the_same_booking_retried_derives_the_same_key(self):
        # DERIVED, NOT RANDOM. A random uuid per request would satisfy the
        # header and protect nobody.
        body = ('{"salon_id":"%s","start":"17:00"}' % self.STOREFRONT).encode()
        first = self.send(body).kwargs["idempotency_key"]
        second = self.send(body).kwargs["idempotency_key"]
        self.assertEqual(first, second)

    def test_a_different_booking_derives_a_different_key(self):
        a = self.send(('{"salon_id":"%s","start":"17:00"}' % self.STOREFRONT).encode())
        b = self.send(('{"salon_id":"%s","start":"18:00"}' % self.STOREFRONT).encode())
        self.assertNotEqual(a.kwargs["idempotency_key"], b.kwargs["idempotency_key"])

    def test_the_callers_own_key_is_used_untouched(self):
        call = self.send(
            ('{"salon_id":"%s"}' % self.STOREFRONT).encode(),
            HTTP_IDEMPOTENCY_KEY="mine-007",
        )
        self.assertEqual(call.kwargs["idempotency_key"], "mine-007")


class TenantForSalonGuardTests(SimpleTestCase):
    """
    What `tenant_for_salon` refuses to look up at all.

    The resolving cases need a database and are covered through the view; what
    can be pinned without one is that nothing unusable ever reaches a query,
    because every one of these would otherwise be a tenant chosen at random.
    """

    def test_a_reference_that_is_not_a_usable_string_is_none(self):
        for ref in (None, "", "   ", 42, [], {"id": "x"}, True):
            with self.subTest(ref):
                self.assertIsNone(tenant_for_salon(ref))


class ServiceRow(types.SimpleNamespace):
    """A row as `services_by_ids` hands it over: columns plus two annotations."""

    @classmethod
    def make(cls, service_id, **overrides):
        return cls(**{
            "id": service_id,
            "tenant_id": ServiceDetailsViewTests.TENANT,
            "salon_id": ServiceDetailsViewTests.SALON,
            "name": "Signature Fade",
            "description": "Skin fade with a hot towel finish.",
            "price_minor": 12000,
            "duration_minutes": 45,
            "category_0_id": ServiceDetailsViewTests.CATEGORY,
            "image_url": "https://cdn.gostyles.app/services/fade.jpg",
            "status": "PUBLISHED",
            "deleted_at": None,
            "online_booking_enabled": True,
            **overrides,
        })


class ServiceDetailsViewTests(SimpleTestCase):
    """
    GET /api/v1/services-details, with the database replaced.

    The selector is mocked because this view's whole job is the arithmetic
    around it: which ids it asks for, what order the answers come back in, and
    what it does about the ones that are missing.
    """

    A = uuid.UUID("66666666-6666-6666-6666-666666666660")
    B = uuid.UUID("66666666-6666-6666-6666-666666666661")
    GHOST = uuid.UUID("66666666-6666-6666-6666-66666666666f")
    TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
    SALON = uuid.UUID("55555555-5555-5555-5555-555555555555")
    CATEGORY = uuid.UUID("77777777-7777-7777-7777-777777777770")
    PARENT = uuid.UUID("77777777-7777-7777-7777-777777777771")

    CATEGORIES = {
        CATEGORY: {
            "id": CATEGORY,
            "name_en": "Fades",
            "parent_id": PARENT,
        },
        PARENT: {
            "id": PARENT,
            "name_en": "Haircut & Styling",
            "parent_id": None,
        },
    }

    def get(self, query, *, rows=(), authenticated=True, categories=None):
        request = APIRequestFactory().get(f"/api/v1/services-details?{query}")
        if authenticated:
            force_authenticate(request, user=Customer())

        with mock.patch(
            "apps.salons.views.services_by_ids", return_value=list(rows)
        ) as selector, mock.patch(
            "apps.salons.views.categories_for_tenants",
            return_value=self.CATEGORIES if categories is None else categories,
        ):
            self.selector = selector
            return ServiceDetailsView.as_view()(request)

    def test_the_requested_order_is_what_comes_back(self):
        """
        Not the database's order. The app renders the basket in the order the
        customer built it, so the array is rebuilt against the query string.
        """
        response = self.get(
            f"service_ids={self.B},{self.A}",
            # Deliberately the other way round, as a plain IN query would
            # return them.
            rows=[ServiceRow.make(self.A), ServiceRow.make(self.B)],
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["id"] for row in response.data], [str(self.B), str(self.A)]
        )

    def test_an_unresolvable_id_is_left_out_rather_than_erroring(self):
        """Two ids asked, one returned. The caller compares lengths."""
        response = self.get(
            f"service_ids={self.A},{self.GHOST}",
            rows=[ServiceRow.make(self.A)],
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [str(self.A)])

    def test_nothing_resolving_is_an_empty_array_not_a_404(self):
        response = self.get(f"service_ids={self.GHOST}", rows=[])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def test_duplicates_collapse_before_the_query(self):
        self.get(f"service_ids={self.A},{self.A}", rows=[ServiceRow.make(self.A)])
        self.assertEqual(self.selector.call_args.args[0], [self.A])

    def test_a_row_carries_the_documented_shape(self):
        response = self.get(f"service_ids={self.A}", rows=[ServiceRow.make(self.A)])
        self.assertEqual(response.data[0], {
            "id": str(self.A),
            "salon_id": str(self.SALON),
            "name": "Signature Fade",
            "description": "Skin fade with a hot towel finish.",
            "price": Decimal("120.00"),
            "duration_min": 45,
            "duration_max": 45,
            # The PARENT category, which is the chip the services tab drew.
            "category": {"id": str(self.PARENT), "label": "Haircut & Styling"},
            "image_url": "https://cdn.gostyles.app/services/fade.jpg",
            "is_active": True,
        })

    def test_a_retired_service_still_resolves(self):
        """
        An old booking has to be describable. Each of the three reasons a
        service leaves the menu comes back as is_active: false, not as a gap.
        """
        for reason in (
            {"status": "DRAFT"},
            {"deleted_at": datetime(2026, 1, 1, tzinfo=dt_timezone.utc)},
            {"online_booking_enabled": False},
        ):
            with self.subTest(reason):
                response = self.get(
                    f"service_ids={self.A}",
                    rows=[ServiceRow.make(self.A, **reason)],
                )
                self.assertEqual(len(response.data), 1)
                self.assertIs(response.data[0]["is_active"], False)

    def test_a_service_with_no_category_is_null_not_other(self):
        response = self.get(
            f"service_ids={self.A}",
            rows=[ServiceRow.make(self.A, category_0_id=None)],
        )
        self.assertIsNone(response.data[0]["category"])

    def test_a_top_level_category_is_its_own_chip(self):
        response = self.get(
            f"service_ids={self.A}",
            rows=[ServiceRow.make(self.A, category_0_id=self.PARENT)],
        )
        self.assertEqual(
            response.data[0]["category"],
            {"id": str(self.PARENT), "label": "Haircut & Styling"},
        )

    def test_missing_or_empty_service_ids_is_422_missing_filter(self):
        """
        Required here, unlike on the stylists endpoint where absent means the
        whole roster. A lookup with nothing to look up is a caller bug.
        """
        for query in ("", "service_ids=", "service_ids=,,"):
            with self.subTest(query):
                response = self.get(query)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.data["code"], "validation_error")
                self.assertEqual(response.data["errors"][0], {
                    "field": "service_ids",
                    "code": "missing_filter",
                    "message": "Provide at least one service id.",
                })

    def test_a_malformed_id_is_422(self):
        response = self.get("service_ids=not-a-uuid")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["errors"][0]["field"], "service_ids")

    def test_anonymous_is_refused(self):
        response = self.get(f"service_ids={self.A}", authenticated=False)
        self.assertEqual(response.status_code, 401)
        self.selector.assert_not_called()


class BookingListClientTests(SimpleTestCase):
    """The client call for the list, with the network replaced."""

    def urlopen(self, body=b'{"count":0,"results":[]}'):
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = body
        response.__enter__.return_value = response
        return mock.patch.object(
            booking_api.urllib.request, "urlopen", return_value=response
        )

    @override_settings(BOOKING_API_URL="http://booking/")
    def test_the_query_rides_along_and_the_token_goes_with_it(self):
        with self.urlopen() as urlopen:
            booking_api.list_bookings(
                "filter=archive&page=2", authorization="Bearer t"
            )
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.get_full_url(),
            "http://booking/v1/mobile-booking?filter=archive&page=2",
        )
        self.assertEqual(request.get_method(), "GET")
        # WHOSE BOOKINGS IS NOT ON THE URL. booking-api reads the customer
        # off this header, and a customer id in the query would be an
        # enumeration of every booking behind one valid login.
        self.assertEqual(request.get_header("Authorization"), "Bearer t")

    @override_settings(BOOKING_API_URL="http://booking/")
    def test_no_query_leaves_no_dangling_question_mark(self):
        with self.urlopen() as urlopen:
            booking_api.list_bookings("", authorization="Bearer t")
        self.assertEqual(
            urlopen.call_args.args[0].get_full_url(),
            "http://booking/v1/mobile-booking",
        )


class BookingListViewTests(SimpleTestCase):
    """
    The endpoint, with both the client and the salon lookup replaced.

    What is worth asserting here is only what this service ADDS: booking-api
    owns the shelves, the ordering and the counts, and none of that is
    re-decided here.
    """

    NOW = datetime(2026, 9, 18, 12, 0, tzinfo=dt_timezone.utc)

    def row(self, **overrides):
        return {
            "id": "bkg-1",
            "salon_id": "marina-walk",
            "status": "CONFIRMED_BY_SALON",
            "payment_status": "FULLY_PAID",
            "booking_type": "SINGLE",
            "date": "2026-09-20",
            # Two days out, written in the salon's own offset.
            "start_time": "2026-09-20T20:00:00+04:00",
            "end_time": "2026-09-20T20:45:00+04:00",
            "services": [{"id": "svc_fade", "name": "Signature Fade"}],
            "stylists": [],
            "total": 216.25,
            "due_amount": 0,
            "created_at": "2026-09-18T14:02:11+04:00",
            **overrides,
        }

    def page(self, rows=None, *, count=None, counts=None):
        rows = [self.row()] if rows is None else rows
        return {
            "count": len(rows) if count is None else count,
            "page": 1,
            "page_size": 20,
            "counts": counts or {"upcoming": 1, "recurring": 0, "archive": 0},
            "results": rows,
        }

    CARD = {
        "id": "3f6a1d2c-88b4-4f0e-9a3d-51c7e2b40f91",
        "name": "The Iron Razor Barbershop",
        "logo_url": "https://cdn/logo.png",
        "city": "Dubai",
        "cancel_window_hours": 24,
        "timezone": "Asia/Dubai",
    }

    def get(self, query="", *, upstream=None, cards=None, authenticated=True):
        request = APIRequestFactory().get(f"/api/v1/bookings?{query}")
        if authenticated:
            force_authenticate(request, user=Customer())
        with mock.patch(
            "apps.salons.views.list_bookings",
            return_value=(200, self.page()) if upstream is None else upstream,
        ) as client, mock.patch(
            "apps.salons.views.salon_cards_for_refs",
            return_value={"marina-walk": self.CARD} if cards is None else cards,
        ), mock.patch(
            "apps.salons.views.datetime"
        ) as clock:
            clock.now.return_value = self.NOW
            clock.fromisoformat = datetime.fromisoformat
            self.client_mock = client
            return BookingListView.as_view()(request)

    # ------------------------------------------------------------ forwarding

    def test_the_page_is_asked_for_with_the_caller_s_parameters(self):
        self.get("filter=archive&pageSize=5&page=3")
        self.assertEqual(
            dict(parse_qsl(self.client_mock.call_args.args[0])),
            {"filter": "archive", "page": "3", "pageSize": "5"},
        )

    def test_a_filter_that_was_not_sent_is_not_invented(self):
        """
        booking-api owns the default. Sending `upcoming` ourselves would
        give this service a second copy of it to keep in step.
        """
        self.get("")
        self.assertNotIn("filter", dict(parse_qsl(self.client_mock.call_args.args[0])))

    def test_page_size_is_capped_before_the_round_trip(self):
        # Each row upstream costs a quote, so an over-large page is trimmed
        # here as well as there. Neither service trusts the other to do it.
        self.get("pageSize=5000")
        self.assertEqual(
            dict(parse_qsl(self.client_mock.call_args.args[0]))["pageSize"], "50"
        )

    def test_a_nonsense_page_falls_back_rather_than_refusing(self):
        """
        The list has ONE refusal and it is `filter`, because that changes
        which bookings come back. `?page=abc` only changes how many, and a
        422 there costs the customer their history over a typo.
        """
        for query in ("page=abc", "page=0", "page=-4", "pageSize=nope"):
            with self.subTest(query):
                response = self.get(query)
                self.assertEqual(response.status_code, 200)

    # ------------------------------------------------------------ enrichment

    def test_the_salon_is_filled_in_from_this_service_s_own_tables(self):
        # booking-api stores a branch id and cannot name a salon (its
        # booking-list.md §9). This is the half only this service can answer.
        row = self.get().data["results"][0]
        self.assertEqual(row["salon"], {
            "id": self.CARD["id"],
            "name": "The Iron Razor Barbershop",
            "logo_url": "https://cdn/logo.png",
            "city": "Dubai",
        })
        # The window is policy, not something the app should see or apply.
        self.assertNotIn("cancel_window_hours", row["salon"])

    def test_a_salon_that_cannot_be_resolved_is_null_not_a_hollow_object(self):
        """
        `null` says "we could not find it". A card with a name-shaped hole
        in it says the salon has no name.
        """
        row = self.get(cards={}).data["results"][0]
        self.assertIsNone(row["salon"])
        # The booking is still real and still listed.
        self.assertEqual(row["id"], "bkg-1")

    def test_a_booking_inside_the_cancellation_window_may_not_be_cancelled(self):
        # Starts 2026-09-20T20:00+04:00 = 16:00Z. A 24h window closes at
        # 2026-09-19T16:00Z, and "now" is a day before that.
        self.assertTrue(self.get().data["results"][0]["can_cancel"])

        late = {**self.CARD, "cancel_window_hours": 24 * 30}
        row = self.get(cards={"marina-walk": late}).data["results"][0]
        self.assertFalse(row["can_cancel"])
        self.assertFalse(row["can_reschedule"])

    def test_a_salon_with_no_published_window_may_still_be_cancelled(self):
        """
        `False` would be the cautious-LOOKING default and is the wrong one:
        it tells customers of every salon that has not filled the field in
        that they may never cancel — a refusal the salon never made.
        """
        bare = {**self.CARD, "cancel_window_hours": None}
        self.assertTrue(
            self.get(cards={"marina-walk": bare}).data["results"][0]["can_cancel"]
        )

    def test_a_finished_booking_offers_no_cancel_button(self):
        # Whatever the window says. Cancelling a cancelled booking is not a
        # thing, and offering the button is a request the salon will refuse.
        for status_word in ("COMPLETED", "CANCELLED", "NO_SHOW"):
            with self.subTest(status_word):
                response = self.get(
                    upstream=(200, self.page([self.row(status=status_word)]))
                )
                self.assertFalse(response.data["results"][0]["can_cancel"])

    def test_a_row_with_no_usable_start_offers_no_cancel_button(self):
        # Saying yes here would show a button the cancel endpoint refuses.
        for start in (None, "not-a-time", "2026-09-20T20:00:00"):
            with self.subTest(start):
                response = self.get(
                    upstream=(200, self.page([self.row(start_time=start)]))
                )
                self.assertFalse(response.data["results"][0]["can_cancel"])

    # ------------------------------------------------------------ pagination

    def test_next_carries_every_parameter_the_caller_sent(self):
        # Rebuilt from the caller's own url, so `filter` and `pageSize`
        # survive into the link rather than being dropped from it.
        response = self.get(
            "filter=archive&pageSize=1",
            upstream=(200, self.page(count=9)),
        )
        self.assertIn("filter=archive", response.data["next"])
        self.assertIn("pageSize=1", response.data["next"])
        self.assertIn("page=2", response.data["next"])
        self.assertIsNone(response.data["previous"])

    def test_the_last_page_has_no_next(self):
        response = self.get("page=2&pageSize=20", upstream=(200, self.page(count=21)))
        self.assertIsNone(response.data["next"])
        self.assertIn("page=1", response.data["previous"])

    def test_the_three_counts_come_back_untouched(self):
        counts = {"upcoming": 3, "recurring": 0, "archive": 10}
        response = self.get(upstream=(200, self.page(counts=counts)))
        self.assertEqual(response.data["counts"], counts)

    def test_every_shelf_answers_with_all_three_badges(self):
        """
        FOUND BY CALLING THE ENDPOINT, not by a mocked body. booking-api's
        repository knows two shelves and the app draws three chips, so the
        `upcoming` and `archive` responses came back with no `recurring`
        key at all and the third badge rendered empty rather than zero.

        This pins the SHAPE the app reads by key. The numbers are
        booking-api's to decide; the three keys existing is not.
        """
        for counts in (
            {"upcoming": 3, "recurring": 0, "archive": 10},
            {"upcoming": 0, "recurring": 0, "archive": 0},
        ):
            with self.subTest(counts):
                response = self.get(upstream=(200, self.page(counts=counts)))
                self.assertEqual(
                    set(response.data["counts"]),
                    {"upcoming", "recurring", "archive"},
                )

    def test_an_empty_shelf_is_a_200_not_a_404(self):
        response = self.get(upstream=(200, self.page([], count=0)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [])
        self.assertIsNone(response.data["next"])

    # ------------------------------------------------------------ refusals

    def test_a_refusal_passes_through_unenriched(self):
        """
        422 invalid_filter reaches the app in booking-api's own shape. There
        is nothing to enrich on a body that carries no bookings.
        """
        body = {"code": "validation_error", "errors": [{"code": "invalid_filter"}]}
        response = self.get("filter=past", upstream=(422, body))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data, body)

    def test_an_unreachable_booking_service_is_503_in_our_envelope(self):
        request = APIRequestFactory().get("/api/v1/bookings")
        force_authenticate(request, user=Customer())
        with mock.patch(
            "apps.salons.views.list_bookings", side_effect=BookingApiUnavailable("down")
        ):
            response = BookingListView.as_view()(request)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["errors"][0]["code"], "booking_api_unavailable")

    def test_anonymous_never_reaches_the_network(self):
        request = APIRequestFactory().get("/api/v1/bookings")
        with mock.patch("apps.salons.views.list_bookings") as client:
            response = BookingListView.as_view()(request)
        self.assertEqual(response.status_code, 401)
        client.assert_not_called()


class BookingPatchMediaTypeTests(SimpleTestCase):
    """The 415 guard, which PATCH was missing while create had it."""

    def patch(self, content_type):
        request = APIRequestFactory().patch(
            "/api/v1/booking/x", data=b"{}", content_type=content_type
        )
        force_authenticate(request, user=Customer())
        with mock.patch("apps.salons.views.patch_booking") as client:
            self.client_mock = client
            return BookingDetailView.as_view()(request, booking_id="x")

    def test_a_form_post_is_refused_before_dialling_out(self):
        # booking-api would answer 422 after a round trip, with a message
        # about the payload rather than about the header.
        response = self.patch("application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 415)
        self.client_mock.assert_not_called()

    def test_json_goes_through(self):
        self.client_mock = None
        request = APIRequestFactory().patch(
            "/api/v1/booking/x", data=b"{}", content_type="application/json"
        )
        force_authenticate(request, user=Customer())
        with mock.patch(
            "apps.salons.views.patch_booking", return_value=(200, {"id": "x"})
        ) as client:
            response = BookingDetailView.as_view()(request, booking_id="x")
        self.assertEqual(response.status_code, 200)
        client.assert_called_once()


class BookingDetailHeaderlessTests(SimpleTestCase):
    """
    Reading and paying for a booking take the id and the token, nothing else.

    WHAT WENT WRONG. `PATCH /booking/<id>` forwarded `X-Tenant-Id` only when
    the app sent one, and the app has no way to know a tenant from a booking
    id. With no tenant, booking-api could not load the salon's catalogue and
    answered `404 BOOKING_NOT_FOUND — Unknown service: <id>` to a payment for
    a booking that plainly existed. The customer could not pay, and the
    message blamed a service.

    A booking already records which salon and tenant it belongs to, so asking
    the caller to repeat it was asking for a fact they did not hold.
    """

    BOOKING = "c4832b75-0ed1-4c70-a005-14a200f6638c"

    def test_reading_sends_no_tenant_header(self):
        request = APIRequestFactory().get(f"/api/v1/booking/{self.BOOKING}")
        force_authenticate(request, user=Customer())
        with mock.patch(
            "apps.salons.views.read_booking", return_value=(200, {"id": self.BOOKING})
        ) as read:
            response = BookingDetailView.as_view()(request, booking_id=self.BOOKING)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("tenant_id", read.call_args.kwargs)

    def test_paying_sends_no_tenant_header(self):
        request = APIRequestFactory().patch(
            f"/api/v1/booking/{self.BOOKING}",
            data=b'{"payment_status":"FULLY_PAID"}',
            content_type="application/json",
        )
        force_authenticate(request, user=Customer())
        with mock.patch(
            "apps.salons.views.patch_booking", return_value=(200, {"id": self.BOOKING})
        ) as patch_call:
            response = BookingDetailView.as_view()(request, booking_id=self.BOOKING)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("tenant_id", patch_call.call_args.kwargs)

    def send_patch(self, booking_id, body, **headers):
        request = APIRequestFactory().patch(
            f"/api/v1/booking/{booking_id}",
            data=body,
            content_type="application/json",
            **headers,
        )
        force_authenticate(request, user=Customer())
        with mock.patch(
            "apps.salons.views.patch_booking", return_value=(200, {})
        ) as call:
            BookingDetailView.as_view()(request, booking_id=booking_id)
        return call.call_args.kwargs["idempotency_key"]

    def test_no_key_is_invented_for_a_payment(self):
        """
        THE BUG THIS PINS. A derived key hashed the customer and the BODY --
        which does not carry the booking id, because that travels in the URL.
        So paying for two different bookings with the same figures produced
        one key, and booking-api refused the second with
        IDEMPOTENCY_KEY_REUSED. Correct of it; the question should never have
        been asked.

        What stops a double payment is the booking's own state: §11.1 patches
        only from DRAFT, so a second attempt is 409 already_paid whatever key
        it carries.
        """
        body = b'{"payment_status":"FULLY_PAID","advance_paid_amount":367.5}'
        self.assertIsNone(self.send_patch(self.BOOKING, body))

    def test_two_bookings_with_identical_payloads_do_not_collide(self):
        # The exact shape that produced the 409: same customer, same money,
        # different booking.
        body = b'{"payment_status":"FULLY_PAID","advance_paid_amount":367.5}'
        first = self.send_patch("848eb05e-e428-4e6c-bd08-7111a34bb5be", body)
        second = self.send_patch("c4832b75-0ed1-4c70-a005-14a200f6638c", body)
        self.assertIsNone(first)
        self.assertIsNone(second)

    def test_the_callers_own_key_is_still_forwarded(self):
        # An app doing its own retry accounting is the better source, and
        # nothing here second-guesses it.
        body = b'{"payment_status":"FULLY_PAID"}'
        self.assertEqual(
            self.send_patch(self.BOOKING, body, HTTP_IDEMPOTENCY_KEY="mine-9"),
            "mine-9",
        )


class BookingDetailSalonTests(SimpleTestCase):
    """
    The drawer's `salon` object.

    booking-api stores a branch id and cannot name a salon (its
    booking-list.md §9), so this service fills it in from the platform
    tables. The DRAWER carries more than a list row: a booking someone is
    about to travel to needs the street and a map pin, not just a name.
    """

    BOOKING = "c4832b75-0ed1-4c70-a005-14a200f6638c"
    SALON = "263e7e84-b93d-4cb3-bc38-20bb0e6c58a6"
    CARD = {
        "id": "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5",
        "name": "Green Wave Salon",
        "logo_url": "https://cdn/logo.png",
        "city": "Dhaka",
        "cancel_window_hours": 24,
        "timezone": "Asia/Dhaka",
        "slug": "green-wave",
        "cover_url": "https://cdn/cover.png",
        "address": "12 Gulshan Ave",
        "region": "Dhaka",
        "country_code": "BD",
        "lat": 23.79,
        "lng": 90.41,
    }

    def get(self, *, cards=None, upstream=None):
        request = APIRequestFactory().get(f"/api/v1/booking/{self.BOOKING}")
        force_authenticate(request, user=Customer())
        body = {"id": self.BOOKING, "salon_id": self.SALON, "total": 367.5}
        with mock.patch(
            "apps.salons.views.read_booking",
            return_value=(200, body) if upstream is None else upstream,
        ), mock.patch(
            "apps.salons.views.salon_cards_for_refs",
            return_value={self.SALON: self.CARD} if cards is None else cards,
        ):
            return BookingDetailView.as_view()(request, booking_id=self.BOOKING)

    def test_the_drawer_carries_the_full_salon(self):
        salon = self.get().data["salon"]
        self.assertEqual(salon["name"], "Green Wave Salon")
        self.assertEqual(salon["address"], "12 Gulshan Ave")
        self.assertEqual(salon["latitude"], 23.79)
        self.assertEqual(salon["longitude"], 90.41)
        self.assertEqual(salon["timezone"], "Asia/Dhaka")

    def test_the_policy_window_is_not_published_to_the_app(self):
        # cancel_window_hours is how can_cancel is DECIDED here. The app is
        # told the answer, not the rule, so it cannot apply its own version
        # of a policy the salon owns.
        self.assertNotIn("cancel_window_hours", self.get().data["salon"])

    def test_a_salon_that_cannot_be_resolved_is_null(self):
        # "We could not find it", rather than an object with a name-shaped
        # hole in it. The booking is still returned.
        response = self.get(cards={})
        self.assertIsNone(response.data["salon"])
        self.assertEqual(response.data["id"], self.BOOKING)

    def test_a_refusal_is_passed_through_untouched(self):
        body = {"detail": "No such booking.", "code": "validation_error"}
        response = self.get(upstream=(404, body))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data, body)
        self.assertNotIn("salon", response.data)

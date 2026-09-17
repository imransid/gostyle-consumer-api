import math
import uuid
import zoneinfo
from decimal import Decimal
from datetime import datetime, timedelta
from urllib.parse import urlencode
from django.http import QueryDict
from django.test import SimpleTestCase

from apps.salons import skills, slots, timezones, translate
from apps.salons.geo import bounding_box, format_distance, radius_box
from apps.salons.hours import is_within, next_opening, next_opening_at, resolve
from apps.salons.money import bps_to_percent, major
from apps.salons.snapshot import field, items, normalize

from apps.salons.params import (
    MAX_SERVICE_IDS,
    ParamError,
    parse_discovery,
    parse_map,
    parse_nearest_available,
    parse_service_ids,
    parse_window,
)
from apps.salons.views import _stylist_order


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

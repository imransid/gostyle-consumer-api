"""
Tests for the pure modules behind the salon profile endpoints.

NO DATABASE in the SimpleTestCase classes below. They run against snapshot.py,
hours.py, money.py and translate.py directly, which is possible because those
four modules import nothing from Django. That is the payoff of keeping them
pure: no fixtures, no transactions, no test database, and a full run in
milliseconds. There is no longer an exception to that rule — see
BoundingBoxTests at the bottom for why the map tests stopped needing one.

WHAT IS WORTH TESTING HERE. Not the happy path, which the seeded salon already
proves end to end. These cover the cases the seed CANNOT reach: a snapshot
published before a section existed, a weekly grid with today's row missing, a
manual state overruling real opening hours, a social payload where every
network is switched off. Those are exactly the shapes that arrive from four
years of production data and never from a fixture written this morning.
"""

import math
import zoneinfo
from decimal import Decimal

from django.test import SimpleTestCase

from apps.salons import timezones, translate
from apps.salons.geo import bounding_box, format_distance, radius_box
from apps.salons.hours import is_within, resolve
from apps.salons.money import bps_to_percent, major
from apps.salons.snapshot import field, items, normalize

from apps.salons.params import ParamError, parse_discovery, parse_map


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

    def test_both_coordinate_spellings_work(self):
        """The endpoint shipped with lat/lng; the app asks for the long ones."""
        self.assertEqual(parse_discovery({"lat": "25.19", "lng": "55.26"})["latitude"], 25.19)
        self.assertEqual(parse_discovery({"latitude": "25.19", "longitude": "55.26"})["longitude"], 55.26)

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

    def test_deprecated_boolean_spelling_still_works(self):
        self.assertTrue(parse_discovery({"open_now": "1"})["is_open_now"])
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
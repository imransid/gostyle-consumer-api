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

from decimal import Decimal

from django.test import SimpleTestCase

from apps.salons import translate
from apps.salons.geo import bounding_box
from apps.salons.hours import is_within, resolve
from apps.salons.money import bps_to_percent, major
from apps.salons.snapshot import field, items, normalize


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
        self.assertEqual(result["closes_at"], "Closes at 10:00 PM")

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

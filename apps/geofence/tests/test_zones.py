"""The distance, the units and the ring rules (apps/geofence/zones.py), no database."""

import math

from django.test import SimpleTestCase

from apps.geofence import zones
from apps.salons.geo import EARTH_RADIUS_KM

DUBAI = (25.2048, 55.2708)
# One degree along a meridian, in metres, with the mean radius used here.
METRES_PER_DEGREE = 2 * math.pi * zones.EARTH_RADIUS_M / 360


def north_of(point, metres):
    return (point[0] + metres / METRES_PER_DEGREE, point[1])


def east_of(point, metres):
    # Along the parallel; haversine measures the great circle, which at a few
    # hundred metres is the same to well under a centimetre.
    return (point[0], point[1] + metres / (METRES_PER_DEGREE * math.cos(math.radians(point[0]))))


class DistanceTests(SimpleTestCase):
    def test_the_same_earth_as_the_map(self):
        self.assertEqual(zones.EARTH_RADIUS_M, EARTH_RADIUS_KM * 1000)

    def test_one_degree_of_latitude(self):
        self.assertAlmostEqual(zones.distance_m(0, 0, 1, 0), 111_194.93, places=1)

    def test_one_degree_of_longitude_on_the_equator(self):
        self.assertAlmostEqual(zones.distance_m(0, 0, 0, 1), 111_194.93, places=1)

    def test_a_known_pair_of_cities(self):
        # London (51.5074, -0.1278) to Paris (48.8566, 2.3522): the haversine
        # distance on a 6371 km sphere is 343.56 km.
        self.assertAlmostEqual(zones.distance_m(51.5074, -0.1278, 48.8566, 2.3522) / 1000, 343.56, places=1)

    def test_the_salon_sheet_distances_in_dubai(self):
        for metres in (50, 200, 800, 15_000):
            with self.subTest(metres=metres):
                self.assertAlmostEqual(zones.distance_m(*DUBAI, *north_of(DUBAI, metres)), metres, places=3)
                self.assertAlmostEqual(zones.distance_m(*DUBAI, *east_of(DUBAI, metres)), metres, delta=0.01)

    def test_latitude_comes_first(self):
        # 800 m east of the salon. Read as (lng, lat) by mistake, it would be
        # thousands of kilometres away.
        east = east_of(DUBAI, 800)
        self.assertAlmostEqual(zones.distance_m(DUBAI[0], DUBAI[1], east[0], east[1]), 800, delta=0.01)
        self.assertGreater(zones.distance_m(DUBAI[1], DUBAI[0], east[0], east[1]), 1_000_000)

    def test_zero_and_symmetric(self):
        far = north_of(DUBAI, 1234)
        self.assertEqual(zones.distance_m(*DUBAI, *DUBAI), 0)
        self.assertAlmostEqual(zones.distance_m(*DUBAI, *far), zones.distance_m(*far, *DUBAI), places=6)

    def test_opposite_sides_of_the_earth(self):
        self.assertAlmostEqual(zones.distance_m(0, 0, 0, 180), math.pi * zones.EARTH_RADIUS_M, places=0)


class ParseServiceAreaTests(SimpleTestCase):
    def rings(self, payload):
        return zones.parse_service_area(payload).rings_m

    def test_the_bug_report_sheet(self):
        # Primary 800 m, Auto-Trigger 200 m, Arrival 50 m, Home-visit 15 km.
        area = zones.parse_service_area(
            {"primaryRadius": 800, "autoTrigger": 200, "arrivalRadius": 50, "homeRadiusKm": 15}
        )
        self.assertEqual(area.rings_m, (("arrival", 50.0), ("auto_trigger", 200.0), ("primary", 800.0)))
        self.assertEqual(area.home_radius_m, 15_000)
        self.assertEqual(area.problems, ())

    def test_what_the_admin_form_really_saves(self):
        # hq-master-admin-frontend-next wizard-step-payload.ts: the whole
        # `geo` state plus primaryRadiusM, with the form's defaults.
        payload = {
            "primaryRadius": 800,
            "autoTrigger": 200,
            "arrivalRadius": 50,
            "peakMultiplier": 1.5,
            "etaModel": "traffic",
            "offZoneBehavior": "warn",
            "homeVisits": "Yes",
            "homeRadiusKm": 15,
            "homeSurcharge": 50,
            "minBookingValue": 200,
            "travelBuffer": 30,
            "paymentAtHome": "card",
            "areasServed": ["Marina", "JBR"],
            "primaryRadiusM": 800,
        }
        area = zones.parse_service_area(payload)
        self.assertEqual(area.rings_m, (("arrival", 50.0), ("auto_trigger", 200.0), ("primary", 800.0)))
        self.assertEqual(area.home_radius_m, 15_000)
        self.assertEqual(area.problems, ())

    def test_an_emptied_box_is_saved_as_zero_and_means_off(self):
        area = zones.parse_service_area({"primaryRadius": 800, "primaryRadiusM": 800, "autoTrigger": 0, "arrivalRadius": 0})
        self.assertEqual(area.rings_m, (("primary", 800.0),))
        self.assertEqual(area.problems, ())

    def test_the_v2_metre_keys(self):
        area = zones.parse_service_area({"primaryRadiusM": 800, "autoTriggerM": 200, "arrivalRadiusM": 50})
        self.assertEqual(area.radius_m("primary"), 800)
        self.assertEqual(area.radius_m("auto_trigger"), 200)
        self.assertEqual(area.radius_m("arrival"), 50)

    def test_the_metre_key_wins_over_the_bare_one(self):
        self.assertEqual(self.rings({"primaryRadiusM": 900, "primaryRadius": 800}), (("primary", 900.0),))

    def test_numbers_sent_as_text(self):
        self.assertEqual(self.rings({"primaryRadiusM": " 800 ", "arrivalRadiusM": "50"}), (("arrival", 50.0), ("primary", 800.0)))

    def test_home_visit_radius_is_kilometres_and_never_a_ring(self):
        area = zones.parse_service_area({"homeRadiusKm": "2.5"})
        self.assertEqual(area.home_radius_m, 2500)
        self.assertEqual(area.rings_m, ())

    def test_kilometres_in_a_metres_box_is_left_out_and_said(self):
        area = zones.parse_service_area({"primaryRadiusM": 0.8, "arrivalRadiusM": 50})
        self.assertEqual(area.rings_m, (("arrival", 50.0),))
        self.assertIn("primaryRadiusM=0.8 m is outside 10..50000 m", area.problems)

    def test_too_wide_is_left_out(self):
        area = zones.parse_service_area({"primaryRadiusM": 500_000})
        self.assertEqual(area.rings_m, ())
        self.assertEqual(len(area.problems), 1)

    def test_the_limits_themselves_are_allowed(self):
        self.assertEqual(self.rings({"arrivalRadiusM": 10, "primaryRadiusM": 50_000}), (("arrival", 10.0), ("primary", 50_000.0)))

    def test_rings_out_of_order_lose_the_inner_one(self):
        area = zones.parse_service_area({"primaryRadiusM": 800, "autoTriggerM": 900, "arrivalRadiusM": 50})
        self.assertEqual(area.rings_m, (("arrival", 50.0), ("primary", 800.0)))
        self.assertIn("auto_trigger ring 900 m is not inside the primary ring 800 m", area.problems)

    def test_equal_rings_are_not_nested(self):
        area = zones.parse_service_area({"primaryRadiusM": 800, "autoTriggerM": 200, "arrivalRadiusM": 200})
        self.assertEqual(area.rings_m, (("auto_trigger", 200.0), ("primary", 800.0)))
        self.assertEqual(len(area.problems), 1)

    def test_a_missing_middle_ring_still_nests_the_others(self):
        self.assertEqual(self.rings({"primaryRadiusM": 800, "arrivalRadiusM": 50}), (("arrival", 50.0), ("primary", 800.0)))

    def test_not_numbers(self):
        area = zones.parse_service_area(
            {"primaryRadiusM": True, "autoTriggerM": "nan", "arrivalRadiusM": "fifty", "homeRadiusKm": [15]}
        )
        self.assertEqual(area.rings_m, ())
        self.assertEqual(area.home_radius_m, None)
        self.assertEqual(len(area.problems), 4)

    def test_empty_values_are_not_set(self):
        area = zones.parse_service_area({"primaryRadiusM": "", "arrivalRadiusM": None})
        self.assertEqual((area.rings_m, area.problems), ((), ()))

    def test_not_an_object(self):
        for payload in (None, [], "800", 800):
            with self.subTest(payload=payload):
                self.assertIsNone(zones.parse_service_area(payload))


class ReadingInsideTests(SimpleTestCase):
    def test_on_the_line_is_inside(self):
        self.assertTrue(zones.reading_inside(800, 800, None, False))

    def test_inside_and_outside(self):
        self.assertTrue(zones.reading_inside(10, 800, None, False))
        self.assertFalse(zones.reading_inside(2000, 800, None, True))

    def test_the_exit_band_holds_the_last_answer(self):
        # 800 m ring: the band runs to 880 m.
        self.assertTrue(zones.reading_inside(850, 800, None, True))
        self.assertFalse(zones.reading_inside(850, 800, None, False))
        self.assertTrue(zones.reading_inside(880, 800, None, True))
        self.assertFalse(zones.reading_inside(880.5, 800, None, True))

    def test_a_small_ring_has_the_minimum_band(self):
        self.assertEqual(zones.exit_margin_m(50), 10)
        self.assertTrue(zones.reading_inside(59, 50, None, True))
        self.assertFalse(zones.reading_inside(61, 50, None, True))

    def test_a_blurred_reading_cannot_tell(self):
        self.assertIsNone(zones.reading_inside(10, 50, 65, False))
        self.assertIsNone(zones.reading_inside(500, 50, 65, True))
        # As wide as the ring is still good enough.
        self.assertTrue(zones.reading_inside(10, 50, 50, False))
        self.assertTrue(zones.reading_inside(10, 800, 65, False))

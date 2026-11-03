"""
The map shows only the customer's country (MAP_COUNTRY_V1,
docs/MAP_COUNTRY_API.md, the mobile team's docs/map-country-fe-contract.md).

The border lookup runs for real, on the borders file the app ships. The
platform's tables are unmanaged (not in the test database), so their
querysets are built and inspected, and the view runs with its selectors
mocked. salon_country is ours, so its writes run for real.
"""

import gzip
import json
import types
import uuid
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.db import DatabaseError
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase, override_settings
from drf_spectacular.generators import SchemaGenerator

from apps.salons import countries, map_country, map_filters, selectors, views
from apps.salons.management.commands import salon_countries
from apps.salons.models import SalonCountry
from apps.salons.params import ParamError, parse_map, parse_user_position

MAP_URL = "/api/v1/discover/map"
SALON = uuid.UUID("33333333-3333-3333-3333-333333333333")
BRANCH_A = uuid.UUID("22222222-2222-2222-2222-2222222222a1")
BRANCH_B = uuid.UUID("22222222-2222-2222-2222-2222222222b2")
DOCS = Path(__file__).resolve().parents[2] / "docs"

# (latitude, longitude)
DUBAI = (25.2048, 55.2708)
MUSCAT = (23.5880, 58.3829)
DHAKA = (23.8103, 90.4125)
RIYADH = (24.7136, 46.6753)
DOHA = (25.2854, 51.5310)
PALM_JUMEIRAH = (25.1124, 55.1390)
DUBAI_MARINA = (25.0800, 55.1400)
AL_AIN = (24.2075, 55.7447)  # UAE, next door to Buraimi
BURAIMI = (24.2500, 55.7900)  # Oman
OFF_DUBAI_20_KM = (25.3500, 55.0500)
OFF_DUBAI_40_KM = (25.5000, 54.9000)
MID_GULF = (26.2000, 53.8000)
MID_ATLANTIC = (30.0, -40.0)


def code(point):
    country = countries.country_at(*point)
    return country.code if country else None


class BordersFileTests(SimpleTestCase):
    """data/country_borders.json.gz, as scripts/build_country_borders.py writes it."""

    def setUp(self):
        with gzip.open(countries.BORDERS_FILE, "rt", encoding="utf-8") as handle:
            self.payload = json.load(handle)

    def test_it_is_natural_earth_and_public_domain(self):
        self.assertIn("Natural Earth", self.payload["source"])
        self.assertIn("public domain", self.payload["source"])

    def test_every_code_is_iso_alpha_2_upper_case_and_once(self):
        codes = [entry["code"] for entry in self.payload["countries"]]
        self.assertEqual(len(codes), len(set(codes)))
        for one in codes:
            self.assertRegex(one, r"^[A-Z]{2}$")
        self.assertGreater(len(codes), 200)

    def test_every_country_has_a_name_and_a_shape(self):
        for entry in self.payload["countries"]:
            with self.subTest(entry["code"]):
                self.assertTrue(entry["name"])
                self.assertTrue(entry["polygons"])

    def test_names_are_the_ones_the_app_shows(self):
        names = {entry["code"]: entry["name"] for entry in self.payload["countries"]}
        self.assertEqual(names["AE"], "United Arab Emirates")
        self.assertEqual(names["BD"], "Bangladesh")
        self.assertEqual(names["SA"], "Saudi Arabia")
        self.assertEqual(names["GB"], "United Kingdom")
        self.assertEqual(names["US"], "United States")
        self.assertEqual(names["CN"], "China")
        # The ones Natural Earth's ISO_A2 leaves at -99.
        self.assertEqual(names["FR"], "France")
        self.assertEqual(names["NO"], "Norway")

    def test_country_by_code(self):
        self.assertEqual(countries.country_by_code("ae"), countries.Country("AE", "United Arab Emirates"))
        self.assertIsNone(countries.country_by_code("ZZ"))
        self.assertIsNone(countries.country_by_code(None))


class CountryAtTests(SimpleTestCase):
    """Section 3: inside the borders, else the nearest country within 25 km."""

    def test_the_cities_in_the_brief(self):
        self.assertEqual(code(DUBAI), "AE")
        self.assertEqual(code(MUSCAT), "OM")
        self.assertEqual(code(DHAKA), "BD")
        self.assertEqual(code(RIYADH), "SA")
        self.assertEqual(code(DOHA), "QA")

    def test_the_coast_and_reclaimed_land_count(self):
        self.assertEqual(code(PALM_JUMEIRAH), "AE")
        self.assertEqual(code(DUBAI_MARINA), "AE")
        self.assertEqual(code(OFF_DUBAI_20_KM), "AE")

    def test_out_at_sea_is_no_country(self):
        self.assertIsNone(code(OFF_DUBAI_40_KM))
        self.assertIsNone(code(MID_GULF))
        self.assertIsNone(code(MID_ATLANTIC))
        self.assertIsNone(code((0.0, 0.0)))

    def test_a_land_border_through_a_town(self):
        self.assertEqual(code(AL_AIN), "AE")
        self.assertEqual(code(BURAIMI), "OM")

    def test_a_few_more(self):
        self.assertEqual(code((51.5074, -0.1278)), "GB")
        self.assertEqual(code((48.8566, 2.3522)), "FR")
        self.assertEqual(code((40.7128, -74.0060)), "US")
        self.assertEqual(code((26.2235, 50.5876)), "BH")
        self.assertEqual(code((29.3759, 47.9774)), "KW")


class GeometryTests(SimpleTestCase):
    """The lookup's own rules, on two made-up square countries."""

    def setUp(self):
        def square(x0, y0, x1, y1):
            # Degrees to the file's whole-number steps (scale 100 here).
            points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            steps = list(points[0])
            for (ax, ay), (bx, by) in zip(points, points[1:]):
                steps += [bx - ax, by - ay]
            return steps

        payload = {
            "source": "test",
            "scale": 100,
            "countries": [
                # AA: 0..10 degrees, with a hole at 4..6 (a lake).
                {"code": "AA", "name": "Aland", "polygons": [[square(0, 0, 1000, 1000), square(400, 400, 600, 600)]]},
                # BB: from 10.5 degrees east.
                {"code": "BB", "name": "Beland", "polygons": [[square(1050, 0, 2000, 1000)]]},
            ],
        }
        patcher = mock.patch.object(countries, "_borders", countries._Borders(payload))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_inside(self):
        self.assertEqual(countries.country_at(2, 2).code, "AA")
        self.assertEqual(countries.country_at(2, 15).code, "BB")

    def test_a_hole_is_not_inside_but_its_shore_is_near(self):
        polygon = countries._borders.shapes[0].polygons[0]
        self.assertFalse(countries._inside(polygon, 500, 500))
        # The middle of the lake is about 111 km from its shore: no country.
        self.assertIsNone(countries.country_at(5, 5))
        # 0.1 degree (about 11 km) from the shore: the country around it.
        self.assertEqual(countries.country_at(5, 4.1).code, "AA")

    def test_between_two_the_nearer_wins(self):
        # The gap is 10 to 10.5 degrees east, at 1 degree north.
        self.assertEqual(countries.country_at(1, 10.1).code, "AA")
        self.assertEqual(countries.country_at(1, 10.4).code, "BB")

    def test_more_than_25_km_from_both_is_none(self):
        self.assertIsNone(countries.country_at(1, -0.3))
        self.assertEqual(countries.country_at(1, -0.2).code, "AA")


class PositionCountryTests(SimpleTestCase):
    """The customer's lookup: rounded to 2 decimals and cached by that."""

    def setUp(self):
        countries._cached_country.cache_clear()
        self.addCleanup(countries._cached_country.cache_clear)

    def test_rounded_then_looked_up_once(self):
        with mock.patch.object(countries, "country_at", wraps=countries.country_at) as looked:
            first = countries.position_country(25.20481, 55.27079)
            second = countries.position_country(25.2012, 55.2687)
        self.assertEqual((first.code, second.code), ("AE", "AE"))
        looked.assert_called_once_with(25.2, 55.27)


class ParseUserPositionTests(SimpleTestCase):
    """Section 7: both or neither, in range, else a 422 on the one to blame."""

    def parse(self, text):
        return parse_user_position(QueryDict(text))

    def test_neither_is_none(self):
        self.assertIsNone(self.parse("latitude=25.2&longitude=55.27"))

    def test_both(self):
        self.assertEqual(self.parse("user_latitude=25.20&user_longitude=55.27"), (25.2, 55.27))

    def test_one_without_the_other(self):
        for text, field in (("user_latitude=25.2", "user_longitude"), ("user_longitude=55.27", "user_latitude")):
            with self.subTest(text), self.assertRaises(ParamError) as caught:
                self.parse(text)
            self.assertEqual(caught.exception.param, field)
            self.assertEqual(caught.exception.message, "Send user_latitude and user_longitude together.")

    def test_out_of_range_or_not_a_number(self):
        for text, field in (
            ("user_latitude=91&user_longitude=55", "user_latitude"),
            ("user_latitude=-90.5&user_longitude=55", "user_latitude"),
            ("user_latitude=25&user_longitude=180.01", "user_longitude"),
            ("user_latitude=25&user_longitude=-181", "user_longitude"),
            ("user_latitude=abc&user_longitude=55", "user_latitude"),
            ("user_latitude=nan&user_longitude=55", "user_latitude"),
        ):
            with self.subTest(text), self.assertRaises(ParamError) as caught:
                self.parse(text)
            self.assertEqual(caught.exception.param, field)

    def test_the_edges_are_in_range(self):
        self.assertEqual(self.parse("user_latitude=-90&user_longitude=180"), (-90.0, 180.0))


class ResolveCountryTests(SimpleTestCase):
    """Section 3's order: the phone, else the map's centre, else none."""

    def resolve(self, user, centre):
        return map_country.resolve_country(user, *(centre or (None, None)))

    def test_the_phone_wins_over_the_map(self):
        # Flew to Riyadh, the map still opens on Dubai: Saudi salons.
        self.assertEqual(self.resolve(RIYADH, DUBAI).code, "SA")
        # In Dubai, panned to Muscat: still the UAE.
        self.assertEqual(self.resolve(DUBAI, MUSCAT).code, "AE")

    def test_location_off_is_the_map_s_centre(self):
        self.assertEqual(self.resolve(None, DUBAI).code, "AE")

    def test_a_phone_out_at_sea_falls_back_to_the_centre(self):
        self.assertEqual(self.resolve(MID_GULF, DUBAI).code, "AE")

    def test_nothing_answers_is_none(self):
        self.assertIsNone(self.resolve(None, MID_GULF))
        self.assertIsNone(self.resolve(MID_GULF, MID_GULF))
        self.assertIsNone(self.resolve(None, None))

    def test_a_country_with_no_salons_is_still_that_country(self):
        self.assertEqual(self.resolve(DHAKA, DHAKA).code, "BD")


class CountryPayloadTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_none_is_null(self):
        self.assertIsNone(map_country.country_payload(None))

    def test_has_salons_is_the_whole_country(self):
        cache.set(map_country.COUNTS_KEY, {"AE": 3})
        with mock.patch.object(map_country, "live_salon_counts_query", side_effect=AssertionError("cached")):
            self.assertEqual(
                map_country.country_payload(countries.Country("AE", "United Arab Emirates")),
                {"code": "AE", "name": "United Arab Emirates", "has_salons": True},
            )
            self.assertEqual(
                map_country.country_payload(countries.Country("BD", "Bangladesh")),
                {"code": "BD", "name": "Bangladesh", "has_salons": False},
            )

    def test_counts_are_read_once_a_minute(self):
        rows = [{"country_code": "AE", "salons": 2}, {"country_code": "OM", "salons": 1}]
        with mock.patch.object(map_country, "live_salon_counts_query", return_value=rows) as query:
            self.assertEqual(map_country.live_salon_counts(), {"AE": 2, "OM": 1})
            self.assertEqual(map_country.live_salon_counts(), {"AE": 2, "OM": 1})
        query.assert_called_once()
        self.assertEqual(map_country.COUNTS_SECONDS, 60)

    def test_the_count_query_is_every_live_salon_grouped_by_country(self):
        sql = str(map_country.live_salon_counts_query().query)
        self.assertIn('"salon_country"', sql)
        self.assertIn("GROUP BY", sql)
        self.assertIn('"visibility" = PUBLIC', sql)
        self.assertNotIn("LIMIT 100", sql)


class SelectorTests(SimpleTestCase):
    """The country goes into the map's query first, before every filter and the cap."""

    def test_no_country_is_the_query_as_before(self):
        sql = str(selectors.map_venue_queryset(latitude=25.2, longitude=55.27,
                                               latitude_delta=1, longitude_delta=1).query)
        self.assertNotIn("salon_country", sql)

    def test_a_country_is_an_exists_on_salon_country(self):
        sql = str(selectors.map_venue_queryset(latitude=25.2, longitude=55.27, latitude_delta=1,
                                               longitude_delta=1, country="AE").query)
        self.assertIn(
            'EXISTS(SELECT 1 AS "a" FROM "salon_country" U0 WHERE (U0."branch_id" = '
            '("storefront"."branch_id") AND U0."country_code" = AE)',
            sql,
        )

    def test_the_cap_comes_after_the_country(self):
        capped = selectors.map_venues(latitude=25.2, longitude=55.27, latitude_delta=1,
                                      longitude_delta=1, limit=5, country="AE")
        self.assertEqual((capped.query.low_mark, capped.query.high_mark), (0, 5))
        self.assertIn("salon_country", str(capped.query))

    def test_the_filters_path_gets_the_country_too(self):
        params = parse_map(QueryDict("latitude=25.2&longitude=55.27"))
        params["country"] = "AE"
        qs, _ = map_filters.filtered_queryset(params, map_filters.NO_FILTERS)
        self.assertIn('FROM "salon_country" U0', str(qs.query))
        self.assertIn('U0."country_code" = AE', str(qs.query))

    def test_map_salons_is_public_with_a_pin(self):
        sql = str(selectors.map_salons().query)
        self.assertIn('"visibility" = PUBLIC', sql)
        self.assertIn('"deleted_at" IS NULL', sql)
        self.assertIn("IS NOT NULL", sql)


class StaleBranchesTests(SimpleTestCase):
    def test_missing_or_moved_pins_only(self):
        sql = str(map_country.stale_branches().query)
        self.assertIn(
            'NOT (EXISTS(SELECT 1 AS "a" FROM "salon_country" U0 WHERE (U0."branch_id" = '
            '("branch"."id") AND U0."lat" = ("branch"."lat") AND U0."lng" = ("branch"."lng"))',
            sql,
        )
        self.assertIn('V0."visibility" = PUBLIC', sql)
        self.assertIn("ORDER BY 1 ASC", sql)


class SalonCountryWriteTests(TestCase):
    """salon_country is ours: written for real."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_work_out_uses_the_same_borders(self):
        rows = map_country.work_out([(BRANCH_A, *DUBAI), (BRANCH_B, *MID_GULF)])
        self.assertEqual([(row.branch_id, row.country_code) for row in rows], [(BRANCH_A, "AE"), (BRANCH_B, None)])
        self.assertEqual((rows[0].lat, rows[0].lng), DUBAI)

    def test_save_adds_and_redoes_rows(self):
        SalonCountry.objects.create(branch_id=BRANCH_A, lat=1.0, lng=1.0, country_code="AE")
        cache.set(map_country.COUNTS_KEY, {"AE": 1})
        written = map_country.save([
            SalonCountry(branch_id=BRANCH_A, lat=MUSCAT[0], lng=MUSCAT[1], country_code="OM"),
            SalonCountry(branch_id=BRANCH_B, lat=MID_GULF[0], lng=MID_GULF[1], country_code=None),
        ])
        self.assertEqual(written, 2)
        self.assertEqual(
            sorted(SalonCountry.objects.values_list("branch_id", "lat", "lng", "country_code")),
            [(BRANCH_A, MUSCAT[0], MUSCAT[1], "OM"), (BRANCH_B, MID_GULF[0], MID_GULF[1], None)],
        )
        # A salon moved country: has_salons counts again.
        self.assertIsNone(cache.get(map_country.COUNTS_KEY))

    def test_save_nothing(self):
        self.assertEqual(map_country.save([]), 0)

    def test_refresh_does_at_most_the_batch(self):
        stale = [(BRANCH_A, *DUBAI), (BRANCH_B, *MUSCAT)]
        with mock.patch.object(map_country, "stale_branches", return_value=stale):
            self.assertEqual(map_country.refresh_salon_countries(limit=1), 1)
            self.assertEqual(list(SalonCountry.objects.values_list("country_code", flat=True)), ["AE"])
            self.assertEqual(map_country.refresh_salon_countries(limit=None), 2)
        self.assertEqual(sorted(SalonCountry.objects.values_list("country_code", flat=True)), ["AE", "OM"])


class KeepSalonsFreshTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_once_a_minute(self):
        with mock.patch.object(map_country, "refresh_salon_countries", return_value=0) as refresh, \
                mock.patch.object(map_country.transaction, "atomic"):
            map_country.keep_salons_fresh()
            map_country.keep_salons_fresh()
        refresh.assert_called_once_with()
        self.assertTrue(cache.get(map_country.CHECKED_KEY))

    def test_a_failure_is_logged_and_the_map_goes_on(self):
        with mock.patch.object(map_country, "refresh_salon_countries", side_effect=DatabaseError("down")), \
                mock.patch.object(map_country.transaction, "atomic"), \
                self.assertLogs("apps.salons.map_country", "ERROR") as logged:
            map_country.keep_salons_fresh()
        self.assertIn("salon countries not refreshed", logged.output[0])


def venue(pk):
    return types.SimpleNamespace(pk=pk, id=pk, lat=25.2, lng=55.27, avg_rating=4.5, photo_url=None)


class DiscoverMapCountryViewTests(SimpleTestCase):
    """GET /discover/map through the real URL, its selectors mocked."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        countries._cached_country.cache_clear()
        for target, name, kwargs in (
            (views, "map_venues", {"return_value": [venue(SALON)]}),
            (map_filters, "filtered_map_venues", {"return_value": ([venue(SALON)], 7)}),
            (map_country, "keep_salons_fresh", {}),
            (map_country, "live_salon_counts", {"return_value": {"AE": 12, "OM": 5}}),
        ):
            patcher = mock.patch.object(target, name, **kwargs)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def get(self, query):
        return self.client.get(f"{MAP_URL}?{query}")

    def map_call(self, user=None, centre=DUBAI, extra=""):
        query = f"latitude={centre[0]}&longitude={centre[1]}&latitudeDelta=0.12&longitudeDelta=0.08&zoom=12&limit=100"
        if user:
            query += f"&user_latitude={user[0]:.2f}&user_longitude={user[1]:.2f}"
        return self.get(query + extra)

    def test_off_the_params_are_ignored_and_there_is_no_country(self):
        response = self.get("latitude=25.2&longitude=55.27&user_latitude=999&user_longitude=nope")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("country", response.json())
        self.assertIsNone(self.map_venues.call_args.kwargs["country"])
        self.keep_salons_fresh.assert_not_called()

    @override_settings(MAP_COUNTRY_V1=True)
    def test_in_dubai_the_uae_only(self):
        response = self.map_call(user=DUBAI)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.map_venues.call_args.kwargs["country"], "AE")
        self.assertEqual(response.json(), {
            "mode": "markers",
            "count": 1,
            "venues": [{"id": str(SALON), "lat": 25.2, "lng": 55.27, "rating": 4.5, "salon_profile_image": None}],
            "country": {"code": "AE", "name": "United Arab Emirates", "has_salons": True},
        })
        self.keep_salons_fresh.assert_called_once_with()

    @override_settings(MAP_COUNTRY_V1=True)
    def test_panned_to_muscat_is_still_the_uae_and_empty(self):
        self.map_venues.return_value = []
        response = self.map_call(user=DUBAI, centre=MUSCAT)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.map_venues.call_args.kwargs["country"], "AE")
        body = response.json()
        self.assertEqual((body["count"], body["venues"]), (0, []))
        self.assertEqual(body["country"], {"code": "AE", "name": "United Arab Emirates", "has_salons": True})

    @override_settings(MAP_COUNTRY_V1=True)
    def test_where_gostyle_has_no_salons(self):
        self.map_venues.return_value = []
        response = self.map_call(user=DHAKA, centre=DHAKA)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)
        self.assertEqual(response.json()["country"], {"code": "BD", "name": "Bangladesh", "has_salons": False})

    @override_settings(MAP_COUNTRY_V1=True)
    def test_flew_to_riyadh_the_map_still_on_dubai(self):
        response = self.map_call(user=RIYADH, centre=DUBAI)
        self.assertEqual(self.map_venues.call_args.kwargs["country"], "SA")
        self.assertEqual(response.json()["country"]["code"], "SA")

    @override_settings(MAP_COUNTRY_V1=True)
    def test_location_off_the_map_s_centre(self):
        response = self.map_call(user=None, centre=DUBAI)
        self.assertEqual(self.map_venues.call_args.kwargs["country"], "AE")
        self.assertEqual(response.json()["country"]["code"], "AE")

    @override_settings(MAP_COUNTRY_V1=True)
    def test_centre_out_at_sea_no_filter_and_null(self):
        response = self.map_call(user=None, centre=MID_GULF)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.map_venues.call_args.kwargs["country"])
        self.assertIsNone(response.json()["country"])

    @override_settings(MAP_COUNTRY_V1=True)
    def test_no_position_at_all_is_null(self):
        response = self.get("limit=10")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["country"])

    @override_settings(MAP_COUNTRY_V1=True, MAP_FILTER_V1=True)
    def test_the_filters_get_the_country_first(self):
        response = self.map_call(user=DUBAI, extra="&rating=4")
        self.assertEqual(response.status_code, 200)
        self.map_venues.assert_not_called()
        map_params, _ = self.filtered_map_venues.call_args.args
        self.assertEqual(map_params["country"], "AE")
        self.assertEqual(response.json()["count"], 7)
        self.assertEqual(response.json()["country"]["code"], "AE")

    @override_settings(MAP_COUNTRY_V1=True)
    def test_one_without_the_other_is_a_422(self):
        for query, field in (
            ("user_latitude=25.20", "user_longitude"),
            ("user_longitude=55.27&latitude=25.2&longitude=55.27", "user_latitude"),
        ):
            with self.subTest(query):
                response = self.get(query)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "validation_error")
                self.assertEqual(response.json()["errors"], [{
                    "field": field, "code": "invalid",
                    "message": "Send user_latitude and user_longitude together.",
                }])
        self.map_venues.assert_not_called()

    @override_settings(MAP_COUNTRY_V1=True)
    def test_out_of_range_is_a_422(self):
        for query, field in (
            ("user_latitude=90.01&user_longitude=55", "user_latitude"),
            ("user_latitude=25&user_longitude=-180.5", "user_longitude"),
        ):
            with self.subTest(query):
                response = self.get(query)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["errors"][0]["field"], field)

    @override_settings(MAP_COUNTRY_V1=True)
    def test_every_param_of_today_still_works(self):
        self.map_call(user=DUBAI, extra="&category=gents&radius=5000")
        self.assertEqual(self.map_venues.call_args.kwargs, {
            "latitude": DUBAI[0], "longitude": DUBAI[1], "latitude_delta": 0.12,
            "longitude_delta": 0.08, "category": "gents", "radius_km": 5.0,
            "limit": 100.0, "country": "AE",
        })


class CommandTests(SimpleTestCase):
    """manage.py salon_countries: a dry run unless --apply."""

    def setUp(self):
        rows = [SalonCountry(branch_id=BRANCH_A, lat=DUBAI[0], lng=DUBAI[1], country_code="AE")]
        for target, name, kwargs in (
            (map_country, "stale_branches", {"return_value": []}),
            (map_country, "work_out", {"return_value": rows}),
            (map_country, "save", {"return_value": 1}),
            (map_country, "live_salon_counts", {"return_value": {"AE": 1}}),
            (salon_countries, "Branch", {}),
        ):
            patcher = mock.patch.object(target, name, **kwargs)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.Branch.objects.filter.return_value.values.return_value = [
            {"id": BRANCH_A, "name": "Iron Razor", "city": "Dubai", "country_code": "OM"},
        ]

    def run_command(self, *args):
        out = StringIO()
        call_command("salon_countries", *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_writes_nothing(self):
        out = self.run_command()
        self.save.assert_not_called()
        self.assertIn("Iron Razor (Dubai)", out)
        self.assertIn("country AE", out)
        self.assertIn("address says OM  (address says another country)", out)
        self.assertIn("Nothing written", out)
        self.assertIn("AE  1", out)

    def test_apply_writes(self):
        out = self.run_command("--apply")
        self.save.assert_called_once()
        self.assertIn("Written: 1", out)


class OpenApiTests(SimpleTestCase):
    def setUp(self):
        self.schema = SchemaGenerator().get_schema(request=None, public=True)

    def test_the_map_documents_both_params(self):
        names = {p["name"] for p in self.schema["paths"][MAP_URL]["get"]["parameters"]}
        self.assertLessEqual({"user_latitude", "user_longitude"}, names)

    def test_the_answer_documents_country(self):
        properties = self.schema["components"]["schemas"]["MapVenues"]["properties"]
        self.assertIn("country", properties)
        country = self.schema["components"]["schemas"]["MapCountry"]
        self.assertEqual(set(country["properties"]), {"code", "name", "has_salons"})


class DocsTests(SimpleTestCase):
    def test_no_em_dash(self):
        for name in ("MAP_COUNTRY_API.md", "MAP_COUNTRY_SERVER.md", "map-country-fe-contract.md"):
            with self.subTest(name):
                self.assertNotIn("\u2014", (DOCS / name).read_text())

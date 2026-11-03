"""
The map drawer's Parking chip (MAP_FILTER_PARKING_V1, docs/MAP_FILTERS_API.md
section 6).

The tests that matter most pin the switch OFF: the drawer, the parse and the
map must be exactly what they were. The querysets are built and inspected,
never run, as in test_map_filters.py: the platform's tables do not exist in
the test database.
"""

import uuid
from pathlib import Path
from unittest import mock

from django.db.models import Exists
from django.test import SimpleTestCase, override_settings

from apps.salons import map_filters, views
from apps.salons.map_filters import MapFilters, parse_map_filters
from apps.salons.params import ParamError
from apps.salons.test_map_filters import (
    CATALOG,
    DRAWER_URL,
    MAP,
    MAP_URL,
    NOW,
    exists_conditions,
    picks,
    q,
    venue,
)

FLAG = "MAP_FILTER_PARKING_V1"
SALON = uuid.UUID("33333333-3333-3333-3333-3333333333c3")


def additional_section():
    with mock.patch.object(map_filters, "category_catalog", return_value=CATALOG):
        sections = {s["key"]: s for s in map_filters.drawer()["sections"]}
    return sections["additional"]


class SwitchOffTests(SimpleTestCase):
    """Off is the default, and it is exactly as before."""

    def test_the_drawer_offers_only_open_now(self):
        section = additional_section()
        self.assertEqual(picks(section), ["open_now"])
        self.assertEqual(section["default"], ["open_now"])

    def test_parking_is_refused_like_any_id_not_offered(self):
        with self.assertRaises(ParamError) as caught:
            parse_map_filters(q("additional=parking"))
        self.assertEqual(
            (caught.exception.param, caught.exception.code, caught.exception.message),
            ("additional", "invalid_choice", "Expected one or more of: open_now. Got 'parking'."),
        )

    def test_open_now_alone_reads_as_before(self):
        self.assertEqual(parse_map_filters(q("additional=open_now")), MapFilters(open_now=True))

    def test_the_compose_file_carries_the_switch_off(self):
        self.assertIn(f"{FLAG}: ${{{FLAG}:-False}}", Path("docker-compose.yml").read_text())


@override_settings(MAP_FILTER_PARKING_V1=True)
class SwitchOnTests(SimpleTestCase):
    def test_the_drawer_offers_parking_after_open_now_and_still_opens_on_open_now(self):
        section = additional_section()
        self.assertEqual(
            section["options"],
            [{"id": "open_now", "label": "Open now"}, {"id": "parking", "label": "Parking"}],
        )
        self.assertEqual(section["default"], ["open_now"])

    def test_every_id_the_drawer_offers_is_one_the_map_takes(self):
        for option_id in picks(additional_section()):
            with self.subTest(id=option_id):
                parse_map_filters(q(f"additional={option_id}"))

    def test_parking_alone_and_with_open_now(self):
        self.assertEqual(parse_map_filters(q("additional=parking")), MapFilters(parking=True))
        self.assertEqual(
            parse_map_filters(q("additional=open_now,PARKING")),
            MapFilters(open_now=True, parking=True),
        )

    def test_parking_is_a_filter(self):
        self.assertTrue(MapFilters(parking=True).active)
        self.assertFalse(MapFilters().active)

    def test_an_unknown_id_is_still_refused(self):
        with self.assertRaises(ParamError) as caught:
            parse_map_filters(q("additional=verified"))
        self.assertEqual(
            caught.exception.message,
            "Expected one or more of: open_now, parking. Got 'verified'.",
        )


class ParkingQueryTests(SimpleTestCase):
    """filtered_queryset: one EXISTS on the live version's Amenities row."""

    def build(self, filters):
        with mock.patch.object(map_filters, "category_catalog", return_value=CATALOG):
            return map_filters.filtered_queryset(MAP, filters)

    def test_no_parking_no_exists(self):
        qs, _ = self.build(MapFilters(rating="4"))
        self.assertEqual(exists_conditions(qs.query), [])

    def test_parking_is_an_exists_on_the_live_version_listing_it(self):
        qs, picked = self.build(MapFilters(parking=True))
        self.assertIsNone(picked)
        (exists,) = exists_conditions(qs.query)
        inner = exists.query
        self.assertEqual(inner.model.__name__, "StorefrontVersion")

        conditions = {cond.lookup_name: cond for cond in inner.where.children}
        # The salon's own published version, the one its profile shows.
        outer = conditions["exact"].rhs.target
        self.assertEqual((outer.model.__name__, outer.name), ("Storefront", "live_version_id"))
        self.assertEqual(conditions["exact"].lhs.target.name, "id")
        # snapshot -> 'AMENITIES' -> 'items' @> '["PARKING"]'
        contains = conditions["contains"]
        self.assertEqual(contains.rhs, ["PARKING"])
        self.assertEqual(contains.lhs.key_name, "items")
        self.assertEqual(contains.lhs.lhs.key_name, "AMENITIES")
        self.assertEqual(contains.lhs.lhs.lhs.target.name, "snapshot")

    def test_with_service_categories_both_exists_are_there(self):
        hair = next(iter(CATALOG))
        qs, picked = self.build(MapFilters(parking=True, service_categories=(hair,)))
        self.assertEqual(picked, [hair])
        self.assertEqual(len(exists_conditions(qs.query)), 2)
        self.assertTrue(all(isinstance(e, Exists) for e in exists_conditions(qs.query)))


class ParkingCountTests(SimpleTestCase):
    def test_parking_alone_counts_in_sql_not_as_a_plain_call(self):
        fake_qs = mock.MagicMock()
        fake_qs.__getitem__.return_value = ["a"]
        fake_qs.count.return_value = 9
        with mock.patch.object(map_filters, "filtered_queryset", return_value=(fake_qs, None)):
            kept, count = map_filters.filtered_map_venues(
                {**MAP, "limit": 1}, MapFilters(parking=True), NOW
            )
        self.assertEqual((kept, count), (["a"], 9))


@override_settings(MAP_FILTER_V1=True)
class ViewTests(SimpleTestCase):
    """Through the real URLs, the selectors mocked."""

    def setUp(self):
        patcher = mock.patch.object(views, "map_venues", return_value=[venue(SALON)])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            map_filters, "filtered_map_venues", return_value=([venue(SALON)], 1)
        )
        self.filtered = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(map_filters, "category_catalog", return_value=CATALOG)
        patcher.start()
        self.addCleanup(patcher.stop)

    def get_map(self, query):
        return self.client.get(f"{MAP_URL}?latitude=25.2&longitude=55.27&{query}")

    def test_off_parking_is_a_400_and_nothing_is_filtered(self):
        response = self.get_map("additional=parking")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["errors"][0]["field"], "additional")
        self.filtered.assert_not_called()

    def test_off_the_drawer_has_no_parking(self):
        sections = {s["key"]: s for s in self.client.get(DRAWER_URL).json()["sections"]}
        self.assertEqual(picks(sections["additional"]), ["open_now"])

    @override_settings(MAP_FILTER_PARKING_V1=True)
    def test_on_parking_filters_and_the_answer_keeps_its_shape(self):
        response = self.get_map("additional=parking")
        self.assertEqual(response.status_code, 200)
        _, filters = self.filtered.call_args.args
        self.assertEqual(filters, MapFilters(parking=True))
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["venues"][0]["id"], str(SALON))

    @override_settings(MAP_FILTER_PARKING_V1=True)
    def test_on_the_drawer_offers_it(self):
        sections = {s["key"]: s for s in self.client.get(DRAWER_URL).json()["sections"]}
        self.assertEqual(picks(sections["additional"]), ["open_now", "parking"])

    @override_settings(MAP_FILTER_V1=False, MAP_FILTER_PARKING_V1=True)
    def test_without_the_drawer_switch_it_is_ignored_as_every_drawer_param_is(self):
        response = self.get_map("additional=parking")
        self.assertEqual(response.status_code, 200)
        self.filtered.assert_not_called()


class GuideTests(SimpleTestCase):
    """docs/MAP_FILTERS_API.md section 6 shows what the drawer answers, switch on."""

    @override_settings(MAP_FILTER_PARKING_V1=True)
    def test_the_section_example_is_the_drawer_s_own(self):
        import json

        from django.conf import settings

        text = (settings.BASE_DIR / "docs" / "MAP_FILTERS_API.md").read_text(encoding="utf-8")
        section = text.split("## 6. The Parking chip", 1)[1]
        block = section.split("```json\n", 1)[1].split("```", 1)[0]
        self.assertEqual(json.loads(block), additional_section())

"""
The map drawer's Rating chips as the Figma shows them (MAP_RATING_FIGMA_V1,
docs/SEARCH_DISCOVERY_AUDIT.md X2, D7): 5, 4.0+, 3.0+, 2.0+, 1.0+, and no
4.5+. Off, the chips are as before.

The querysets are built and inspected, never run. The views are called
through their real URLs with their selectors mocked.
"""

import types
import uuid
from unittest import mock

from django.db.models import Exists
from django.http import QueryDict
from django.test import SimpleTestCase, override_settings

from apps.salons import map_filters
from apps.salons.map_filters import MapFilters, parse_map_filters
from apps.salons.params import ParamError, parse_map

SALON = uuid.UUID("33333333-3333-3333-3333-3333333333a1")
MAP_URL = "/api/v1/discover/map"
DRAWER_URL = "/api/v1/me/map/filters"


def rating_section():
    with mock.patch.object(map_filters, "category_catalog", return_value={}):
        sections = {s["key"]: s for s in map_filters.drawer()["sections"]}
    return sections["rating"]


def gte_values(qs):
    """The values of every plain `>=` in the WHERE (no box is sent, so only the rating's)."""
    return [
        cond.rhs for cond in qs.query.where.children
        if getattr(cond, "lookup_name", None) == "gte"
        and not isinstance(getattr(cond, "lhs", None), Exists)
    ]


def floor_for(rating):
    with mock.patch.object(map_filters, "category_catalog", return_value={}):
        qs, _ = map_filters.filtered_queryset(parse_map(QueryDict("")), MapFilters(rating=rating))
    return gte_values(qs)


class OffTests(SimpleTestCase):
    """Off (the default): the chips and the ids the map takes are today's."""

    def test_the_chips_are_as_before(self):
        self.assertEqual(
            rating_section()["options"],
            [{"id": "any", "label": "Any"}, {"id": "3", "label": "3.0+"},
             {"id": "4", "label": "4.0+"}, {"id": "4.5", "label": "4.5+"},
             {"id": "5", "label": "5.0"}],
        )

    def test_4_5_is_taken_and_2_and_1_are_refused(self):
        self.assertEqual(parse_map_filters(QueryDict("rating=4.5")).rating, "4.5")
        for rating in ("2", "1"):
            with self.subTest(rating=rating), self.assertRaises(ParamError) as caught:
                parse_map_filters(QueryDict(f"rating={rating}"))
            self.assertEqual(caught.exception.code, "invalid_choice")


@override_settings(MAP_RATING_FIGMA_V1=True)
class OnTests(SimpleTestCase):
    """On: the Figma's chips, highest first."""

    def test_the_figma_s_chips_highest_first(self):
        self.assertEqual(
            rating_section()["options"],
            [{"id": "any", "label": "Any"}, {"id": "5", "label": "5"},
             {"id": "4", "label": "4.0+"}, {"id": "3", "label": "3.0+"},
             {"id": "2", "label": "2.0+"}, {"id": "1", "label": "1.0+"}],
        )

    def test_the_section_s_shape_and_default_are_unchanged(self):
        section = rating_section()
        self.assertEqual(
            (section["key"], section["title"], section["multiple"], section["default"]),
            ("rating", "Rating", False, ["any"]),
        )

    def test_every_chip_offered_is_one_the_map_takes(self):
        for option in rating_section()["options"]:
            with self.subTest(id=option["id"]):
                parse_map_filters(QueryDict(f"rating={option['id']}"))

    def test_4_5_is_now_refused_like_any_id_not_offered(self):
        with self.assertRaises(ParamError) as caught:
            parse_map_filters(QueryDict("rating=4.5"))
        self.assertEqual((caught.exception.param, caught.exception.code), ("rating", "invalid_choice"))
        self.assertIn("5, 4, 3, 2, 1", caught.exception.message)

    def test_the_floor_is_the_pick_itself(self):
        for rating, floor in (("5", 5.0), ("4", 4.0), ("3", 3.0), ("2", 2.0), ("1", 1.0)):
            with self.subTest(rating=rating):
                self.assertEqual(floor_for(rating), [floor])


def venue():
    return types.SimpleNamespace(pk=SALON, id=SALON, lat=25.2, lng=55.27, avg_rating=2.4, photo_url=None)


@override_settings(MAP_FILTER_V1=True)
class ViewTests(SimpleTestCase):
    """Through the real URLs."""

    def setUp(self):
        patcher = mock.patch.object(map_filters, "category_catalog", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(map_filters, "filtered_map_venues", return_value=([venue()], 1))
        self.filtered = patcher.start()
        self.addCleanup(patcher.stop)

    def rating_ids(self):
        body = self.client.get(DRAWER_URL).json()
        (section,) = [s for s in body["sections"] if s["key"] == "rating"]
        return [o["id"] for o in section["options"]]

    def test_off_the_drawer_and_the_map_are_as_before(self):
        self.assertEqual(self.rating_ids(), ["any", "3", "4", "4.5", "5"])
        self.assertEqual(self.client.get(f"{MAP_URL}?rating=2").status_code, 400)
        self.assertEqual(self.client.get(f"{MAP_URL}?rating=4.5").status_code, 200)

    @override_settings(MAP_RATING_FIGMA_V1=True)
    def test_on_the_drawer_offers_the_figma_s_chips(self):
        self.assertEqual(self.rating_ids(), ["any", "5", "4", "3", "2", "1"])

    @override_settings(MAP_RATING_FIGMA_V1=True)
    def test_on_the_map_takes_2_and_refuses_4_5(self):
        response = self.client.get(f"{MAP_URL}?rating=2")
        self.assertEqual(response.status_code, 200)
        _, filters = self.filtered.call_args.args
        self.assertEqual(filters.rating, "2")

        refused = self.client.get(f"{MAP_URL}?rating=4.5")
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()["errors"][0]["field"], "rating")

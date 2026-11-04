"""
The map pin's logo (MAP_PIN_LOGO_V1, docs/SEARCH_DISCOVERY_AUDIT.md X1):
every venue of GET /discover/map also sends `logo_url`, by the card's own
LOGO rule. Off, the venues are exactly as before.

The querysets are built and inspected, never run (the platform's tables are
unmanaged). The view is called through its real URL with its selectors
mocked.
"""

import types
import uuid
from unittest import mock

from django.http import QueryDict
from django.test import SimpleTestCase, override_settings

from apps.salons import map_filters, selectors, views
from apps.salons.map_filters import MapFilters
from apps.salons.params import parse_map
from apps.salons.serializers import MapVenueSerializer

SALON = uuid.UUID("33333333-3333-3333-3333-3333333333a1")
MAP_URL = "/api/v1/discover/map"
LOGO = "https://cdn.example/logo.png"


def venue(logo=None, **extra):
    row = types.SimpleNamespace(
        pk=SALON, id=SALON, lat=25.2, lng=55.27, avg_rating=4.5, photo_url="https://cdn.example/cover.jpg",
    )
    if logo is not None:
        row.logo_url = logo
    for key, value in extra.items():
        setattr(row, key, value)
    return row


def conditions(subquery):
    """{(field, lookup): value} of a Subquery's own WHERE (once annotated, Django keeps its Query)."""
    query = getattr(subquery, "query", subquery)
    found = {}
    for cond in query.where.children:
        target = getattr(getattr(cond, "lhs", None), "target", None)
        if target is not None:
            found[(target.name, cond.lookup_name)] = cond.rhs
    return found


class LogoRuleTests(SimpleTestCase):
    """selectors.logo_url_subquery: the one LOGO rule."""

    def test_the_newest_public_approved_live_logo(self):
        sub = selectors.logo_url_subquery()
        found = conditions(sub)
        self.assertEqual(found[("kind", "exact")], "LOGO")
        self.assertIs(found[("is_public", "exact")], True)
        self.assertEqual(found[("moderation_status", "exact")], "APPROVED")
        self.assertIs(found[("deleted_at", "isnull")], True)
        self.assertEqual(tuple(sub.query.order_by), ("-created_at",))
        self.assertEqual(sub.query.model.__name__, "StorefrontMedia")

    def test_the_card_reads_it_from_there(self):
        with mock.patch.object(selectors, "logo_url_subquery", wraps=selectors.logo_url_subquery) as rule:
            selectors.discoverable_salons()
        rule.assert_called_once_with()


class PinQueryTests(SimpleTestCase):
    """map_venue_queryset: the logo only with the switch on."""

    def build(self):
        return selectors.map_venue_queryset(
            latitude=25.2, longitude=55.27, latitude_delta=0.1, longitude_delta=0.1,
        )

    def test_off_the_marker_query_has_no_logo(self):
        self.assertNotIn("logo_url", self.build().query.annotations)

    @override_settings(MAP_PIN_LOGO_V1=True)
    def test_on_the_pin_reads_the_card_s_rule(self):
        with mock.patch.object(selectors, "logo_url_subquery", wraps=selectors.logo_url_subquery) as rule:
            qs = self.build()
        rule.assert_called_once_with()
        self.assertIn("logo_url", qs.query.annotations)
        self.assertEqual(conditions(qs.query.annotations["logo_url"])[("kind", "exact")], "LOGO")

    @override_settings(MAP_PIN_LOGO_V1=True)
    def test_on_the_cover_stays_the_profile_image(self):
        qs = self.build()
        self.assertIn("photo_url", qs.query.annotations)
        self.assertEqual(conditions(qs.query.annotations["photo_url"])[("kind", "exact")], "COVER")

    @override_settings(MAP_PIN_LOGO_V1=True)
    def test_on_the_drawer_s_filtered_query_has_it_too(self):
        with mock.patch.object(map_filters, "category_catalog", return_value={}):
            qs, _ = map_filters.filtered_queryset(
                parse_map(QueryDict("latitude=25.2&longitude=55.27")), MapFilters(rating="4")
            )
        self.assertIn("logo_url", qs.query.annotations)


class PinShapeTests(SimpleTestCase):
    """MapVenueSerializer: one more key with the switch on, none off."""

    def test_off_the_venue_is_exactly_as_before(self):
        self.assertEqual(
            MapVenueSerializer(venue(logo=LOGO)).data,
            {"id": str(SALON), "lat": 25.2, "lng": 55.27, "rating": 4.5,
             "salon_profile_image": "https://cdn.example/cover.jpg"},
        )

    @override_settings(MAP_PIN_LOGO_V1=True)
    def test_on_the_logo_comes_after_the_profile_image(self):
        data = MapVenueSerializer(venue(logo=LOGO)).data
        self.assertEqual(
            list(data), ["id", "lat", "lng", "rating", "salon_profile_image", "logo_url"]
        )
        self.assertEqual(data["logo_url"], LOGO)

    @override_settings(MAP_PIN_LOGO_V1=True)
    def test_on_no_logo_is_null(self):
        self.assertIsNone(MapVenueSerializer(venue()).data["logo_url"])
        self.assertIsNone(MapVenueSerializer(venue(logo_url=None)).data["logo_url"])


class PinViewTests(SimpleTestCase):
    """GET /discover/map through the real URL."""

    def setUp(self):
        patcher = mock.patch.object(views, "map_venues", return_value=[venue(logo=LOGO)])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            map_filters, "filtered_map_venues", return_value=([venue(logo=LOGO)], 1)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_off_the_answer_is_today_s(self):
        body = self.client.get(f"{MAP_URL}?latitude=25.2&longitude=55.27").json()
        self.assertEqual(body, {
            "mode": "markers", "count": 1,
            "venues": [{"id": str(SALON), "lat": 25.2, "lng": 55.27, "rating": 4.5,
                        "salon_profile_image": "https://cdn.example/cover.jpg"}],
        })

    @override_settings(MAP_PIN_LOGO_V1=True)
    def test_on_a_plain_call_sends_the_logo(self):
        body = self.client.get(f"{MAP_URL}?latitude=25.2&longitude=55.27").json()
        self.assertEqual(body["venues"][0]["logo_url"], LOGO)

    @override_settings(MAP_PIN_LOGO_V1=True, MAP_FILTER_V1=True)
    def test_on_a_filtered_call_sends_the_logo_too(self):
        body = self.client.get(f"{MAP_URL}?latitude=25.2&longitude=55.27&rating=4").json()
        self.assertEqual(body["venues"][0]["logo_url"], LOGO)

"""
The type-ahead (SEARCH_SUGGEST_V1, docs/SEARCH_SUGGEST_API.md): GET
/search/suggest.

The querysets are built and inspected, never run (the platform's tables do
not exist in the test database); the readers are fed rows through mocks.
The ranking and the grouping run for real.
"""

import uuid
from unittest import mock

from django.core.cache import cache
from django.db.models import Exists
from django.http import QueryDict
from django.test import SimpleTestCase, override_settings
from rest_framework.throttling import ScopedRateThrottle

from apps.salons import search_suggest
from apps.salons.params import ParamError

URL = "/api/v1/search/suggest"
SALON_A = uuid.UUID("33333333-3333-3333-3333-3333333333a1")
SALON_B = uuid.UUID("33333333-3333-3333-3333-3333333333b2")
TENANT_A = uuid.UUID("11111111-1111-1111-1111-1111111111a0")
TENANT_B = uuid.UUID("11111111-1111-1111-1111-1111111111b0")
BRANCH_A = uuid.UUID("22222222-2222-2222-2222-2222222222a1")


class Rows:
    """A stand-in queryset: every call returns itself, slicing and iterating give `rows`."""

    def __init__(self, rows):
        self.rows = list(rows)

    def __getattr__(self, name):
        return lambda *args, **kwargs: self

    def __getitem__(self, item):
        return self.rows[item]

    def __iter__(self):
        return iter(self.rows)


class ParseTests(SimpleTestCase):
    def parse(self, text):
        return search_suggest.parse(QueryDict(text))

    def refused(self, text):
        with self.assertRaises(ParamError) as caught:
            self.parse(text)
        return caught.exception.param, caught.exception.code

    def test_q_and_a_position(self):
        self.assertEqual(self.parse("q=fa"), ("fa", None, None))
        self.assertEqual(self.parse("q=fade&latitude=25.2&longitude=55.27"), ("fade", 25.2, 55.27))

    def test_bad_values(self):
        self.assertEqual(self.refused(""), ("q", "required"))
        self.assertEqual(self.refused("q=%20%20"), ("q", "required"))
        self.assertEqual(self.refused("q=a"), ("q", "too_short"))
        self.assertEqual(self.refused("q=" + "x" * 101), ("q", "invalid"))
        self.assertEqual(self.refused("q=fade&latitude=25.2"), ("longitude", "required"))


class RankTests(SimpleTestCase):
    def test_starts_then_a_word_starts_then_inside(self):
        self.assertEqual(search_suggest.rank("Fade Studio", "fa"), 0)
        self.assertEqual(search_suggest.rank("Skin Fade", "FA"), 1)
        self.assertEqual(search_suggest.rank("Bob Haircut", "air"), 2)
        self.assertEqual(search_suggest.rank(None, "fa"), 2)


class QueryTests(SimpleTestCase):
    """The queries, built not run: the same rules as the screens they lead to."""

    def test_salons_are_found_as_discover_search_finds_them_names_only(self):
        qs = search_suggest.salon_query("iron")
        self.assertEqual(
            set(qs.query.values_select) | set(qs.query.annotation_select),
            {"id", "published_name", "branch_name", "logo_url"},
        )
        sql = str(qs.query)
        self.assertIn("PUBLIC", sql)
        self.assertNotIn("gallery", sql.lower().split("where")[0])

    def test_with_a_position_the_distance_too(self):
        qs = search_suggest.salon_query("iron", 25.2, 55.27)
        self.assertIn("distance_km", qs.query.annotation_select)

    def test_stylists_at_a_public_salon_by_full_name(self):
        qs = search_suggest.stylist_query("omar k")
        exists = [c for c in qs.query.where.children if isinstance(getattr(c, "lhs", None), Exists)]
        self.assertGreaterEqual(len(exists), 2)  # a live login, and a public salon at their branch
        self.assertIn("full_name", qs.query.annotations)
        self.assertEqual(
            set(qs.query.values_select) | set(qs.query.annotation_select),
            {"id", "branch_id", "first_name", "last_name", "avatar_url"},
        )
        sql = str(qs.query)
        self.assertIn("employment_status", sql)
        self.assertIn("PUBLIC", sql)


class SalonsTests(SimpleTestCase):
    def test_best_match_then_nearest_at_most_five(self):
        rows = [
            {"id": uuid.UUID(int=i), "published_name": name, "branch_name": "B", "logo_url": None, "distance_km": km}
            for i, (name, km) in enumerate([
                ("The Iron Razor", 1.0), ("Iron Lounge", 9.0), ("Iron Bar", 2.0),
                ("Iron Six", 3.0), ("Iron Seven", 4.0), ("Iron Eight", 5.0),
            ])
        ]
        with mock.patch.object(search_suggest, "salon_query", return_value=Rows(rows)):
            found = search_suggest.salons("iron", 25.2, 55.27)
        self.assertEqual(
            [s["name"] for s in found], ["Iron Bar", "Iron Six", "Iron Seven", "Iron Eight", "Iron Lounge"]
        )
        self.assertEqual(found[0]["distance"], "2.0 km")

    def test_the_branch_name_when_nothing_was_published(self):
        rows = [{"id": SALON_A, "published_name": None, "branch_name": "Al Quoz", "logo_url": "l.png"}]
        with mock.patch.object(search_suggest, "salon_query", return_value=Rows(rows)):
            self.assertEqual(search_suggest.salons("quo"), [
                {"id": str(SALON_A), "name": "Al Quoz", "logo_url": "l.png", "distance": None},
            ])


class ServicesTests(SimpleTestCase):
    def call(self, storefront_tenants, service_rows, q="fade"):
        with mock.patch.object(search_suggest.Storefront, "objects") as storefronts, \
                mock.patch.object(search_suggest.Service, "objects") as services:
            storefronts.filter.return_value.values_list.return_value = storefront_tenants
            services.filter.return_value = Rows(service_rows)
            found = search_suggest.services(q)
        self.services = services
        return found

    def test_one_per_name_with_how_many_salons_offer_it(self):
        found = self.call(
            [TENANT_A, TENANT_A, TENANT_B],  # tenant A has two public salons
            [("Skin Fade", TENANT_A), ("skin  fade", TENANT_B), ("Fade", TENANT_B), ("Bob", TENANT_A)],
        )
        self.assertEqual(found, [{"name": "Fade", "salons": 1}, {"name": "Skin Fade", "salons": 3}, {"name": "Bob", "salons": 2}])

    def test_the_menu_rules_and_the_name(self):
        self.call([TENANT_A], [])
        kwargs = self.services.filter.call_args.kwargs
        self.assertEqual(
            (kwargs["status"], kwargs["deleted_at__isnull"], kwargs["online_booking_enabled"], kwargs["name__icontains"]),
            ("PUBLISHED", True, True, "fade"),
        )

    def test_no_public_salon_reads_no_service(self):
        self.assertEqual(self.call([], []), [])
        self.services.filter.assert_not_called()


class StylistsTests(SimpleTestCase):
    def test_each_with_their_salon_best_match_first(self):
        stylists = [
            {"id": uuid.UUID(int=1), "branch_id": BRANCH_A, "first_name": "Sam", "last_name": "Omar", "avatar_url": None},
            {"id": uuid.UUID(int=2), "branch_id": BRANCH_A, "first_name": "Omar", "last_name": "Khan", "avatar_url": "a.jpg"},
            {"id": uuid.UUID(int=3), "branch_id": uuid.uuid4(), "first_name": "Omar", "last_name": "Gone", "avatar_url": None},
        ]
        salons = [{"id": SALON_A, "branch_id": BRANCH_A, "published_name": "Del Capo", "branch_name": "B"}]
        with mock.patch.object(search_suggest, "stylist_query", return_value=Rows(stylists)), \
                mock.patch.object(search_suggest, "with_published_card_fields", return_value=Rows(salons)), \
                mock.patch.object(search_suggest, "discoverable_salons"):
            found = search_suggest.stylists("omar")
        self.assertEqual(found, [
            {"id": str(uuid.UUID(int=2)), "name": "Omar Khan", "photo_url": "a.jpg",
             "salon": {"id": str(SALON_A), "name": "Del Capo"}},
            {"id": str(uuid.UUID(int=1)), "name": "Sam Omar", "photo_url": None,
             "salon": {"id": str(SALON_A), "name": "Del Capo"}},
        ])

    def test_nobody_found_reads_no_salon(self):
        with mock.patch.object(search_suggest, "stylist_query", return_value=Rows([])), \
                mock.patch.object(search_suggest, "with_published_card_fields") as cards:
            self.assertEqual(search_suggest.stylists("zz"), [])
        cards.assert_not_called()


class ViewTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_off_it_is_404(self):
        self.assertEqual(self.client.get(f"{URL}?q=fade").status_code, 404)

    @override_settings(SEARCH_SUGGEST_V1=True)
    def test_on_the_three_lists(self):
        body = {"salons": [], "services": [{"name": "Fade", "salons": 2}], "stylists": []}
        with mock.patch.object(search_suggest, "suggest", return_value=body) as suggest:
            response = self.client.get(f"{URL}?q=fa&latitude=25.2&longitude=55.27")
        self.assertEqual((response.status_code, response.json()), (200, body))
        suggest.assert_called_once_with("fa", 25.2, 55.27)

    @override_settings(SEARCH_SUGGEST_V1=True)
    def test_a_short_q_is_the_usual_422(self):
        response = self.client.get(f"{URL}?q=f")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["errors"][0], {
            "field": "q", "code": "too_short", "message": "Send at least 2 characters.",
        })

    @override_settings(SEARCH_SUGGEST_V1=True)
    def test_throttled_per_address(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"search_suggest": "2/min"}), \
                mock.patch.object(search_suggest, "suggest", return_value={}):
            codes = [self.client.get(f"{URL}?q=fade").status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_the_rate_is_set(self):
        self.assertIn("search_suggest", ScopedRateThrottle.THROTTLE_RATES)

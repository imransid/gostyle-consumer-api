"""
The Search Results tabs (SEARCH_RESULTS_V1, docs/SEARCH_RESULTS_API.md):
GET /search/salons and GET /search/services.

The querysets are built and inspected, never run (the platform's tables are
unmanaged, so they do not exist in the test database). The views are called
through their real URLs with search_results' readers mocked. The rules run
for real.
"""

import types
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.db.models import Exists
from django.http import QueryDict
from django.test import SimpleTestCase, override_settings

from apps.salons import map_availability, map_filters, search_results, search_views
from apps.salons.params import ParamError
from apps.salons.search_results import SALON_SORTS, SERVICE_SORTS, SearchQuery, parse_search

SALON_A = uuid.UUID("33333333-3333-3333-3333-3333333333a1")
SALON_B = uuid.UUID("33333333-3333-3333-3333-3333333333b2")
TENANT_A = uuid.UUID("11111111-1111-1111-1111-1111111111a0")
TENANT_B = uuid.UUID("11111111-1111-1111-1111-1111111111b0")
BRANCH_A = uuid.UUID("22222222-2222-2222-2222-2222222222a1")
BRANCH_B = uuid.UUID("22222222-2222-2222-2222-2222222222b1")
CUT = uuid.UUID("66666666-6666-6666-6666-666666666601")
FADE = uuid.UUID("66666666-6666-6666-6666-666666666602")
HAIR = map_filters.category_option_id("Hair")
CATALOG = {HAIR: {"label": "Hair", "category_ids": frozenset({uuid.UUID(int=7)}), "salons": 2}}
NOW = datetime(2026, 11, 4, 8, 0, tzinfo=dt_timezone.utc)
SALONS_URL = "/api/v1/search/salons"
SERVICES_URL = "/api/v1/search/services"


def salons_query(text=""):
    return parse_search(QueryDict(text), SALON_SORTS, "rating")


def services_query(text=""):
    return parse_search(QueryDict(text), SERVICE_SORTS, "popular")


def refused(text, sorts=SALON_SORTS):
    with SimpleTestCase().assertRaises(ParamError) as caught:
        parse_search(QueryDict(text), sorts, "rating")
    return caught.exception.param, caught.exception.code


def plain_lookups(qs):
    """(lookup, value) of the plain WHERE conditions, Exists ones left out."""
    return [
        (cond.lookup_name, cond.rhs) for cond in qs.query.where.children
        if hasattr(cond, "lookup_name") and not isinstance(getattr(cond, "lhs", None), Exists)
    ]


def build(text="", **kwargs):
    with mock.patch.object(map_filters, "category_catalog", return_value=CATALOG), \
            mock.patch.object(search_results, "public_categories", return_value={}):
        return search_results.salon_queryset(salons_query(text), **kwargs)


class ParseTests(SimpleTestCase):
    """parse_search: the params of both tabs."""

    def test_nothing_sent(self):
        self.assertEqual(salons_query(), SearchQuery(sort="rating", page_size=20))
        self.assertEqual(services_query().sort, "popular")

    def test_with_a_position_the_nearest_first_unless_a_sort_is_sent(self):
        self.assertEqual(salons_query("latitude=25.2&longitude=55.27").sort, "distance")
        self.assertEqual(services_query("latitude=25.2&longitude=55.27").sort, "distance")
        self.assertEqual(salons_query("latitude=25.2&longitude=55.27&sort=rating").sort, "rating")

    def test_every_filter_read(self):
        query = salons_query(
            "q=fade&latitude=25.2&longitude=55.27&tab=near_me&gender=Gents"
            f"&service_categories={HAIR},gone&rating=4&availability=today&distance=10"
            "&additional=open_now&amenities=parking,packages&sort=popular&pageSize=5"
        )
        self.assertEqual(query, SearchQuery(
            q="fade", latitude=25.2, longitude=55.27, tab="near_me", gender="gents",
            service_categories=(HAIR, "gone"), rating="4", availability="today", distance_km=10.0,
            open_now=True, amenities=("parking", "packages"), sort="popular", page_size=5,
        ))

    def test_all_and_any_are_no_filter(self):
        query = salons_query("gender=all&tab=any&rating=any&availability=any&distance=any")
        self.assertEqual(query, SearchQuery(sort="rating"))

    def test_the_rating_ids_are_the_drawer_s_with_its_switch(self):
        self.assertEqual(salons_query("rating=4.5").rating, "4.5")
        self.assertEqual(refused("rating=2"), ("rating", "invalid_choice"))
        with self.settings(MAP_RATING_FIGMA_V1=True):
            self.assertEqual(salons_query("rating=2").rating, "2")
            self.assertEqual(refused("rating=4.5"), ("rating", "invalid_choice"))

    def test_what_measures_from_the_customer_needs_the_position(self):
        self.assertEqual(refused("distance=10"), ("distance", "required"))
        self.assertEqual(refused("tab=near_me"), ("tab", "required"))
        self.assertEqual(refused("sort=distance"), ("sort", "required"))
        self.assertEqual(salons_query("tab=packages").tab, "packages")

    def test_bad_values(self):
        self.assertEqual(refused("latitude=25.2"), ("longitude", "required"))
        self.assertEqual(refused("latitude=91&longitude=55"), ("latitude", "invalid"))
        self.assertEqual(refused("q=" + "x" * 101), ("q", "invalid"))
        self.assertEqual(refused("gender=kids"), ("gender", "invalid"))
        self.assertEqual(refused("tab=mall"), ("tab", "invalid_choice"))
        self.assertEqual(refused("amenities=metro"), ("amenities", "invalid_choice"))
        self.assertEqual(refused("additional=verified"), ("additional", "invalid_choice"))
        self.assertEqual(refused("sort=price"), ("sort", "invalid"))
        self.assertEqual(refused("pageSize=51"), ("pageSize", "invalid"))
        self.assertEqual(services_query("sort=price").sort, "price")


class SalonQueryTests(SimpleTestCase):
    """salon_queryset: the SQL half, built and inspected, not run."""

    def test_nothing_sent_is_the_discover_card_query(self):
        qs, picked = build()
        self.assertIsNone(picked)
        self.assertNotIn("distance_km", qs.query.annotations)
        for name in ("published_name", "logo_url", "avg_rating", "gallery_urls"):
            self.assertIn(name, qs.query.annotations)

    def test_a_position_adds_the_distance_and_drops_salons_without_a_pin(self):
        qs, _ = build("latitude=25.2&longitude=55.27")
        self.assertIn("distance_km", qs.query.annotations)
        self.assertIn(("isnull", False), plain_lookups(qs))

    def test_distance_is_a_radius_around_the_customer(self):
        qs, _ = build("latitude=25.2&longitude=55.27&distance=10")
        self.assertIn(("lte", 10.0), plain_lookups(qs))

    def test_gender_is_strict(self):
        qs, _ = build("gender=gents")
        self.assertIn("salon_category", qs.query.annotations)
        self.assertNotIn("salon_category", build("gender=all")[0].query.annotations)

    def test_rating_is_the_plain_average_at_least_the_pick(self):
        qs, _ = build("rating=4")
        self.assertIn(("gte", 4.0), plain_lookups(qs))

    def test_parking_is_the_amenity_or_the_badge(self):
        qs, _ = build("amenities=parking")
        self.assertIn("lists_parking", qs.query.annotations)
        self.assertIn("shows_free_parking", qs.query.annotations)
        sql = str(qs.query)
        self.assertIn("AMENITIES", sql)
        self.assertIn("manualBadges", sql)

    def test_free_parking_is_the_badge_only(self):
        qs, _ = build("amenities=free_parking")
        self.assertNotIn("lists_parking", qs.query.annotations)
        sql = str(qs.query)
        self.assertIn("manualBadges", sql)
        self.assertNotIn('"AMENITIES"', sql)

    def test_service_categories_known_ones_only(self):
        qs, picked = build(f"service_categories={HAIR},gone")
        self.assertEqual(picked, [HAIR])
        self.assertTrue(any(isinstance(getattr(c, "lhs", None), Exists) for c in qs.query.where.children))
        qs, picked = build("service_categories=gone")
        self.assertIsNone(picked)

    def test_the_text_finds_the_salon_by_name_city_or_a_service_on_its_menu(self):
        qs, _ = build("q=fade")
        self.assertIn("offers_text", qs.query.annotations)
        sql = str(qs.query)
        self.assertIn("tags", sql)

    def test_without_the_text_half_the_text_is_not_read(self):
        qs, _ = build("q=fade", with_text=False)
        self.assertNotIn("offers_text", qs.query.annotations)

    def test_service_text_is_name_label_or_tag(self):
        categories = {uuid.UUID(int=9): {"id": uuid.UUID(int=9), "name_en": "Fades", "parent_id": None, "icon": None}}
        found = search_results.service_text("fade", categories)
        names = {child[0] for child in found.children}
        self.assertEqual(names, {"name__icontains", "category_0_id__in", "tags__icontains"})


def card_row(pk, open_now=True):
    return types.SimpleNamespace(pk=pk, open_now=open_now)


class KeepTests(SimpleTestCase):
    """keep: open now, packages, then availability, each only when asked."""

    def setUp(self):
        patcher = mock.patch.object(
            search_results.SalonCardSerializer, "get_open", lambda self, row: row.open_now
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nothing_asked_keeps_every_row(self):
        rows = [card_row(SALON_A), card_row(SALON_B)]
        self.assertEqual(search_results.keep(rows, salons_query(), None, NOW), rows)
        self.assertFalse(search_results.needs_python(salons_query()))

    def test_open_now(self):
        rows = [card_row(SALON_A), card_row(SALON_B, open_now=False)]
        kept = search_results.keep(rows, salons_query("additional=open_now"), None, NOW)
        self.assertEqual([r.pk for r in kept], [SALON_A])

    def test_packages_by_the_tab_or_the_amenity(self):
        rows = [card_row(SALON_A), card_row(SALON_B)]
        with mock.patch.object(search_results, "tab_has_packages", side_effect=lambda r: r.pk == SALON_B):
            for text in ("tab=packages", "amenities=packages"):
                kept = search_results.keep(rows, salons_query(text), None, NOW)
                self.assertEqual([r.pk for r in kept], [SALON_B], text)

    def test_availability_last_on_what_is_left_with_the_picked_categories(self):
        rows = [card_row(SALON_A), card_row(SALON_B, open_now=False)]
        with mock.patch.object(map_availability, "available_ids", return_value={SALON_A}) as ids:
            kept = search_results.keep(
                rows, salons_query("additional=open_now&availability=today"), [HAIR], NOW
            )
        self.assertEqual([r.pk for r in kept], [SALON_A])
        asked_rows, window, picked, now = ids.call_args.args
        self.assertEqual(([r.pk for r in asked_rows], window, picked, now), ([SALON_A], "today", [HAIR], NOW))

    def test_no_row_left_does_not_read_who_is_busy(self):
        with mock.patch.object(map_availability, "available_ids") as ids:
            search_results.keep([], salons_query("availability=week"), None, NOW)
        ids.assert_not_called()


class PackagesRuleTests(SimpleTestCase):
    """tab_has_packages: the Packages tab's own rules, by its switch (D14)."""

    def test_off_the_tab_s_old_list(self):
        salon = object()
        with mock.patch.object(search_results, "salon_packages") as packages:
            packages.return_value.exists.return_value = True
            self.assertTrue(search_results.tab_has_packages(salon))
        packages.assert_called_once_with(salon)

    @override_settings(PACKAGES_LIVE_RULES_V1=True)
    def test_on_the_tab_s_live_bundles(self):
        from apps.salons import views

        with mock.patch.object(views.SalonPackagesView, "live_bundles", return_value=[]) as bundles:
            self.assertFalse(search_results.tab_has_packages("salon"))
        bundles.assert_called_once_with("salon")


class SalonOrderTests(SimpleTestCase):
    def test_the_orders(self):
        self.assertEqual(search_results.salon_ordering(salons_query("latitude=1&longitude=1")), ("distance_km", "id"))
        rating = search_results.salon_ordering(salons_query())
        self.assertEqual((rating[1], rating[2]), ("-review_count", "id"))
        popular = search_results.salon_ordering(salons_query("latitude=1&longitude=1&sort=popular"))
        self.assertEqual(popular[1:], ("-review_count", "distance_km", "id"))

    def test_popular_reads_the_weighted_rating(self):
        with mock.patch.object(map_filters, "category_catalog", return_value=CATALOG), \
                mock.patch.object(search_results, "average_salon_rating", return_value=4.2):
            qs = search_results.salons(salons_query("sort=popular"))
        self.assertIn("popular_score", qs.query.annotations)


def salon_dict(salon_id, tenant, branch, rating=None, km=None, name="Del Capo"):
    return {
        "id": salon_id, "tenant_id": tenant, "branch_id": branch, "published_name": name,
        "branch_name": "Branch", "category": "male", "avg_rating": rating, "review_count": 0,
        "logo_url": f"https://cdn.example/{salon_id}.png", "distance_km": km,
    }


def service_dict(service_id, tenant, name, price_minor=10000, days_old=10, tags=()):
    return {
        "id": service_id, "tenant_id": tenant, "name": name, "description": None,
        "price_minor": price_minor, "currency": "AED", "duration_minutes": 30,
        "category_0_id": None, "created_at": NOW - timedelta(days=days_old), "tags": list(tags),
    }


A = salon_dict(SALON_A, TENANT_A, BRANCH_A, rating=4.8, km=3.1)
B = salon_dict(SALON_B, TENANT_B, BRANCH_B, rating=None, km=1.2, name="Lush Glow")


def row(salon, service, branch_price_minor=None):
    return types.SimpleNamespace(
        salon=salon, service=service, branch_price_minor=branch_price_minor, branch_currency=None
    )


class ServiceRuleTests(SimpleTestCase):
    """The Services tab: matching, order, the result."""

    def test_a_tag_finds_a_service_as_its_name_does(self):
        tagged = row(A, service_dict(CUT, TENANT_A, "Classic Cut", tags=["Skin Fade"]))
        self.assertTrue(search_results.service_matches(tagged, {}, "fade"))
        self.assertTrue(search_results.service_matches(row(A, service_dict(FADE, TENANT_A, "Fade")), {}, "FADE"))
        self.assertFalse(search_results.service_matches(row(A, service_dict(CUT, TENANT_A, "Cut")), {}, "fade"))
        as_text = row(A, {**service_dict(CUT, TENANT_A, "Cut"), "tags": "{skin fade}"})
        self.assertTrue(search_results.service_matches(as_text, {}, "fade"))

    def rows(self):
        return [
            row(A, service_dict(CUT, TENANT_A, "Cut", price_minor=20000, days_old=1)),
            row(A, service_dict(FADE, TENANT_A, "Fade", price_minor=9000, days_old=5)),
            row(B, service_dict(CUT, TENANT_B, "Cut", price_minor=15000, days_old=3), branch_price_minor=5000),
        ]

    def names(self, rows):
        return [(r.salon["published_name"], r.service["name"]) for r in rows]

    def test_distance_the_salon_s_nearest_first(self):
        found = search_results.order_services(self.rows(), services_query("latitude=1&longitude=1"), 1)
        self.assertEqual(self.names(found)[0], ("Lush Glow", "Cut"))

    def test_rating_the_salon_s_best_first_unrated_last(self):
        found = search_results.order_services(self.rows(), services_query("sort=rating"), 1)
        self.assertEqual(self.names(found), [("Del Capo", "Cut"), ("Del Capo", "Fade"), ("Lush Glow", "Cut")])

    def test_price_lowest_first_at_that_salon_s_price(self):
        found = search_results.order_services(self.rows(), services_query("sort=price"), 1)
        self.assertEqual(self.names(found), [("Lush Glow", "Cut"), ("Del Capo", "Fade"), ("Del Capo", "Cut")])

    def test_popular_most_booked_first_and_the_database_down_is_no_error(self):
        counts = {(BRANCH_A, FADE): 9}
        with mock.patch.object(search_results, "service_booking_counts", return_value=counts):
            found = search_results.order_services(self.rows(), services_query(), 1)
        self.assertEqual(self.names(found)[0], ("Del Capo", "Fade"))
        with mock.patch.object(
            search_results, "service_booking_counts",
            side_effect=search_results.BookingCountsUnavailable("down"),
        ):
            self.assertEqual(len(search_results.order_services(self.rows(), services_query(), 1)), 3)

    def test_the_result(self):
        images = {(SALON_A, CUT): ["https://cdn.example/1.jpg"]}
        found = search_results.service_result(
            row(A, service_dict(CUT, TENANT_A, "Cut", price_minor=19900)), {}, images
        )
        self.assertEqual(
            list(found),
            ["id", "name", "description", "price", "currency", "duration_min", "duration_max",
             "images", "category", "salon"],
        )
        self.assertEqual((found["price"], found["currency"]), (199, "AED"))
        self.assertEqual(found["images"], ["https://cdn.example/1.jpg"])
        self.assertEqual(found["salon"], {
            "id": str(SALON_A), "name": "Del Capo", "logo_url": f"https://cdn.example/{SALON_A}.png",
            "category": "gents", "distance": "3.1 km", "rating": "4.8",
        })

    def test_no_photo_no_rating_no_distance(self):
        found = search_results.service_result(row(B, service_dict(CUT, TENANT_B, "Cut")), {}, {})
        self.assertEqual(found["images"], [])
        self.assertIsNone(found["salon"]["rating"])
        self.assertEqual(found["salon"]["distance"], "1.2 km")


class ImagesTests(SimpleTestCase):
    """images_for: own photos first (the salon's business only), then the linked gallery, 4 at most."""

    def test_two_queries_for_a_page(self):
        rows = [row(A, service_dict(CUT, TENANT_A, "Cut"))]
        with mock.patch.object(search_results.ServiceMedia, "objects") as own, \
                mock.patch.object(search_results.StorefrontMedia, "objects") as linked:
            own.filter.return_value.order_by.return_value.values_list.return_value = [
                (CUT, TENANT_A, "own-1"), (CUT, TENANT_B, "someone-else"), (CUT, TENANT_A, "own-2"),
            ]
            linked.filter.return_value.order_by.return_value.values_list.return_value = [
                (SALON_A, CUT, f"gallery-{i}") for i in range(5)
            ]
            found = search_results.images_for(rows)
        self.assertEqual(found[(SALON_A, CUT)], ["own-1", "own-2", "gallery-0", "gallery-1"])
        self.assertEqual(linked.filter.call_args.kwargs["kind"], "GALLERY")
        self.assertEqual(linked.filter.call_args.kwargs["moderation_status"], "APPROVED")

    def test_an_empty_page_reads_nothing(self):
        with mock.patch.object(search_results.ServiceMedia, "objects") as own:
            self.assertEqual(search_results.images_for([]), {})
        own.filter.assert_not_called()


class SwitchOffTests(SimpleTestCase):
    def test_both_routes_are_404(self):
        self.assertEqual(self.client.get(SALONS_URL).status_code, 404)
        self.assertEqual(self.client.get(SERVICES_URL).status_code, 404)


class _Card:
    """A stand-in for the card serializer in the view tests."""

    def __init__(self, rows, many=True, context=None):
        self.data = [{"id": str(r.pk)} for r in rows]


@override_settings(SEARCH_RESULTS_V1=True)
class SalonsViewTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch.object(search_views, "SalonCardSerializer", _Card)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_page_shape_of_discover(self):
        rows = [card_row(uuid.UUID(int=i)) for i in range(3)]
        with mock.patch.object(search_results, "salons", return_value=rows) as salons:
            body = self.client.get(f"{SALONS_URL}?pageSize=2&q=fade").json()
        self.assertEqual(set(body), {"count", "next", "previous", "results"})
        self.assertEqual((body["count"], len(body["results"])), (3, 2))
        self.assertIn("pageSize=2", body["next"])
        query = salons.call_args.args[0]
        self.assertEqual((query.q, query.page_size), ("fade", 2))
        self.assertEqual(salons.call_args.kwargs["now"].tzinfo, dt_timezone.utc)

    def test_a_drawer_id_not_offered_is_the_map_s_400(self):
        for text, field in (("rating=7", "rating"), ("amenities=metro", "amenities"), ("distance=10", "distance")):
            with self.subTest(text=text):
                response = self.client.get(f"{SALONS_URL}?{text}")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["errors"][0]["field"], field)

    def test_another_param_is_the_usual_422(self):
        for text, field in (("latitude=25", "longitude"), ("sort=distance", "sort"), ("pageSize=0", "pageSize")):
            with self.subTest(text=text):
                response = self.client.get(f"{SALONS_URL}?{text}")
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["errors"][0]["field"], field)

    def test_availability_down_is_503(self):
        with mock.patch.object(
            search_results, "salons", side_effect=map_availability.AvailabilityUnavailable("down")
        ):
            response = self.client.get(f"{SALONS_URL}?availability=today")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        # The map's 503, in the same envelope.
        self.assertEqual(body["code"], "service_unavailable")
        self.assertEqual(body["errors"][0]["code"], "availability_unavailable")


@override_settings(SEARCH_RESULTS_V1=True)
class ServicesViewTests(SimpleTestCase):
    def call(self, text=""):
        rows = [
            row(A, service_dict(CUT, TENANT_A, "Cut", price_minor=20000)),
            row(B, service_dict(CUT, TENANT_B, "Cut", price_minor=15000)),
            row(A, service_dict(FADE, TENANT_A, "Fade", price_minor=9000)),
        ]
        with mock.patch.object(search_results, "service_list", return_value=(rows, {})), \
                mock.patch.object(search_results, "images_for", return_value={}) as images:
            body = self.client.get(f"{SERVICES_URL}?{text}").json()
        self.images = images
        return body

    def test_price_order_paged_and_the_images_read_for_the_page_only(self):
        body = self.call("sort=price&pageSize=2")
        self.assertEqual(body["count"], 3)
        self.assertEqual([r["price"] for r in body["results"]], [90, 150])
        (page,) = self.images.call_args.args
        self.assertEqual(len(page), 2)

    def test_each_result_has_the_tab_s_fields(self):
        body = self.call("sort=price&pageSize=1")
        self.assertEqual(
            set(body["results"][0]),
            {"id", "name", "description", "price", "currency", "duration_min", "duration_max",
             "images", "category", "salon"},
        )
        self.assertEqual(
            set(body["results"][0]["salon"]), {"id", "name", "logo_url", "category", "distance", "rating"}
        )

    def test_price_is_a_services_sort_only(self):
        self.assertEqual(self.client.get(f"{SALONS_URL}?sort=price").status_code, 422)

"""
The search over the map (SEARCH_POPULAR_V1, docs/SEARCH_POPULAR_API.md): GET
/services across salons and is_popular on GET /discover, as the mobile team's
doc asks (docs/search-popular-fe-contract.md).

The platform's tables are unmanaged, so they do not exist in the test
database: the querysets are built and inspected, never run, and the views are
called with their selectors mocked. The rules (service_search.py) are pure and
run for real. The SQL itself was run on a local database (the guide's
"Checked" section).
"""

import types
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.conf import settings
from django.db.models import F
from django.db.models.expressions import OrderBy
from django.http import QueryDict
from django.test import SimpleTestCase, override_settings
from drf_spectacular.generators import SchemaGenerator
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from apps.salons import map_availability, selectors, service_search, service_search_views, views
from apps.salons.params import (
    ParamError,
    asks_for_one_menu,
    parse_discovery,
    parse_service_search,
)

SERVICES_URL = "/api/v1/services"

TENANT_A = uuid.UUID("11111111-1111-1111-1111-1111111111a0")
TENANT_B = uuid.UUID("11111111-1111-1111-1111-1111111111b0")
BRANCH_A1 = uuid.UUID("22222222-2222-2222-2222-2222222222a1")
BRANCH_A2 = uuid.UUID("22222222-2222-2222-2222-2222222222a2")
BRANCH_B = uuid.UUID("22222222-2222-2222-2222-2222222222b0")
SALON_A1 = uuid.UUID("33333333-3333-3333-3333-3333333333a1")
SALON_A2 = uuid.UUID("33333333-3333-3333-3333-3333333333a2")
SALON_B = uuid.UUID("33333333-3333-3333-3333-3333333333b0")

COLOUR = uuid.UUID("55555555-5555-5555-5555-5555555555c0")
HIGHLIGHTS = uuid.UUID("55555555-5555-5555-5555-5555555555c1")
CUTS = uuid.UUID("55555555-5555-5555-5555-5555555555d0")
B_CUTS = uuid.UUID("55555555-5555-5555-5555-5555555555e0")
DELETED = uuid.UUID("55555555-5555-5555-5555-5555555555ff")

ROOT = uuid.UUID("66666666-6666-6666-6666-6666666666a1")
BALAYAGE = uuid.UUID("66666666-6666-6666-6666-6666666666a2")
GENTS = uuid.UUID("66666666-6666-6666-6666-6666666666a3")
FADE = uuid.UUID("66666666-6666-6666-6666-6666666666b1")

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=dt_timezone.utc)

CATEGORIES = {
    COLOUR: {"id": COLOUR, "name_en": "Hair Colour", "parent_id": None, "icon": "drop"},
    HIGHLIGHTS: {"id": HIGHLIGHTS, "name_en": "Highlights", "parent_id": COLOUR, "icon": None},
    CUTS: {"id": CUTS, "name_en": "Haircut & Styling", "parent_id": None, "icon": "scissors"},
    B_CUTS: {"id": B_CUTS, "name_en": "Cuts", "parent_id": None, "icon": None},
}


def q(text):
    return QueryDict(text)


def salon(salon_id, tenant, branch, name="Iron Razor", branch_name="Al Quoz Branch",
          category="male", rating=None, reviews=0):
    return {
        "id": salon_id,
        "tenant_id": tenant,
        "branch_id": branch,
        "published_name": name,
        "branch_name": branch_name,
        "category": category,
        "avg_rating": rating,
        "review_count": reviews,
    }


def service(service_id, tenant, name, category=None, days_old=10, price_minor=10000,
            description=None, minutes=30):
    return {
        "id": service_id,
        "tenant_id": tenant,
        "name": name,
        "description": description,
        "price_minor": price_minor,
        "duration_minutes": minutes,
        "category_0_id": category,
        "created_at": NOW - timedelta(days=days_old),
    }


A1 = salon(SALON_A1, TENANT_A, BRANCH_A1, name="Del Capo Barber", rating=4.8, reviews=40)
A2 = salon(SALON_A2, TENANT_A, BRANCH_A2, name=None, branch_name="Del Capo Marina",
           rating=4.0, reviews=2)
B = salon(SALON_B, TENANT_B, BRANCH_B, name="Colour Bar", category="Female")

MENU = [
    service(ROOT, TENANT_A, "Root Colour", None, days_old=3, price_minor=25000),
    service(BALAYAGE, TENANT_A, "Balayage", HIGHLIGHTS, days_old=20, price_minor=45000),
    service(GENTS, TENANT_A, "The Gentleman's Cut", CUTS, days_old=1, price_minor=18000,
            description="A classic cut, no colour.", minutes=20),
    service(FADE, TENANT_B, "Skin Fade", B_CUTS, days_old=5, price_minor=9000),
]

# Salon A2 set its own price for the gentleman's cut; A1 did not.
BRANCH_ROWS = {
    (GENTS, BRANCH_A2): {"service_id": GENTS, "branch_id": BRANCH_A2,
                         "price_minor": 20000, "available": True},
    (ROOT, BRANCH_A1): {"service_id": ROOT, "branch_id": BRANCH_A1,
                        "price_minor": None, "available": True},
}


def rows_of(salons=(A1, A2, B), services=MENU, branch_rows=BRANCH_ROWS):
    return service_search.listing(list(salons), list(services), branch_rows)


def ids(rows):
    return [(row.salon["id"], row.service["id"]) for row in rows]


# ---------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------


class ServiceSearchParamsTests(SimpleTestCase):
    """parse_service_search: the doc's 2.1 params and its section 5 errors."""

    def error(self, text):
        with self.assertRaises(ParamError) as caught:
            parse_service_search(q(text))
        return caught.exception.param

    def test_nothing_sent_is_the_newest_twenty_everywhere(self):
        self.assertEqual(parse_service_search(q("")), {
            "is_popular": False,
            "search": None,
            "latitude": None,
            "longitude": None,
            "radius_km": None,
            "page_size": 20,
        })

    def test_everything_together(self):
        found = parse_service_search(q(
            "is_popular=true&search=fade&latitude=25.1972&longitude=55.2744"
            "&radius=500&page=1&pageSize=10"
        ))
        self.assertEqual(found, {
            "is_popular": True,
            "search": "fade",
            "latitude": 25.1972,
            "longitude": 55.2744,
            "radius_km": 500.0,
            "page_size": 10,
        })

    def test_is_popular_reads_every_boolean_spelling(self):
        for raw, wanted in (("true", True), ("1", True), ("YES", True), ("false", False),
                            ("0", False), ("", False)):
            with self.subTest(raw=raw):
                self.assertIs(parse_service_search(q(f"is_popular={raw}"))["is_popular"], wanted)

    def test_is_popular_that_is_no_boolean_is_an_error(self):
        self.assertEqual(self.error("is_popular=maybe"), "is_popular")

    def test_an_empty_search_is_the_same_as_none(self):
        self.assertIsNone(parse_service_search(q("search="))["search"])
        self.assertIsNone(parse_service_search(q("search=%20%20"))["search"])
        self.assertEqual(parse_service_search(q("search=%20fade%20"))["search"], "fade")

    def test_a_search_over_100_characters_is_an_error(self):
        self.assertEqual(self.error("search=" + "a" * 101), "search")

    def test_page_size_from_1_to_50(self):
        for raw in ("1", "20", "50"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_service_search(q(f"pageSize={raw}"))["page_size"], int(raw))

    def test_page_size_out_of_range_or_not_a_whole_number_is_an_error(self):
        for raw in ("0", "51", "100", "-1", "abc", "10.5", "1e2", "²", "١٠"):
            with self.subTest(raw=raw):
                self.assertEqual(self.error(f"pageSize={raw}"), "pageSize")

    def test_page_size_is_read_too_and_checked_the_same(self):
        self.assertEqual(parse_service_search(q("page_size=7"))["page_size"], 7)
        self.assertEqual(self.error("page_size=99"), "page_size")
        # The app's spelling wins.
        self.assertEqual(parse_service_search(q("pageSize=5&page_size=7"))["page_size"], 5)

    def test_half_a_position_is_an_error(self):
        self.assertEqual(self.error("latitude=25.19"), "longitude")
        self.assertEqual(self.error("longitude=55.27"), "latitude")

    def test_radius_as_on_discover(self):
        self.assertEqual(self.error("radius=5"), "radius")
        self.assertEqual(self.error("latitude=25&longitude=55&radius=0"), "radius")
        self.assertEqual(self.error("latitude=north&longitude=55"), "latitude")

    def test_the_old_coordinate_names_are_refused_as_on_discover(self):
        self.assertEqual(self.error("lat=25&lng=55"), "lat")


class OneMenuOrEverySalonTests(SimpleTestCase):
    """Which of the two answers GET /services gives."""

    def test_any_of_the_old_ids_is_the_old_menu_route(self):
        for text in (f"tenant_id={TENANT_A}", f"branch_id={BRANCH_A1}",
                     f"category_id={CUTS}", "tenant_id=nope"):
            with self.subTest(text=text):
                self.assertTrue(asks_for_one_menu(q(text)))

    def test_none_of_them_or_empty_is_the_list_across_salons(self):
        for text in ("", "is_popular=true", "search=fade", "tenant_id=", "branch_id=%20"):
            with self.subTest(text=text):
                self.assertFalse(asks_for_one_menu(q(text)))


class DiscoverParamsTests(SimpleTestCase):
    """parse_discovery's two new keys, read only with the switch on."""

    def test_off_neither_is_read(self):
        found = parse_discovery(q("is_popular=maybe&pageSize=999"))
        self.assertIs(found["is_popular"], False)
        self.assertIsNone(found["page_size"])

    def test_on_is_popular_and_page_size_are_read(self):
        found = parse_discovery(q("is_popular=true&pageSize=10"), search_popular=True)
        self.assertIs(found["is_popular"], True)
        self.assertEqual(found["page_size"], 10)
        plain = parse_discovery(q(""), search_popular=True)
        self.assertIs(plain["is_popular"], False)
        self.assertIsNone(plain["page_size"])

    def test_on_bad_values_are_errors(self):
        for text, field in (("is_popular=maybe", "is_popular"), ("pageSize=0", "pageSize"),
                            ("pageSize=51", "pageSize")):
            with self.subTest(text=text), self.assertRaises(ParamError) as caught:
                parse_discovery(q(text), search_popular=True)
            self.assertEqual(caught.exception.param, field)

    def test_the_old_page_size_is_not_read_here(self):
        # page_size keeps the paginator's old reading (cut to 50), switch on or off.
        self.assertIsNone(parse_discovery(q("page_size=999"), search_popular=True)["page_size"])

    def test_every_other_key_is_what_it_was(self):
        text = q("latitude=25.19&longitude=55.26&radius=5&category=gents&search=iron&sort=distance")
        off, on = parse_discovery(text), parse_discovery(text, search_popular=True)
        for found in (off, on):
            self.assertEqual(
                (found["latitude"], found["longitude"], found["radius_km"], found["category"],
                 found["search"], found["sort"]),
                (25.19, 55.26, 5.0, "gents", "iron", "distance"),
            )


# ---------------------------------------------------------------------------
# The rules (service_search.py)
# ---------------------------------------------------------------------------


class ListingTests(SimpleTestCase):
    """One result per salon and service of its tenant (doc 2.4)."""

    def test_each_salon_lists_its_own_tenant_s_services(self):
        self.assertEqual(sorted(ids(rows_of()), key=str), sorted([
            (SALON_A1, ROOT), (SALON_A1, BALAYAGE), (SALON_A1, GENTS),
            (SALON_A2, ROOT), (SALON_A2, BALAYAGE), (SALON_A2, GENTS),
            (SALON_B, FADE),
        ], key=str))

    def test_the_same_service_at_two_salons_has_each_salon_s_price(self):
        by = {(row.salon["id"], row.service["id"]): row for row in rows_of()}
        self.assertEqual(service_search.result(by[(SALON_A1, GENTS)], CATEGORIES)["price"], 180)
        self.assertEqual(service_search.result(by[(SALON_A2, GENTS)], CATEGORIES)["price"], 200)
        # A branch row with no price is the service's own price (menu.service_price).
        self.assertEqual(service_search.result(by[(SALON_A1, ROOT)], CATEGORIES)["price"], 250)

    def test_a_salon_with_no_services_lists_nothing(self):
        self.assertEqual(rows_of(salons=(B,), services=MENU[:3]), [])

    def test_branch_availability_is_the_tab_s_rule(self):
        # Off (as on the tab): a branch's `available` row is not read.
        no = {(GENTS, BRANCH_A1): {"price_minor": None, "available": False}}
        self.assertIn((SALON_A1, GENTS), ids(rows_of(branch_rows=no)))
        # On: only services with a true row, as salon_services filters.
        with mock.patch.object(service_search, "BRANCH_AVAILABILITY_ENABLED", True):
            kept = ids(rows_of(salons=(A1,), branch_rows=no | {
                (ROOT, BRANCH_A1): {"price_minor": None, "available": True},
            }))
        self.assertEqual(kept, [(SALON_A1, ROOT)])


class ResultTests(SimpleTestCase):
    """service_search.result: the doc's 2.2 fields, and only those."""

    def result(self, salon_row, service_row, branch_price_minor=None):
        row = types.SimpleNamespace(
            salon=salon_row, service=service_row, branch_price_minor=branch_price_minor
        )
        return service_search.result(row, CATEGORIES)

    def test_the_doc_s_fields(self):
        self.assertEqual(self.result(A1, MENU[2]), {
            "id": str(GENTS),
            "name": "The Gentleman's Cut",
            "description": "A classic cut, no colour.",
            "price": 180,
            "duration_min": 20,
            "duration_max": 20,
            "category": {"id": str(CUTS), "label": "Haircut & Styling"},
            "salon": {"id": str(SALON_A1), "name": "Del Capo Barber", "category": "gents"},
        })

    def test_the_same_row_as_the_services_tab(self):
        from apps.salons.menu import service_row

        svc = types.SimpleNamespace(
            id=GENTS, name="The Gentleman's Cut", description="A classic cut, no colour.",
            price_minor=18000, branch_price_minor=20000, duration_minutes=20,
        )
        found = self.result(A2, MENU[2], branch_price_minor=20000)
        for key, value in service_row(svc).items():
            self.assertEqual(found[key], value)

    def test_the_category_is_the_tab_s_chip_its_parent_first(self):
        self.assertEqual(self.result(A1, MENU[1])["category"],
                         {"id": str(COLOUR), "label": "Hair Colour"})

    def test_no_category_or_a_deleted_one_is_other_never_null(self):
        self.assertEqual(self.result(A1, MENU[0])["category"], {"id": "other", "label": "Other"})
        deleted = service(ROOT, TENANT_A, "Root Colour", DELETED)
        self.assertEqual(self.result(A1, deleted)["category"], {"id": "other", "label": "Other"})

    def test_the_salon_s_name_and_category_as_on_its_card(self):
        self.assertEqual(self.result(A2, MENU[2])["salon"]["name"], "Del Capo Marina")
        self.assertEqual(self.result(B, MENU[3])["salon"]["category"], "ladies")
        nobody = salon(SALON_B, TENANT_B, BRANCH_B, category=None)
        self.assertIsNone(self.result(nobody, MENU[3])["salon"]["category"])

    def test_a_service_with_no_description_is_null(self):
        self.assertIsNone(self.result(A1, MENU[0])["description"])


class SearchMatchTests(SimpleTestCase):
    """What search finds (doc 2.5)."""

    def found(self, term):
        rows = [row for row in rows_of(salons=(A1, B))
                if service_search.matches(row, CATEGORIES, term)]
        return {row.service["id"] for row in rows}

    def test_colour_finds_a_name_and_a_category_label(self):
        # "Root Colour" by its name, "Balayage" filed under "Hair Colour".
        self.assertEqual(self.found("colour"), {ROOT, BALAYAGE})

    def test_case_is_ignored_and_the_term_can_be_anywhere(self):
        self.assertEqual(self.found("COLOUR"), {ROOT, BALAYAGE})
        self.assertEqual(self.found("ntleman"), {GENTS})
        self.assertEqual(self.found("fade"), {FADE})

    def test_not_the_description_and_not_the_salon_s_name(self):
        # GENTS' description says "colour" and salon B is "Colour Bar".
        self.assertNotIn(GENTS, self.found("colour"))
        self.assertNotIn(FADE, self.found("colour"))
        self.assertEqual(self.found("del capo"), set())

    def test_the_label_is_the_chip_s_not_the_service_s_own_category(self):
        # Balayage's own category is "Highlights"; its category.label is "Hair Colour".
        self.assertEqual(self.found("highlights"), set())
        self.assertEqual(self.found("styling"), {GENTS})

    def test_other_is_the_label_of_a_service_with_no_category(self):
        self.assertEqual(self.found("other"), {ROOT})

    def test_label_ids_and_finds_other_say_what_the_query_must_keep(self):
        self.assertEqual(service_search.label_ids(CATEGORIES, "colour"), {COLOUR, HIGHLIGHTS})
        self.assertEqual(service_search.label_ids(CATEGORIES, "CUT"), {CUTS, B_CUTS})
        self.assertEqual(service_search.label_ids(CATEGORIES, "zzz"), set())
        self.assertTrue(service_search.finds_other("Oth"))
        self.assertFalse(service_search.finds_other("colour"))


class OrderTests(SimpleTestCase):
    """newest_first and popular_first (doc 2.3)."""

    def test_without_is_popular_the_newest_service_first(self):
        found = [sid for _, sid in ids(service_search.newest_first(rows_of(salons=(A1, B))))]
        self.assertEqual(found, [GENTS, ROOT, FADE, BALAYAGE])

    def test_one_service_at_two_salons_keeps_one_order_every_time(self):
        found = ids(service_search.newest_first(rows_of(salons=(A2, A1))))
        self.assertEqual(found[:2], [(SALON_A1, GENTS), (SALON_A2, GENTS)])

    def test_is_popular_the_most_booked_first(self):
        counts = {(BRANCH_B, FADE): 9, (BRANCH_A1, BALAYAGE): 4, (BRANCH_A1, ROOT): 1}
        found = ids(service_search.popular_first(rows_of(salons=(A1, B)), counts))
        self.assertEqual(found, [
            (SALON_B, FADE), (SALON_A1, BALAYAGE), (SALON_A1, ROOT), (SALON_A1, GENTS),
        ])

    def test_counts_belong_to_one_salon_s_branch(self):
        counts = {(BRANCH_A2, GENTS): 3}
        found = ids(service_search.popular_first(rows_of(salons=(A1, A2)), counts))
        self.assertEqual(found[0], (SALON_A2, GENTS))

    def test_ties_the_salon_s_rating_then_the_newest(self):
        rated = salon(SALON_B, TENANT_B, BRANCH_B, rating=4.9, reviews=1)
        nobody = salon(SALON_A1, TENANT_A, BRANCH_A1, rating=None)
        found = ids(service_search.popular_first(rows_of(salons=(nobody, rated)), {}))
        # 4.9 first; the salon nobody rated after it, its services newest first.
        self.assertEqual(found, [
            (SALON_B, FADE), (SALON_A1, GENTS), (SALON_A1, ROOT), (SALON_A1, BALAYAGE),
        ])

    def test_a_higher_rating_wins_a_tie(self):
        found = ids(service_search.popular_first(rows_of(salons=(A2, A1)), {}))
        self.assertEqual([salon_id for salon_id, _ in found[:3]], [SALON_A1] * 3)


class SpreadTests(SimpleTestCase):
    """At most 2 services of one salon on a page of is_popular (doc 2.3)."""

    @staticmethod
    def rows(salon_ids):
        return [
            types.SimpleNamespace(salon={"id": salon_id}, service={"id": index})
            for index, salon_id in enumerate(salon_ids)
        ]

    def pages(self, spread, size):
        return [spread[i:i + size] for i in range(0, len(spread), size)]

    def test_a_busy_salon_does_not_fill_the_row(self):
        rows = self.rows("AAAAABCD")
        spread = service_search.spread(rows, 4, pages=1)
        self.assertEqual([r.salon["id"] for r in spread[:4]], list("AABC"))

    def test_the_skipped_ones_lead_the_next_page(self):
        rows = self.rows("AAAAABCDEF")
        spread = service_search.spread(rows, 4, pages=3)
        self.assertEqual(
            ["".join(r.salon["id"] for r in page) for page in self.pages(spread, 4)],
            ["AABC", "AADE", "AF"],
        )

    def test_a_page_short_of_other_salons_is_filled_so_every_page_is_full(self):
        rows = self.rows("AAAAAB")
        spread = service_search.spread(rows, 4, pages=2)
        self.assertEqual(
            ["".join(r.salon["id"] for r in page) for page in self.pages(spread, 4)],
            ["AABA", "AA"],
        )

    def test_nothing_is_lost_or_doubled(self):
        rows = self.rows("AAABBBBCAAD" * 3)
        spread = service_search.spread(rows, 5, pages=10)
        self.assertEqual(sorted(id(r) for r in spread), sorted(id(r) for r in rows))

    def test_a_page_is_the_same_whichever_page_was_asked(self):
        rows = self.rows("AAAAABBBBBCCDDE")
        far = service_search.spread(rows, 3, pages=5)
        for pages in (1, 2, 3):
            with self.subTest(pages=pages):
                self.assertEqual(service_search.spread(rows, 3, pages)[:3 * pages], far[:3 * pages])

    def test_after_the_spread_pages_the_order_is_kept(self):
        rows = self.rows("AAAAB")
        self.assertEqual(service_search.spread(rows, 2, pages=0), rows)

    def test_every_spread_page_keeps_the_cap_while_others_are_left(self):
        rows = self.rows("AAAAAAABBBBBBBCCCCDE" * 2)
        spread = service_search.spread(rows, 6, pages=4)
        for page in self.pages(spread[:24], 6):
            taken = {salon_id: sum(r.salon["id"] == salon_id for r in page) for salon_id in "ABCDE"}
            self.assertLessEqual(max(taken.values()), 2, page)


class CountedStatusesTests(SimpleTestCase):
    """Which bookings count (doc 2.3: cancelled ones left out)."""

    def test_every_status_that_holds_a_chair_and_no_show(self):
        self.assertEqual(
            set(service_search.COUNTED_STATUSES),
            set(map_availability.BLOCKING_STATUSES) | {"no_show"},
        )

    def test_not_cancelled_or_never_booked(self):
        for status in ("cancelled", "expired", "skipped", "rescheduled", "draft", "held"):
            self.assertNotIn(status, service_search.COUNTED_STATUSES)

    def test_the_last_30_days(self):
        self.assertEqual(service_search.popular_since(NOW), NOW - timedelta(days=30))


# ---------------------------------------------------------------------------
# The queries, built and inspected
# ---------------------------------------------------------------------------


def lookups(where):
    """(field, lookup, value) of every plain condition, nested ones flattened."""
    found = []
    for child in where.children:
        if hasattr(child, "children"):
            found.extend(lookups(child))
        elif hasattr(child, "lookup_name"):
            target = getattr(child.lhs, "target", None)
            found.append((getattr(target, "attname", None), child.lookup_name, child.rhs))
    return found


class MenuServicesQueryTests(SimpleTestCase):
    """selectors.menu_services: the Services tab's rules, and the search's narrowing."""

    def test_the_tab_s_three_rules_for_these_tenants(self):
        found = lookups(selectors.menu_services([TENANT_A]).query.where)
        self.assertIn(("status", "exact", "PUBLISHED"), found)
        self.assertIn(("deleted_at", "isnull", True), found)
        self.assertIn(("online_booking_enabled", "exact", True), found)
        self.assertIn(("tenant_id", "in", [TENANT_A]), found)

    def test_no_tenant_reads_nothing(self):
        self.assertEqual(selectors.menu_services([]).query.is_empty(), True)

    def test_search_keeps_a_name_or_a_category(self):
        qs = selectors.menu_services([TENANT_A], term="colour", label_ids={COLOUR})
        found = lookups(qs.query.where)
        self.assertIn(("name", "icontains", "colour"), found)
        self.assertIn(("category_0_id", "in", [COLOUR]), found)
        self.assertNotIn(("category_0_id", "isnull", True), found)

    def test_a_term_in_other_keeps_the_services_with_no_live_category(self):
        qs = selectors.menu_services([TENANT_A], term="oth", other_from=[CUTS])
        found = lookups(qs.query.where)
        # No category, or one outside the live ones (NOT IN, Django adding
        # its IS NOT NULL for the nullable column).
        self.assertIn(("category_0_id", "isnull", True), found)
        self.assertIn(("category_0_id", "in", [CUTS]), found)

    def test_only_the_fields_a_result_needs(self):
        qs = selectors.menu_services([TENANT_A])
        self.assertEqual(
            set(qs.query.values_select),
            {"id", "tenant_id", "name", "description", "price_minor", "duration_minutes",
             "category_0_id", "created_at"},
        )


class SearchSalonsQueryTests(SimpleTestCase):
    """selectors.search_salons: /discover's salons, near the customer when sent."""

    def test_everywhere_without_a_position(self):
        qs = selectors.search_salons()
        self.assertNotIn("distance_km", qs.query.annotations)
        self.assertEqual(lookups(qs.query.where), lookups(selectors.discoverable_salons().query.where))

    def test_with_a_position_and_radius_only_salons_that_close(self):
        qs = selectors.search_salons(25.19, 55.27, 5.0)
        self.assertIn("distance_km", qs.query.annotations)
        found = lookups(qs.query.where)
        self.assertIn((None, "lte", 5.0), found)
        self.assertIn((None, "isnull", False), found)

    def test_with_a_position_and_no_radius_only_salons_with_a_pin(self):
        found = lookups(selectors.search_salons(25.19, 55.27).query.where)
        self.assertIn((None, "isnull", False), found)
        self.assertNotIn((None, "lte", 5.0), found)

    def test_only_the_card_s_name_category_and_rating_are_read(self):
        qs = selectors.search_salons()
        self.assertEqual(
            set(qs.query.values_select) | set(qs.query.annotation_select),
            {"id", "tenant_id", "branch_id", "published_name", "branch_name", "category",
             "avg_rating", "review_count"},
        )


class BookingCountsTests(SimpleTestCase):
    """selectors.service_booking_counts: booking-api's database, read-only."""

    def test_the_query(self):
        since = NOW - timedelta(days=30)
        qs = selectors.booking_count_query([BRANCH_A1], since, ("confirmed",))
        self.assertEqual(qs.db, "booking")
        found = lookups(qs.query.where)
        self.assertIn(("branch_id", "in", [BRANCH_A1]), found)
        self.assertIn(("created_at", "gte", since), found)
        self.assertIn(("status", "in", ["confirmed"]), found)
        self.assertEqual(set(qs.query.values_select), {"booking__branch_id", "service_id"})
        self.assertTrue(qs.query.annotations["bookings"].distinct)

    def test_without_the_booking_database_it_says_so(self):
        with mock.patch.dict(settings.DATABASES):
            settings.DATABASES.pop("booking", None)
            with self.assertRaises(selectors.BookingCountsUnavailable):
                selectors.service_booking_counts([BRANCH_A1], NOW, ("confirmed",))

    def test_no_branch_reads_nothing(self):
        self.assertEqual(selectors.service_booking_counts([], NOW, ("confirmed",)), {})

    def test_rows_become_a_dict_and_a_failed_read_says_so(self):
        from django.db import DatabaseError

        rows = [{"booking__branch_id": BRANCH_A1, "service_id": GENTS, "bookings": 3}]
        with mock.patch.dict(settings.DATABASES, {"booking": {}}):
            with mock.patch.object(selectors, "booking_count_query", return_value=rows):
                self.assertEqual(
                    selectors.service_booking_counts([BRANCH_A1], NOW, ("confirmed",)),
                    {(BRANCH_A1, GENTS): 3},
                )

            class Failing:
                def __iter__(self):
                    raise DatabaseError("down")

            with mock.patch.object(selectors, "booking_count_query", return_value=Failing()), \
                    self.assertRaises(selectors.BookingCountsUnavailable):
                selectors.service_booking_counts([BRANCH_A1], NOW, ("confirmed",))


class PopularScoreTests(SimpleTestCase):
    """selectors.with_popular_score: the weighted rating of doc 3.1."""

    def score(self, rating, reviews, average):
        """The formula exactly as the doc writes it."""
        m = selectors.POPULAR_TRUSTED_REVIEWS
        return (reviews / (reviews + m)) * rating + (m / (reviews + m)) * average

    def test_the_doc_s_example_three_hundred_reviews_beat_one(self):
        self.assertGreater(self.score(4.8, 300, 4.2), self.score(5.0, 1, 4.2))
        self.assertEqual(selectors.POPULAR_TRUSTED_REVIEWS, 10)

    def test_the_annotation_is_the_doc_s_formula(self):
        qs = selectors.with_popular_score(selectors.discoverable_salons(), 4.2)
        self.assertIn("popular_score", qs.query.annotations)
        text = repr(qs.query.annotations["popular_score"])
        # (v * R + m * C) / (v + m), with v the review count and R the rating
        # (discoverable_salons' subqueries, resolved).
        v = 'Cast(Coalesce(SELECT COUNT(U0."id")'
        r = 'Cast(SELECT AVG(U0."rating")'
        self.assertTrue(text.startswith(f"ExpressionWrapper({v}"), text)
        self.assertIn(f"Value(0))) * {r}", text)
        self.assertIn("LIMIT 1) + Value(10.0) * Value(4.2) / " + v, text)
        self.assertTrue(text.endswith("Value(0))) + Value(10.0))"), text)

    def test_no_salon_rated_yet_still_builds(self):
        qs = selectors.with_popular_score(selectors.discoverable_salons(), None)
        self.assertIn("Value(0.0)", repr(qs.query.annotations["popular_score"]))


# ---------------------------------------------------------------------------
# GET /discover?is_popular=
# ---------------------------------------------------------------------------


def discover_view(text):
    view = views.SalonDiscoveryListView()
    view.request = Request(APIRequestFactory().get(f"/api/v1/discover?{text}"))
    view.format_kwarg = None
    view.kwargs = {}
    return view


class DiscoverPopularTests(SimpleTestCase):

    def queryset(self, text):
        with mock.patch.object(views, "average_salon_rating", return_value=4.2) as average:
            qs = discover_view(text).get_queryset()
        return qs, average

    @override_settings(SEARCH_POPULAR_V1=True)
    def test_popular_first_then_more_reviews_then_id(self):
        qs, average = self.queryset("is_popular=true")
        average.assert_called_once()
        self.assertIn("popular_score", qs.query.annotations)
        self.assertEqual(
            tuple(qs.query.order_by),
            (OrderBy(F("popular_score"), descending=True, nulls_last=True), "-review_count", "id"),
        )

    @override_settings(SEARCH_POPULAR_V1=True)
    def test_with_a_position_the_nearest_breaks_a_tie(self):
        qs, _ = self.queryset("is_popular=true&latitude=25.19&longitude=55.27&radius=500")
        self.assertEqual(tuple(qs.query.order_by)[1:], ("-review_count", "distance_km", "id"))

    @override_settings(SEARCH_POPULAR_V1=True)
    def test_it_wins_over_sort_and_every_filter_still_narrows(self):
        popular, _ = self.queryset(
            "is_popular=true&sort=rating&category=gents&search=iron&is_top_rated=true"
        )
        plain, _ = self.queryset("sort=rating&category=gents&search=iron&is_top_rated=true")
        self.assertEqual(popular.query.order_by[0].expression, F("popular_score"))
        self.assertEqual(lookups(popular.query.where), lookups(plain.query.where))

    @override_settings(SEARCH_POPULAR_V1=True)
    def test_without_is_popular_today_s_order(self):
        qs, average = self.queryset("is_popular=false")
        average.assert_not_called()
        self.assertNotIn("popular_score", qs.query.annotations)
        self.assertEqual(
            tuple(qs.query.order_by),
            (OrderBy(F("avg_rating"), descending=True, nulls_last=True), "-review_count", "id"),
        )

    @override_settings(SEARCH_POPULAR_V1=False)
    def test_off_is_popular_is_ignored(self):
        qs, average = self.queryset("is_popular=true")
        average.assert_not_called()
        self.assertNotIn("popular_score", qs.query.annotations)
        # Not read, so not checked either: exactly today.
        discover_view("is_popular=maybe").get_queryset()

    @override_settings(SEARCH_POPULAR_V1=True)
    def test_a_bad_is_popular_is_a_422(self):
        response = self.client.get("/api/v1/discover?is_popular=maybe")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["errors"][0]["field"], "is_popular")

    @override_settings(SEARCH_POPULAR_V1=True)
    def test_a_page_size_out_of_range_is_a_422(self):
        response = self.client.get("/api/v1/discover?pageSize=51")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["errors"][0]["field"], "pageSize")


class DiscoverPageSizeTests(SimpleTestCase):
    """pageSize on /discover, and page_size exactly as it was."""

    def page(self, text):
        view = discover_view(text)
        return view.paginate_queryset(list(range(80)))

    @override_settings(SEARCH_POPULAR_V1=True)
    def test_on_page_size_is_honoured(self):
        self.assertEqual(len(self.page("pageSize=10")), 10)
        self.assertEqual(len(self.page("pageSize=10&page_size=30")), 10)

    @override_settings(SEARCH_POPULAR_V1=True)
    def test_page_size_keeps_its_old_reading(self):
        self.assertEqual(len(self.page("")), 15)
        self.assertEqual(len(self.page("page_size=30")), 30)
        self.assertEqual(len(self.page("page_size=100")), 50)
        self.assertEqual(len(self.page("page_size=abc")), 15)

    @override_settings(SEARCH_POPULAR_V1=False)
    def test_off_page_size_is_ignored_as_today(self):
        self.assertEqual(len(self.page("pageSize=10")), 15)


# ---------------------------------------------------------------------------
# GET /services across salons
# ---------------------------------------------------------------------------


class ServicesAcrossSalons:
    """GET /services with the selectors mocked at the edges."""

    def call(self, text="", salons=(A1, A2, B), services=MENU, counts=None, counts_error=None):
        with ExitStack() as stack:
            def patch(name, **kw):
                return stack.enter_context(mock.patch.object(service_search_views, name, **kw))

            self.salons = patch("search_salons", return_value=list(salons))
            self.categories = patch("categories_for_tenants", return_value=CATEGORIES)
            self.menu = patch("menu_services", side_effect=lambda tenants, **kw: [
                svc for svc in services if svc["tenant_id"] in tenants
            ])
            self.branch_rows = patch("branch_service_rows", return_value=BRANCH_ROWS)
            if counts_error is not None:
                self.counts = patch("service_booking_counts", side_effect=counts_error)
            else:
                self.counts = patch("service_booking_counts", return_value=counts or {})
            stack.enter_context(mock.patch.object(
                service_search_views.timezone, "now", return_value=NOW
            ))
            return self.client.get(f"{SERVICES_URL}?{text}")


@override_settings(SEARCH_POPULAR_V1=True)
class ServicesAcrossSalonsTests(ServicesAcrossSalons, SimpleTestCase):

    def test_the_page_shape_of_discover(self):
        response = self.call("pageSize=3")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"count", "next", "previous", "results"})
        self.assertEqual(body["count"], 7)
        self.assertEqual(len(body["results"]), 3)
        self.assertIsNone(body["previous"])
        self.assertIn("page=2", body["next"])
        self.assertIn("pageSize=3", body["next"])

    def test_the_default_page_is_twenty(self):
        many = [service(uuid.uuid4(), TENANT_A, f"Cut {i}") for i in range(25)]
        body = self.call(salons=(A1,), services=many).json()
        self.assertEqual((body["count"], len(body["results"])), (25, 20))

    def test_a_page_that_is_no_number_is_the_paginator_s_404_never_a_500(self):
        for page in ("abc", "²", "0"):
            with self.subTest(page=page):
                self.assertEqual(self.call(f"is_popular=true&page={page}").status_code, 404)

    def test_page_two(self):
        body = self.call("pageSize=3&page=3").json()
        self.assertEqual(len(body["results"]), 1)
        self.assertIsNone(body["next"])
        self.assertIn("page=2", body["previous"])

    def test_without_is_popular_the_newest_first_and_no_counts_read(self):
        body = self.call().json()
        self.assertEqual(
            [(r["salon"]["id"], r["name"]) for r in body["results"]][:3],
            [(str(SALON_A1), "The Gentleman's Cut"), (str(SALON_A2), "The Gentleman's Cut"),
             (str(SALON_A1), "Root Colour")],
        )
        self.counts.assert_not_called()

    def test_is_popular_the_most_booked_first(self):
        body = self.call("is_popular=true", counts={(BRANCH_B, FADE): 5, (BRANCH_A2, ROOT): 2}).json()
        self.assertEqual(
            [(r["salon"]["id"], r["id"]) for r in body["results"]][:2],
            [(str(SALON_B), str(FADE)), (str(SALON_A2), str(ROOT))],
        )
        branches, since, statuses = self.counts.call_args.args
        self.assertEqual(set(branches), {BRANCH_A1, BRANCH_A2, BRANCH_B})
        self.assertEqual(since, NOW - timedelta(days=30))
        self.assertEqual(statuses, service_search.COUNTED_STATUSES)

    def test_is_popular_at_most_two_of_one_salon_on_a_page(self):
        many = [service(uuid.uuid4(), TENANT_A, f"Cut {i}", days_old=i) for i in range(6)]
        body = self.call("is_popular=true&pageSize=4", salons=(A1, B), services=many + MENU[3:]).json()
        page = [r["salon"]["id"] for r in body["results"]]
        self.assertEqual(page, [str(SALON_A1), str(SALON_A1), str(SALON_B), str(SALON_A1)])
        # The fourth is A1's again: no other salon was left to fill the row.
        self.assertEqual(body["count"], 7)

    def test_without_booking_api_s_database_the_tie_break_order_not_an_error(self):
        with self.assertLogs(service_search_views.logger, "WARNING"):
            response = self.call(
                "is_popular=true",
                counts_error=service_search_views.BookingCountsUnavailable("not set"),
            )
        self.assertEqual(response.status_code, 200)
        # A1 is rated 4.8, A2 4.0, B nobody: by the salon's rating.
        self.assertEqual(response.json()["results"][0]["salon"]["id"], str(SALON_A1))

    def test_search_finds_a_name_or_a_category_label(self):
        body = self.call("search=colour").json()
        self.assertEqual(
            sorted((r["salon"]["id"], r["name"]) for r in body["results"]),
            sorted([(str(SALON_A1), "Balayage"), (str(SALON_A1), "Root Colour"),
                    (str(SALON_A2), "Balayage"), (str(SALON_A2), "Root Colour")]),
        )
        kwargs = self.menu.call_args.kwargs
        self.assertEqual((kwargs["term"], kwargs["label_ids"], kwargs["other_from"]),
                         ("colour", {COLOUR, HIGHLIGHTS}, None))

    def test_search_for_other_reads_the_services_with_no_category(self):
        self.call("search=other")
        self.assertEqual(set(self.menu.call_args.kwargs["other_from"]), set(CATEGORIES))

    def test_search_with_is_popular_and_a_position_together(self):
        self.call("search=fade&is_popular=true&latitude=25.19&longitude=55.27&radius=500")
        self.salons.assert_called_once_with(25.19, 55.27, 500.0)
        self.counts.assert_called_once()

    def test_without_a_position_every_salon_counts(self):
        self.call("is_popular=true")
        self.salons.assert_called_once_with(None, None, None)

    def test_nothing_matches_is_a_200_with_no_results(self):
        response = self.call("search=zzz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"count": 0, "next": None, "previous": None, "results": []})

    def test_no_salon_near_is_a_200_with_no_results(self):
        response = self.call("latitude=1&longitude=1&radius=1", salons=())
        self.assertEqual(response.json()["count"], 0)

    def test_section_5_errors_are_422_in_the_envelope(self):
        for text, field in (("is_popular=maybe", "is_popular"), ("pageSize=51", "pageSize"),
                            ("pageSize=0", "pageSize"), ("latitude=25.19", "longitude"),
                            ("longitude=55.27", "latitude")):
            with self.subTest(text=text):
                response = self.call(text)
                self.assertEqual(response.status_code, 422)
                body = response.json()
                self.assertEqual(body["code"], "validation_error")
                self.assertEqual(body["errors"][0]["field"], field)
                self.salons.assert_not_called()

    def test_auth_is_optional(self):
        self.assertEqual(self.call().status_code, 200)

    def test_a_result_is_the_doc_s_fields(self):
        body = self.call("search=fade").json()
        self.assertEqual(body["results"], [{
            "id": str(FADE),
            "name": "Skin Fade",
            "description": None,
            "price": 90,
            "duration_min": 30,
            "duration_max": 30,
            "category": {"id": str(B_CUTS), "label": "Cuts"},
            "salon": {"id": str(SALON_B), "name": "Colour Bar", "category": "ladies"},
        }])


class OldMenuRouteTests(ServicesAcrossSalons, SimpleTestCase):
    """The old route, one salon's menu, is untouched, switch on or off."""

    @override_settings(SEARCH_POPULAR_V1=True)
    def test_on_tenant_id_is_still_one_salon_s_menu(self):
        with mock.patch.object(views, "discoverable_salons") as lookup, \
                mock.patch.object(views.SalonServicesView, "get") as menu:
            lookup.return_value.filter.return_value.first.return_value = types.SimpleNamespace(id=SALON_A1)
            menu.return_value.data = {"service_categories": [], "service_groups": []}
            response = self.client.get(f"{SERVICES_URL}?tenant_id={TENANT_A}")
        self.assertEqual(response.json(), {"service_categories": [], "service_groups": []})

    @override_settings(SEARCH_POPULAR_V1=True)
    def test_on_a_bad_old_id_is_still_its_422(self):
        response = self.client.get(f"{SERVICES_URL}?branch_id={BRANCH_A1}")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["errors"][0]["field"], "tenant_id")

    @override_settings(SEARCH_POPULAR_V1=False)
    def test_off_no_tenant_id_is_the_422_it_was(self):
        response = self.call("is_popular=true")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["errors"][0]["field"], "tenant_id")
        self.salons.assert_not_called()


class OpenApiTests(SimpleTestCase):
    """Swagger tells both answers apart."""

    def parameters(self, path):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        return {p["name"]: p for p in schema["paths"][path]["get"]["parameters"]}

    def test_services_lists_the_new_params(self):
        found = self.parameters(SERVICES_URL)
        for name in ("is_popular", "search", "latitude", "longitude", "radius", "page", "pageSize"):
            self.assertIn(name, found)
        self.assertNotIn("required", found["tenant_id"])

    def test_discover_lists_is_popular_and_page_size(self):
        found = self.parameters("/api/v1/discover")
        self.assertIn("is_popular", found)
        self.assertIn("pageSize", found)

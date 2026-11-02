"""
The map's Filters drawer (MAP_FILTER_V1, docs/MAP_FILTERS_API.md): GET
/me/map/filters and the five filter params on GET /discover/map
(docs/map-filter-params-fe-contract.md).

The querysets are built and inspected, never run: the platform's tables are
unmanaged, so they do not exist in the test database. The views are called
with their selectors mocked. The arithmetic is pure and runs for real.
"""

import random
import types
import uuid
import zoneinfo
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from unittest import mock

from django.core.cache import cache
from django.db import DatabaseError
from django.db.models import Exists
from django.http import QueryDict
from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from drf_spectacular.generators import SchemaGenerator

from apps.salons import map_availability, map_filters, map_views, selectors, slots, views
from apps.salons.map_availability import ANY_SERVICE, AvailabilityUnavailable
from apps.salons.map_filters import MapFilters, NO_FILTERS, parse_map_filters
from apps.salons.params import ParamError, parse_map

DUBAI = zoneinfo.ZoneInfo("Asia/Dubai")
SALON_A = uuid.UUID("33333333-3333-3333-3333-3333333333a1")
SALON_B = uuid.UUID("33333333-3333-3333-3333-3333333333b2")
TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
BRANCH = uuid.UUID("22222222-2222-2222-2222-222222222222")
STYLIST = uuid.UUID("99999999-9999-9999-9999-999999999990")
CUT = uuid.UUID("66666666-6666-6666-6666-666666666660")
COLOUR = uuid.UUID("66666666-6666-6666-6666-666666666661")
HAIR = map_filters.category_option_id("Hair")
NAILS = map_filters.category_option_id("Nails")

MAP_URL = "/api/v1/discover/map"
DRAWER_URL = "/api/v1/me/map/filters"


def q(text):
    return QueryDict(text)


def picks(section):
    """Every option id the drawer offers in one section."""
    return [option["id"] for option in section["options"]]


CATALOG = {
    HAIR: {"label": "Hair", "category_ids": frozenset({uuid.UUID(int=1), uuid.UUID(int=2)}), "salons": 3},
    NAILS: {"label": "Nails", "category_ids": frozenset({uuid.UUID(int=3)}), "salons": 1},
}


class CategoryIdTests(SimpleTestCase):
    """A drawer category is a NAME across salons, and its id a UUID made from it."""

    def test_the_same_name_is_the_same_id_however_it_is_spelled(self):
        self.assertEqual(map_filters.category_option_id("Hair"), map_filters.category_option_id("  hair "))
        self.assertEqual(
            map_filters.category_option_id("Hair  Cut"), map_filters.category_option_id("hair cut")
        )

    def test_another_name_is_another_id(self):
        self.assertNotEqual(HAIR, NAILS)

    def test_the_id_is_a_uuid_like_the_fe_doc_shows(self):
        self.assertEqual(str(uuid.UUID(HAIR)), HAIR)

    def test_the_id_never_changes_between_releases(self):
        # Pinned: every drawer the app has loaded holds these. A new
        # CATEGORY_NAMESPACE would change them all.
        self.assertEqual(HAIR, str(uuid.uuid5(map_filters.CATEGORY_NAMESPACE, "hair")))
        self.assertEqual(
            str(map_filters.CATEGORY_NAMESPACE),
            str(uuid.uuid5(uuid.NAMESPACE_URL,
                           "https://api.gostyle.uk/api/v1/me/map/filters/service_categories")),
        )

    def test_no_name_has_no_id(self):
        self.assertIsNone(map_filters.category_option_id("   "))
        self.assertIsNone(map_filters.category_option_id(None))

    def test_a_service_is_filed_as_on_the_services_tab_parent_first(self):
        parent, child, lone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        categories = {
            parent: {"id": parent, "name_en": "Hair", "parent_id": None, "icon": None},
            child: {"id": child, "name_en": "Colouring", "parent_id": parent, "icon": None},
            lone: {"id": lone, "name_en": "Nails", "parent_id": None, "icon": None},
        }
        self.assertEqual(map_filters.service_option_id(categories, child), HAIR)
        self.assertEqual(map_filters.service_option_id(categories, parent), HAIR)
        self.assertEqual(map_filters.service_option_id(categories, lone), NAILS)
        # No category, or a deleted one: the tab's "Other", which is no chip here.
        self.assertIsNone(map_filters.service_option_id(categories, None))
        self.assertIsNone(map_filters.service_option_id(categories, uuid.uuid4()))


class BuildCatalogTests(SimpleTestCase):
    """map_filters.build_catalog: one option per name, across every public salon."""

    def setUp(self):
        self.t1, self.t2, self.t3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.hair1, self.colour1, self.hair2, self.hair3 = (uuid.uuid4() for _ in range(4))
        self.spa2, self.empty3 = uuid.uuid4(), uuid.uuid4()
        rows = [
            (self.hair1, "Hair", None),
            (self.colour1, "Colouring", self.hair1),
            (self.hair2, "hair", None),
            (self.hair3, "HAIR ", None),
            (self.spa2, "Spa", None),
            (self.empty3, "Waxing", None),
        ]
        self.categories = {
            cid: {"id": cid, "name_en": name, "parent_id": parent, "icon": None}
            for cid, name, parent in rows
        }
        self.salons_of = {self.t1: 2, self.t2: 1, self.t3: 1}
        self.on_menu = [
            (self.t1, self.colour1),
            (self.t2, self.hair2),
            (self.t2, self.spa2),
            (self.t3, self.hair3),
            (self.t3, uuid.uuid4()),  # a deleted category: "Other"
        ]

    def build(self):
        return map_filters.build_catalog(self.salons_of, self.categories, self.on_menu)

    def test_one_option_per_name_with_every_salon_that_offers_it(self):
        catalog = self.build()
        self.assertEqual(set(catalog), {HAIR, map_filters.category_option_id("Spa")})
        # t1 has two public salons, t2 and t3 one each.
        self.assertEqual(catalog[HAIR]["salons"], 4)
        self.assertEqual(catalog[map_filters.category_option_id("Spa")]["salons"], 1)

    def test_the_ids_are_every_category_filed_under_the_name_children_included(self):
        self.assertEqual(
            self.build()[HAIR]["category_ids"],
            {self.hair1, self.colour1, self.hair2, self.hair3},
        )

    def test_a_category_with_nothing_on_a_menu_is_not_an_option(self):
        self.assertNotIn(map_filters.category_option_id("Waxing"), self.build())

    def test_the_label_is_the_spelling_most_salons_use(self):
        self.on_menu.append((uuid.uuid4(), self.hair2))
        self.salons_of[self.on_menu[-1][0]] = 1
        # "Hair" (t1), "hair" (t2 and the new one), "HAIR" (t3).
        self.assertEqual(self.build()[HAIR]["label"], "hair")

    def test_a_tie_on_the_label_is_settled_the_same_way_every_time(self):
        self.on_menu = [(self.t1, self.hair1), (self.t2, self.hair2)]
        self.assertEqual(self.build()[HAIR]["label"], "Hair")


class DrawerTests(SimpleTestCase):
    """GET /me/map/filters' body: map_filters.drawer."""

    def setUp(self):
        patcher = mock.patch.object(map_filters, "category_catalog", return_value=CATALOG)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sections = {s["key"]: s for s in map_filters.drawer()["sections"]}

    def test_the_sections_in_the_fe_doc_order(self):
        self.assertEqual(
            [s["key"] for s in map_filters.drawer()["sections"]],
            ["service_categories", "rating", "availability", "distance", "additional"],
        )

    def test_every_section_has_the_same_keys(self):
        for section in self.sections.values():
            self.assertEqual(
                list(section), ["key", "title", "multiple", "default", "options"], section["key"]
            )
            for option in section["options"]:
                self.assertEqual(list(option), ["id", "label"])

    def test_the_drawer_opens_with_open_now_on_and_everything_else_any(self):
        self.assertEqual(
            {key: s["default"] for key, s in self.sections.items()},
            {
                "service_categories": [],
                "rating": ["any"],
                "availability": ["any"],
                "distance": ["any"],
                "additional": ["open_now"],
            },
        )

    def test_single_choice_sections_start_with_any(self):
        for key in ("rating", "availability", "distance"):
            self.assertFalse(self.sections[key]["multiple"])
            self.assertEqual(self.sections[key]["options"][0], {"id": "any", "label": "Any"})
        for key in ("service_categories", "additional"):
            self.assertTrue(self.sections[key]["multiple"])
            self.assertNotIn("any", picks(self.sections[key]))

    def test_the_options(self):
        self.assertEqual(picks(self.sections["rating"]), ["any", "3", "4", "4.5", "5"])
        self.assertEqual(picks(self.sections["availability"]), ["any", "today", "tomorrow", "week"])
        self.assertEqual(picks(self.sections["distance"]), ["any", "5", "10", "20", "100"])
        self.assertEqual(picks(self.sections["additional"]), ["open_now"])

    def test_no_verified_chip_while_no_salon_can_carry_the_mark(self):
        self.assertNotIn("verified", picks(self.sections["additional"]))

    def test_categories_most_offered_first(self):
        self.assertEqual(
            self.sections["service_categories"]["options"],
            [{"id": HAIR, "label": "Hair"}, {"id": NAILS, "label": "Nails"}],
        )

    def test_every_id_the_drawer_offers_is_one_the_map_takes(self):
        # Both ends in one module: an offered id is never refused.
        for key, section in self.sections.items():
            for option_id in picks(section):
                with self.subTest(key=key, id=option_id):
                    query = f"{key}={option_id}"
                    if key == "distance" and option_id != "any":
                        query += "&origin_latitude=25.2&origin_longitude=55.27"
                    parse_map_filters(q(query))


class ParseTests(SimpleTestCase):
    """map_filters.parse_map_filters: the five params, as the FE doc's section 6 says."""

    def assert_refused(self, query, param, code):
        with self.assertRaises(ParamError) as caught:
            parse_map_filters(q(query))
        self.assertEqual((caught.exception.param, caught.exception.code), (param, code))
        return caught.exception

    def test_a_plain_map_call_has_no_filter(self):
        self.assertEqual(parse_map_filters(q("latitude=25.2&longitude=55.27&zoom=12")), NO_FILTERS)
        self.assertFalse(NO_FILTERS.active)

    def test_any_is_not_sent_in_every_param(self):
        found = parse_map_filters(q(
            "service_categories=any&rating=any&availability=any&distance=any&additional=any"
        ))
        self.assertEqual(found, NO_FILTERS)

    def test_empty_values_are_not_sent(self):
        found = parse_map_filters(q(
            "service_categories=&rating=&availability=&distance=&additional="
        ))
        self.assertEqual(found, NO_FILTERS)

    def test_everything_picked(self):
        found = parse_map_filters(q(
            f"service_categories={HAIR},{NAILS}&rating=4&availability=today"
            "&distance=10&origin_latitude=25.1972&origin_longitude=55.2744&additional=open_now"
        ))
        self.assertEqual(found, MapFilters(
            service_categories=(HAIR, NAILS), rating="4", availability="today",
            distance_km=10.0, origin=(25.1972, 55.2744), open_now=True,
        ))
        self.assertTrue(found.active)

    def test_each_param_alone_is_a_filter(self):
        for query in (
            f"service_categories={HAIR}", "rating=5", "availability=week",
            "distance=5&origin_latitude=25.2&origin_longitude=55.27", "additional=open_now",
        ):
            self.assertTrue(parse_map_filters(q(query)).active, query)

    def test_an_id_the_drawer_does_not_offer_is_refused(self):
        for param, bad in (
            ("rating", "4.0"), ("rating", "6"), ("availability", "now"), ("distance", "7"),
        ):
            with self.subTest(param):
                extra = "&origin_latitude=25.2&origin_longitude=55.27" if param == "distance" else ""
                exc = self.assert_refused(f"{param}={bad}{extra}", param, "invalid_choice")
                self.assertIn(bad, exc.message)

    def test_one_id_where_one_is_taken(self):
        self.assert_refused("rating=3,4", "rating", "invalid_choice")

    def test_verified_is_refused_like_any_id_not_offered(self):
        self.assert_refused("additional=open_now,verified", "additional", "invalid_choice")

    def test_distance_needs_both_origin_params(self):
        self.assert_refused("distance=10", "origin_latitude", "required")
        self.assert_refused("distance=10&origin_latitude=25.2", "origin_longitude", "required")
        self.assert_refused("distance=10&origin_longitude=55.2", "origin_latitude", "required")

    def test_an_origin_param_without_distance_is_refused(self):
        self.assert_refused("origin_latitude=25.2&origin_longitude=55.2", "origin_latitude", "invalid")
        self.assert_refused("origin_longitude=55.2", "origin_longitude", "invalid")
        # `any` is not sent, so it is no distance either.
        self.assert_refused("distance=any&origin_latitude=25.2&origin_longitude=55.2",
                            "origin_latitude", "invalid")

    def test_an_origin_must_be_a_place(self):
        self.assert_refused("distance=10&origin_latitude=abc&origin_longitude=55.2",
                            "origin_latitude", "invalid")
        self.assert_refused("distance=10&origin_latitude=25.2&origin_longitude=200",
                            "origin_longitude", "invalid")

    def test_lists_split_on_commas_each_id_once_any_dropped(self):
        found = parse_map_filters(q(f"service_categories= {HAIR.upper()} ,any,,{NAILS},{HAIR}"))
        self.assertEqual(found.service_categories, (HAIR, NAILS))

    def test_unknown_category_ids_are_kept_here_and_dropped_later(self):
        found = parse_map_filters(q("service_categories=not-a-category"))
        self.assertEqual(found.service_categories, ("not-a-category",))

    def test_the_ids_are_read_whatever_their_case(self):
        found = parse_map_filters(q("rating=ANY&availability=Today&additional=OPEN_NOW"))
        self.assertEqual((found.rating, found.availability, found.open_now), (None, "today", True))


def where_lookups(query):
    """(lookup, value) of every plain WHERE condition, Exists conditions left out."""
    return [
        (cond.lookup_name, cond.rhs)
        for cond in query.where.children
        if not isinstance(getattr(cond, "lhs", None), Exists) and hasattr(cond, "lookup_name")
    ]


def exists_conditions(query):
    """The EXISTS conditions of a query (a filter on Exists is `exact` on it)."""
    return [
        cond.lhs for cond in query.where.children
        if isinstance(getattr(cond, "lhs", None), Exists)
    ]


MAP = parse_map(q("latitude=25.2&longitude=55.27&latitudeDelta=0.1&longitudeDelta=0.1"))


class FilteredQuerysetTests(SimpleTestCase):
    """map_filters.filtered_queryset: the SQL half, built and inspected, not run."""

    def build(self, filters, map_params=MAP, catalog=CATALOG):
        with mock.patch.object(map_filters, "category_catalog", return_value=catalog):
            return map_filters.filtered_queryset(map_params, filters)

    def test_no_filter_is_the_map_s_own_query(self):
        qs, picked = self.build(NO_FILTERS)
        plain = selectors.map_venue_queryset(
            latitude=25.2, longitude=55.27, latitude_delta=0.1, longitude_delta=0.1,
        )
        self.assertEqual(where_lookups(qs.query), where_lookups(plain.query))
        self.assertEqual(tuple(qs.query.order_by), ("id",))
        self.assertIsNone(picked)

    def test_rating_compares_the_rating_as_the_pin_shows_it(self):
        for rating, floor in (("3", 2.95), ("4", 3.95), ("4.5", 4.45), ("5", 4.95)):
            qs, _ = self.build(MapFilters(rating=rating))
            self.assertIn("gte", [lookup for lookup, _ in where_lookups(qs.query)])
            self.assertAlmostEqual(
                [value for lookup, value in where_lookups(qs.query) if lookup == "gte"
                 and isinstance(value, float) and value < 5][-1],
                floor,
            )

    def test_the_rating_floor_keeps_what_rounds_up_to_the_pick(self):
        # MapVenueSerializer shows round(avg, 1). Half up, avg >= floor is the pick.
        for rating, floor in map_filters.RATING_FLOORS.items():
            self.assertEqual(round(floor + 0.0001, 1), float(rating))
            self.assertLess(round(floor - 0.0001, 1), float(rating))

    def test_distance_measures_from_the_origin_under_its_own_name(self):
        qs, _ = self.build(MapFilters(distance_km=10.0, origin=(25.19, 55.27)))
        self.assertIn(map_filters.ORIGIN_DISTANCE, qs.query.annotations)
        self.assertNotIn("distance_km", qs.query.annotations)
        self.assertIn(("lte", 10.0), where_lookups(qs.query))
        # The nearest to the customer first, so the cap keeps those.
        self.assertEqual(tuple(qs.query.order_by), (map_filters.ORIGIN_DISTANCE, "id"))

    def test_with_a_radius_too_the_map_keeps_its_own_order_and_both_distances(self):
        with_radius = parse_map(q("latitude=25.2&longitude=55.27&radius=5000"))
        qs, _ = self.build(MapFilters(distance_km=10.0, origin=(25.19, 55.27)), map_params=with_radius)
        self.assertIn("distance_km", qs.query.annotations)
        self.assertIn(map_filters.ORIGIN_DISTANCE, qs.query.annotations)
        self.assertEqual(tuple(qs.query.order_by), ("distance_km", "id"))

    def test_service_categories_is_an_exists_on_the_menu_of_the_known_ids(self):
        qs, picked = self.build(MapFilters(service_categories=(HAIR, "gone-since")))
        self.assertEqual(picked, [HAIR])
        (exists,) = exists_conditions(qs.query)
        inner = exists.query
        found = {
            (cond.lhs.target.name, cond.lookup_name): cond.rhs
            for cond in inner.where.children
            if hasattr(cond, "lhs") and hasattr(cond.lhs, "target")
        }
        self.assertEqual(found[("status", "exact")], "PUBLISHED")
        self.assertIs(found[("deleted_at", "isnull")], True)
        self.assertIs(found[("online_booking_enabled", "exact")], True)
        # `category_0` is inspectdb's name for the category_id foreign key.
        self.assertEqual(set(found[("category_0", "in")]), set(CATALOG[HAIR]["category_ids"]))
        # The salon's own business: the outer row's tenant.
        outer = found[("tenant_id", "exact")].target
        self.assertEqual((outer.model.__name__, outer.name), ("Storefront", "tenant_id"))

    def test_every_category_id_unknown_is_no_filter(self):
        qs, picked = self.build(MapFilters(service_categories=("gone", "also-gone")))
        self.assertIsNone(picked)
        self.assertEqual(exists_conditions(qs.query), [])


def row(pk, manual_state=None):
    """A map row as filtered_map_venues reads it for "open now"."""
    return types.SimpleNamespace(
        pk=pk, branch_timezone="Asia/Dubai", published_hours={}, manual_state=manual_state,
    )


NOW = datetime(2026, 11, 2, 16, 0, tzinfo=dt_timezone.utc)  # 20:00 in Dubai, a Monday


class KeepOpenAndAvailableTests(SimpleTestCase):
    """The Python half: open now, then availability on what is left, order kept."""

    def test_open_now_by_the_card_s_own_rule(self):
        rows = [row(SALON_A, "OPEN"), row(SALON_B, "CLOSED"), row(uuid.uuid4())]
        kept = map_filters.keep_open_and_available(rows, MapFilters(open_now=True), None, NOW)
        # Open by hand, shut by hand, and no hours at all (unknown is not open).
        self.assertEqual([r.pk for r in kept], [SALON_A])

    def test_availability_runs_last_on_what_is_left(self):
        rows = [row(SALON_A, "OPEN"), row(SALON_B, "CLOSED")]
        with mock.patch.object(map_availability, "available_ids", return_value={SALON_A}) as found:
            kept = map_filters.keep_open_and_available(
                rows, MapFilters(open_now=True, availability="today"), [HAIR], NOW
            )
        self.assertEqual([r.pk for r in kept], [SALON_A])
        asked, window, picked, now = found.call_args.args
        self.assertEqual(([r.pk for r in asked], window, picked, now), ([SALON_A], "today", [HAIR], NOW))

    def test_availability_stops_once_the_cap_is_full(self):
        rows = [row(uuid.uuid4(), "OPEN") for _ in range(5)]
        every = {r.pk for r in rows}
        with mock.patch.object(map_availability, "available_ids", return_value=every) as found:
            kept = map_filters.keep_open_and_available(
                rows, MapFilters(availability="week"), None, NOW, cap=2
            )
        self.assertEqual(kept, rows[:2])
        found.assert_called_once()
        self.assertEqual(found.call_args.args[0], rows[:2])

    def test_availability_reads_on_in_order_until_the_cap_is_full(self):
        rows = [row(uuid.uuid4(), "OPEN") for _ in range(5)]
        # Only the 2nd and the 5th have a start left.
        with mock.patch.object(
            map_availability, "available_ids", return_value={rows[1].pk, rows[4].pk}
        ) as found:
            kept = map_filters.keep_open_and_available(
                rows, MapFilters(availability="today"), None, NOW, cap=2
            )
        self.assertEqual(kept, [rows[1], rows[4]])
        self.assertEqual(
            [call.args[0] for call in found.call_args_list], [rows[0:2], rows[2:4], rows[4:5]]
        )

    def test_nothing_left_asks_nothing(self):
        with mock.patch.object(map_availability, "available_ids") as found:
            kept = map_filters.keep_open_and_available(
                [row(SALON_B, "CLOSED")], MapFilters(open_now=True, availability="week"), None, NOW
            )
        self.assertEqual(kept, [])
        found.assert_not_called()


class FilteredMapVenuesTests(SimpleTestCase):
    """filtered_map_venues: filter first, then the cap."""

    def test_the_cap_comes_after_the_python_filters(self):
        rows = [row(uuid.uuid4(), "CLOSED") for _ in range(3)] + [row(SALON_A, "OPEN"), row(SALON_B, "OPEN")]
        fake_qs = mock.MagicMock()
        fake_qs.annotate.return_value = fake_qs
        with mock.patch.object(map_filters, "filtered_queryset", return_value=(fake_qs, None)), \
                mock.patch.object(map_filters, "with_published_card_fields", return_value=rows):
            kept = map_filters.filtered_map_venues({**MAP, "limit": 1}, MapFilters(open_now=True), NOW)
        self.assertEqual([r.pk for r in kept], [SALON_A])

    def test_sql_only_filters_slice_in_sql(self):
        fake_qs = mock.MagicMock()
        fake_qs.__getitem__.return_value = ["a", "b"]
        with mock.patch.object(map_filters, "filtered_queryset", return_value=(fake_qs, None)):
            kept = map_filters.filtered_map_venues({**MAP, "limit": 2}, MapFilters(rating="4"), NOW)
        fake_qs.__getitem__.assert_called_once_with(slice(None, 2))
        self.assertEqual(kept, ["a", "b"])


class LatestStartTests(SimpleTestCase):
    """slots.latest_start: the last of `starts`, worked out instead of walked."""

    def test_same_as_the_last_of_starts_on_many_random_days(self):
        rng = random.Random(20261102)
        day_start = datetime(2026, 11, 3, tzinfo=DUBAI)
        for _ in range(3000):
            free = []
            cursor = day_start + timedelta(minutes=rng.randrange(0, 600))
            for _ in range(rng.randrange(0, 4)):
                begin = cursor + timedelta(minutes=rng.randrange(0, 120), seconds=rng.choice([0, 0, 17]))
                end = begin + timedelta(minutes=rng.randrange(0, 240))
                free.append((begin, end))
                cursor = end
            duration = rng.choice([0, 5, 15, 20, 25, 30, 45, 60, 90, 135])
            window_start = day_start + timedelta(minutes=rng.randrange(0, 900))
            window_end = window_start + timedelta(minutes=rng.randrange(1, 900))
            earliest = day_start + timedelta(minutes=rng.randrange(0, 1200), seconds=rng.randrange(0, 60))
            walked = slots.starts(free, duration, window_start, window_end, earliest, day_start)
            self.assertEqual(
                slots.latest_start(free, duration, window_start, window_end, earliest, day_start),
                walked[-1] if walked else None,
                (free, duration, window_start, window_end, earliest),
            )

    def test_none_when_nothing_fits(self):
        day_start = datetime(2026, 11, 3, tzinfo=DUBAI)
        free = [(day_start + timedelta(hours=10), day_start + timedelta(hours=10, minutes=20))]
        self.assertIsNone(slots.latest_start(free, 30, day_start, day_start + timedelta(days=1),
                                             day_start, day_start))
        self.assertIsNone(slots.latest_start([], 30, day_start, day_start + timedelta(days=1),
                                             day_start, day_start))


def at(day, hhmm):
    hour, minute = map(int, hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=DUBAI)


TUESDAY = date(2026, 11, 3)
WEEKLY = [
    {"day": d, "open": "10:00", "close": "22:00", "closed": False}
    for d in ("mon", "tue", "wed", "thu", "fri", "sat")
] + [{"day": "sun", "closed": True}]


def entry(services=None, coverage=None, shifts=None, option_of=None, days=(TUESDAY,)):
    """A salon's inputs, as map_availability._read leaves them."""
    salon = types.SimpleNamespace(pk=SALON_A, branch_id=BRANCH, tenant_id=TENANT)
    open_days = {}
    for day in days:
        day_start = datetime.combine(day, time.min, tzinfo=DUBAI)
        open_days[day] = (day_start, map_availability.open_span(WEEKLY, None, day, day_start))
    return map_availability._Salon(
        salon=salon, tz=DUBAI, today=days[0], open_days=open_days,
        services=services if services is not None else {
            CUT: {"id": CUT, "category_0_id": None, "duration_minutes": 30, "stage_minutes": 30,
                  "lead_time_minutes": None},
            COLOUR: {"id": COLOUR, "category_0_id": None, "duration_minutes": 60, "stage_minutes": 60,
                     "lead_time_minutes": 120},
        },
        coverage=coverage if coverage is not None else {STYLIST: [CUT, COLOUR]},
        shifts=shifts if shifts is not None else {
            (STYLIST, day): {"start_time": "09:00", "end_time": "21:00", "break_time": "13:00"}
            for day in days
        },
        option_of=option_of if option_of is not None else {CUT: HAIR, COLOUR: NAILS},
    )


class OpenSpanTests(SimpleTestCase):
    """map_availability.open_span: views._open_span's rule, from rows already read."""

    def test_open_on_the_published_hours(self):
        day_start = datetime.combine(TUESDAY, time.min, tzinfo=DUBAI)
        self.assertEqual(
            map_availability.open_span(WEEKLY, None, TUESDAY, day_start),
            (at(TUESDAY, "10:00"), at(TUESDAY, "22:00")),
        )

    def test_shut_by_hand_or_in_the_grid_or_with_no_hours(self):
        sunday = date(2026, 11, 8)
        day_start = datetime.combine(TUESDAY, time.min, tzinfo=DUBAI)
        self.assertIsNone(map_availability.open_span(WEEKLY, "CLOSED", TUESDAY, day_start))
        self.assertIsNone(map_availability.open_span(
            WEEKLY, None, sunday, datetime.combine(sunday, time.min, tzinfo=DUBAI)))
        self.assertIsNone(map_availability.open_span(None, None, TUESDAY, day_start))

    def test_busy_by_hand_is_still_open(self):
        day_start = datetime.combine(TUESDAY, time.min, tzinfo=DUBAI)
        self.assertIsNotNone(map_availability.open_span(WEEKLY, "BUSY", TUESDAY, day_start))


@override_settings(SLOT_MINUTES_AS_BOOKED_V1=True)
class PlanTests(SimpleTestCase):
    """map_availability._plan: per day and category, the latest moment to book."""

    def plan(self, salon, busy=None):
        return map_availability._plan(salon, busy or {})

    def test_the_last_free_start_minus_the_notice(self):
        busy = {(BRANCH, STYLIST): [(at(TUESDAY, "18:00"), at(TUESDAY, "19:00"))]}
        plan = self.plan(entry(), busy)[TUESDAY.isoformat()]
        # Free 10:00-13:00, 14:00-18:00, 19:00-21:00 (shift 09-21, salon from 10).
        self.assertEqual(plan[HAIR], at(TUESDAY, "20:30").timestamp())      # 30 min, no notice
        self.assertEqual(plan[NAILS], at(TUESDAY, "18:00").timestamp())     # 60 min from 20:00, 2 h notice
        self.assertEqual(plan[ANY_SERVICE], at(TUESDAY, "20:30").timestamp())

    def test_bookings_and_the_break_take_their_time(self):
        busy = {(BRANCH, STYLIST): [(at(TUESDAY, "19:00"), at(TUESDAY, "21:00")),
                                    (at(TUESDAY, "16:30"), at(TUESDAY, "18:00"))]}
        plan = self.plan(entry(), busy)[TUESDAY.isoformat()]
        # Free 10:00-13:00, 14:00-16:30 and 18:00-19:00.
        self.assertEqual(plan[HAIR], at(TUESDAY, "18:30").timestamp())
        self.assertEqual(plan[NAILS], at(TUESDAY, "18:00").timestamp() - 7200)

    def test_a_booked_evening_leaves_the_afternoon(self):
        busy = {(BRANCH, STYLIST): [(at(TUESDAY, "16:30"), at(TUESDAY, "21:00"))]}
        plan = self.plan(entry(), busy)[TUESDAY.isoformat()]
        # Free 10:00-13:00 and 14:00-16:30; the break is 13:00-14:00.
        self.assertEqual(plan[HAIR], at(TUESDAY, "16:00").timestamp())
        self.assertEqual(plan[NAILS], at(TUESDAY, "15:30").timestamp() - 7200)

    def test_another_branch_s_booking_is_not_this_one_s(self):
        busy = {(uuid.uuid4(), STYLIST): [(at(TUESDAY, "10:00"), at(TUESDAY, "21:00"))]}
        plan = self.plan(entry(), busy)[TUESDAY.isoformat()]
        self.assertEqual(plan[HAIR], at(TUESDAY, "20:30").timestamp())

    def test_the_engine_s_trading_day_bounds_the_shift(self):
        shifts = {(STYLIST, TUESDAY): {"start_time": "08:00", "end_time": "23:30", "break_time": None}}
        late = entry(shifts=shifts)
        late.open_days[TUESDAY] = (
            late.open_days[TUESDAY][0], (at(TUESDAY, "08:00"), at(TUESDAY, "23:59")))
        plan = self.plan(late)[TUESDAY.isoformat()]
        # booking-api books 10:00 to 22:00: the last 30 minutes start at 21:30.
        self.assertEqual(plan[HAIR], at(TUESDAY, "21:30").timestamp())

    def test_a_fully_booked_day_is_not_in_the_plan(self):
        busy = {(BRANCH, STYLIST): [(at(TUESDAY, "09:00"), at(TUESDAY, "21:00"))]}
        self.assertEqual(self.plan(entry(), busy), {})

    def test_no_shift_that_day_is_no_start(self):
        self.assertEqual(self.plan(entry(shifts={})), {})

    def test_a_service_nobody_here_can_do_alone_is_no_start(self):
        plan = self.plan(entry(coverage={STYLIST: [CUT]}))[TUESDAY.isoformat()]
        self.assertNotIn(NAILS, plan)

    def test_a_service_with_no_category_counts_for_any_service_only(self):
        plan = self.plan(entry(option_of={CUT: None, COLOUR: None}))[TUESDAY.isoformat()]
        self.assertEqual(list(plan), [ANY_SERVICE])

    def test_the_same_starts_as_the_slot_picker(self):
        # views._offers walks the grid; the plan's book-by must be its last start.
        busy = {(BRANCH, STYLIST): [(at(TUESDAY, "17:10"), at(TUESDAY, "17:55"))]}
        plan = self.plan(entry(), busy)[TUESDAY.isoformat()]
        day_start = datetime.combine(TUESDAY, time.min, tzinfo=DUBAI)
        free = slots.subtract(
            [(at(TUESDAY, "10:00"), at(TUESDAY, "21:00"))],
            [(at(TUESDAY, "17:10"), at(TUESDAY, "17:55")), slots.break_span(day_start, "13:00")],
        )
        walked = slots.starts(free, 60, day_start, day_start + timedelta(days=1), day_start, day_start)
        self.assertEqual(plan[NAILS], walked[-1].timestamp() - 7200)


class AvailableIdsTests(SimpleTestCase):
    """map_availability.available_ids: book-by at or after now, in the pick's days."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.salons = [
            types.SimpleNamespace(pk=SALON_A, branch_timezone="Asia/Dubai"),
            types.SimpleNamespace(pk=SALON_B, branch_timezone="Asia/Dubai"),
        ]

    def put(self, salon_id, plan, today=date(2026, 11, 2)):
        cache.set(map_availability._cache_key(salon_id, today), plan, 60)

    def test_a_start_left_today_counts_and_a_gone_one_does_not(self):
        monday = "2026-11-02"
        self.put(SALON_A, {monday: {ANY_SERVICE: at(date(2026, 11, 2), "20:30").timestamp()}})
        self.put(SALON_B, {monday: {ANY_SERVICE: at(date(2026, 11, 2), "19:45").timestamp()}})
        self.assertEqual(map_availability.available_ids(self.salons, "today", None, NOW), {SALON_A})

    def test_a_start_exactly_now_still_counts(self):
        self.put(SALON_A, {"2026-11-02": {ANY_SERVICE: NOW.timestamp()}})
        self.put(SALON_B, {})
        self.assertEqual(map_availability.available_ids(self.salons, "today", None, NOW), {SALON_A})

    def test_tomorrow_and_the_week_read_their_own_days(self):
        self.put(SALON_A, {"2026-11-03": {ANY_SERVICE: at(date(2026, 11, 3), "11:00").timestamp()}})
        self.put(SALON_B, {"2026-11-08": {ANY_SERVICE: at(date(2026, 11, 8), "11:00").timestamp()}})
        self.assertEqual(map_availability.available_ids(self.salons, "today", None, NOW), set())
        self.assertEqual(map_availability.available_ids(self.salons, "tomorrow", None, NOW), {SALON_A})
        self.assertEqual(
            map_availability.available_ids(self.salons, "week", None, NOW), {SALON_A, SALON_B}
        )

    def test_the_seventh_day_is_the_last_one_looked_at(self):
        self.put(SALON_A, {"2026-11-09": {ANY_SERVICE: at(date(2026, 11, 9), "11:00").timestamp()}})
        self.put(SALON_B, {})
        self.assertEqual(map_availability.available_ids(self.salons, "week", None, NOW), set())

    def test_with_categories_only_their_services_count(self):
        tomorrow = "2026-11-03"
        later = at(date(2026, 11, 3), "11:00").timestamp()
        self.put(SALON_A, {tomorrow: {ANY_SERVICE: later, HAIR: later}})
        self.put(SALON_B, {tomorrow: {ANY_SERVICE: later, NAILS: later}})
        self.assertEqual(
            map_availability.available_ids(self.salons, "tomorrow", [HAIR], NOW), {SALON_A}
        )
        self.assertEqual(
            map_availability.available_ids(self.salons, "tomorrow", [HAIR, NAILS], NOW),
            {SALON_A, SALON_B},
        )

    def test_today_is_the_salon_s_today(self):
        # 22:30 UTC on the 2nd is already the 3rd in Dubai.
        late = datetime(2026, 11, 2, 22, 30, tzinfo=dt_timezone.utc)
        self.put(SALON_A, {"2026-11-03": {ANY_SERVICE: at(date(2026, 11, 3), "11:00").timestamp()}},
                 today=date(2026, 11, 3))
        self.put(SALON_B, {}, today=date(2026, 11, 3))
        self.assertEqual(map_availability.available_ids(self.salons, "today", None, late), {SALON_A})

    def test_a_salon_not_cached_is_worked_out_once_and_kept(self):
        self.put(SALON_B, {})
        plan = {"2026-11-02": {ANY_SERVICE: at(date(2026, 11, 2), "21:00").timestamp()}}
        with mock.patch.object(
            map_availability, "_plans", return_value=[(self.salons[0], date(2026, 11, 2), plan)]
        ) as worked:
            found = map_availability.available_ids(self.salons, "today", None, NOW)
        self.assertEqual(found, {SALON_A})
        ((todo,), _) = worked.call_args
        self.assertEqual([(s.pk, today) for s, _, today in todo], [(SALON_A, date(2026, 11, 2))])
        self.assertEqual(cache.get(map_availability._cache_key(SALON_A, date(2026, 11, 2))), plan)


class ReadTests(SimpleTestCase):
    """map_availability._read: a salon's inputs, read lazily."""

    salon = types.SimpleNamespace(
        pk=SALON_A, tenant_id=TENANT, branch_id=BRANCH, branch_timezone="Asia/Dubai",
        published_hours={"weekly": WEEKLY},
    )

    def patch(self, **answers):
        fakes = {}
        for name in ("manual_states_between", "salon_stylists", "menu_timing_rows",
                     "stylist_service_coverage", "shift_rows_between", "salon_categories"):
            patcher = mock.patch.object(map_availability, name, return_value=answers.get(name))
            fakes[name] = patcher.start()
            self.addCleanup(patcher.stop)
        return fakes

    def test_shut_all_week_reads_nothing_else(self):
        fakes = self.patch(manual_states_between={})
        closed = types.SimpleNamespace(**{**vars(self.salon), "published_hours": {}})
        found = map_availability._read(closed, DUBAI, TUESDAY)
        self.assertEqual(found.open_days, {})
        fakes["salon_stylists"].assert_not_called()

    def test_shut_by_hand_on_a_day_drops_only_that_day(self):
        self.patch(manual_states_between={TUESDAY: "CLOSED"}, salon_stylists=mock.MagicMock(
            values_list=mock.Mock(return_value=[])))
        found = map_availability._read(self.salon, DUBAI, TUESDAY)
        self.assertNotIn(TUESDAY, found.open_days)
        # Tuesday to Monday, Sunday closed in the grid, Tuesday by hand.
        self.assertEqual(len(found.open_days), 5)

    def test_nobody_to_book_stops_before_the_menu(self):
        stylists = mock.MagicMock()
        stylists.values_list.return_value = []
        fakes = self.patch(manual_states_between={}, salon_stylists=stylists)
        found = map_availability._read(self.salon, DUBAI, TUESDAY)
        self.assertEqual(found.coverage, {})
        fakes["menu_timing_rows"].assert_not_called()

    def test_everything_read_once_for_the_seven_days(self):
        stylists = mock.MagicMock()
        stylists.values_list.return_value = [STYLIST]
        menu = [{"id": CUT, "category_0_id": uuid.UUID(int=1), "duration_minutes": 30,
                 "stage_minutes": 30, "lead_time_minutes": None}]
        fakes = self.patch(
            manual_states_between={}, salon_stylists=stylists, menu_timing_rows=menu,
            stylist_service_coverage={STYLIST: [CUT]},
            shift_rows_between=[{"staff_member_id": STYLIST, "shift_date": TUESDAY,
                                 "start_time": "09:00", "end_time": "21:00", "break_time": None}],
            salon_categories={uuid.UUID(int=1): {"id": uuid.UUID(int=1), "name_en": "Hair",
                                                 "parent_id": None, "icon": None}},
        )
        found = map_availability._read(self.salon, DUBAI, TUESDAY)
        self.assertEqual(found.option_of, {CUT: HAIR})
        self.assertIn((STYLIST, TUESDAY), found.shifts)
        fakes["shift_rows_between"].assert_called_once_with(
            TENANT, BRANCH, [STYLIST], TUESDAY, date(2026, 11, 9))
        fakes["manual_states_between"].assert_called_once_with(SALON_A, TUESDAY, date(2026, 11, 9))


NO_BOOKING_DB = types.SimpleNamespace(DATABASES={"default": {}})
BOOKING_DB = types.SimpleNamespace(DATABASES={"booking": {}})


class BusyTests(SimpleTestCase):
    """map_availability._busy: booking-api's /busy rule, on its database, read-only."""

    def wanted(self):
        return [entry()]

    def test_no_booking_database_is_no_answer_never_everyone_free(self):
        with mock.patch.object(map_availability, "settings", NO_BOOKING_DB):
            with self.assertRaises(AvailabilityUnavailable):
                map_availability._busy(self.wanted())

    def test_nobody_to_book_reads_nothing(self):
        with mock.patch.object(map_availability, "BookingItem") as items:
            self.assertEqual(map_availability._busy([entry(coverage={})]), {})
        items.objects.using.assert_not_called()

    def test_the_busy_rule(self):
        start, end = at(TUESDAY, "10:00"), at(TUESDAY, "10:45")
        naive = datetime(2026, 11, 3, 8, 0)
        with mock.patch.object(map_availability, "settings", BOOKING_DB), \
                mock.patch.object(map_availability, "BookingItem") as items:
            items.objects.using.return_value.filter.return_value.values_list.return_value = [
                (BRANCH, STYLIST, start, end), (BRANCH, STYLIST, naive, naive + timedelta(hours=1)),
            ]
            busy = map_availability._busy(self.wanted())
        items.objects.using.assert_called_once_with("booking")
        kwargs = items.objects.using.return_value.filter.call_args.kwargs
        self.assertEqual(kwargs["booking__status__in"], map_availability.BLOCKING_STATUSES)
        self.assertEqual(kwargs["staff_id__in"], {STYLIST})
        self.assertEqual(kwargs["booking__branch_id__in"], {BRANCH})
        # Overlap, from the salon's midnight to the end of the day after the last.
        self.assertEqual(kwargs["booking__end_at__gt"], datetime.combine(TUESDAY, time.min, tzinfo=DUBAI))
        self.assertEqual(kwargs["booking__start_at__lt"],
                         datetime.combine(TUESDAY + timedelta(days=8), time.min, tzinfo=DUBAI))
        self.assertEqual(busy[(BRANCH, STYLIST)][0], (start, end))
        # A naive time is UTC, never the server's.
        self.assertEqual(busy[(BRANCH, STYLIST)][1][0], naive.replace(tzinfo=dt_timezone.utc))

    def test_the_database_failing_is_no_answer(self):
        rows = mock.MagicMock()
        rows.__iter__.side_effect = DatabaseError("connection refused")
        with mock.patch.object(map_availability, "settings", BOOKING_DB), \
                mock.patch.object(map_availability, "BookingItem") as items:
            items.objects.using.return_value.filter.return_value.values_list.return_value = rows
            with self.assertRaises(AvailabilityUnavailable):
                map_availability._busy(self.wanted())

    def test_the_statuses_are_booking_api_s_blocking_states(self):
        # booking-api src/domain/booking/lifecycle.ts BLOCKING_STATES. A visit
        # that is over (completed, settled) still held the chair.
        self.assertEqual(
            set(map_availability.BLOCKING_STATUSES),
            {"pending_payment", "pending_confirmation", "confirmed", "checked_in",
             "in_service", "completed", "settled"},
        )


def venue(pk, rating=4.5):
    return types.SimpleNamespace(pk=pk, id=pk, lat=25.2, lng=55.27, avg_rating=rating, photo_url=None)


class DiscoverMapViewTests(SimpleTestCase):
    """GET /discover/map through the real URL, its selectors mocked."""

    def setUp(self):
        patcher = mock.patch.object(views, "map_venues", return_value=[venue(SALON_A)])
        self.map_venues = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(map_filters, "filtered_map_venues", return_value=[venue(SALON_B)])
        self.filtered = patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, query):
        return self.client.get(f"{MAP_URL}?{query}")

    def test_off_by_default_the_params_are_ignored_as_before(self):
        response = self.get("latitude=25.2&longitude=55.27&rating=bogus&additional=verified&distance=3")
        self.assertEqual(response.status_code, 200)
        self.filtered.assert_not_called()
        self.assertEqual(response.json()["venues"][0]["id"], str(SALON_A))

    @override_settings(MAP_FILTER_V1=True)
    def test_on_a_plain_call_is_exactly_as_today(self):
        response = self.get(
            "latitude=25.2&longitude=55.27&latitudeDelta=0.1&longitudeDelta=0.1&zoom=12&limit=100"
        )
        self.assertEqual(response.status_code, 200)
        self.filtered.assert_not_called()
        self.map_venues.assert_called_once_with(
            latitude=25.2, longitude=55.27, latitude_delta=0.1, longitude_delta=0.1,
            category=None, radius_km=None, limit=100.0,
        )
        self.assertEqual(response.json(), {
            "mode": "markers", "count": 1,
            "venues": [{"id": str(SALON_A), "lat": 25.2, "lng": 55.27, "rating": 4.5,
                        "salon_profile_image": None}],
        })

    @override_settings(MAP_FILTER_V1=True)
    def test_on_the_picks_filter_and_the_response_keeps_its_shape(self):
        response = self.get(
            "latitude=25.2&longitude=55.27&rating=4&availability=today&additional=open_now"
            f"&service_categories={HAIR}&distance=10&origin_latitude=25.19&origin_longitude=55.27"
        )
        self.assertEqual(response.status_code, 200)
        self.map_venues.assert_not_called()
        map_params, filters = self.filtered.call_args.args
        self.assertEqual(map_params["latitude"], 25.2)
        self.assertEqual(filters, MapFilters(
            service_categories=(HAIR,), rating="4", availability="today",
            distance_km=10.0, origin=(25.19, 55.27), open_now=True,
        ))
        self.assertEqual(self.filtered.call_args.kwargs["now"].tzinfo, dt_timezone.utc)
        self.assertEqual(response.json(), {
            "mode": "markers", "count": 1,
            "venues": [{"id": str(SALON_B), "lat": 25.2, "lng": 55.27, "rating": 4.5,
                        "salon_profile_image": None}],
        })

    @override_settings(MAP_FILTER_V1=True)
    def test_nothing_matches_is_200_and_empty(self):
        self.filtered.return_value = []
        response = self.get("rating=5")
        self.assertEqual((response.status_code, response.json()),
                         (200, {"mode": "markers", "count": 0, "venues": []}))

    @override_settings(MAP_FILTER_V1=True)
    def test_any_everywhere_is_a_plain_call(self):
        response = self.get("rating=any&availability=any&distance=any&service_categories=any&additional=")
        self.assertEqual(response.status_code, 200)
        self.filtered.assert_not_called()
        self.map_venues.assert_called_once()

    @override_settings(MAP_FILTER_V1=True)
    def test_an_id_not_offered_is_the_field_error_envelope(self):
        response = self.get("rating=7")
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["code"], "validation_error")
        self.assertEqual(body["errors"][0]["field"], "rating")
        self.assertEqual(body["errors"][0]["code"], "invalid_choice")
        self.filtered.assert_not_called()

    @override_settings(MAP_FILTER_V1=True)
    def test_distance_without_its_origin_is_refused(self):
        response = self.get("distance=10")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["errors"],
            [{"field": "origin_latitude", "code": "required",
              "message": "Send origin_latitude and origin_longitude with distance."}],
        )

    @override_settings(MAP_FILTER_V1=True)
    def test_the_old_params_keep_their_errors(self):
        response = self.get("lat=25.2")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["errors"][0]["code"], "invalid")

    @override_settings(MAP_FILTER_V1=True)
    def test_who_is_busy_unreadable_is_a_503(self):
        self.filtered.side_effect = AvailabilityUnavailable("BOOKING_DB_NAME is not set")
        response = self.get("availability=today")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["code"], "service_unavailable")
        self.assertEqual(body["errors"][0]["code"], "availability_unavailable")


class DrawerViewTests(SimpleTestCase):
    """GET /me/map/filters through the real URL."""

    def setUp(self):
        patcher = mock.patch.object(map_filters, "category_catalog", return_value=CATALOG)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_route(self):
        self.assertIs(resolve(DRAWER_URL).func.view_class, map_views.MapFiltersView)

    def test_off_by_default_it_is_a_404(self):
        response = self.client.get(DRAWER_URL)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")

    @override_settings(MAP_FILTER_V1=True)
    def test_on_it_is_the_drawer_signed_out_too(self):
        response = self.client.get(DRAWER_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), map_filters.drawer())

    @override_settings(MAP_FILTER_V1=True)
    def test_the_swagger_example_is_what_the_view_answers(self):
        catalog = {
            map_filters.category_option_id(name): {
                "label": name, "category_ids": frozenset(), "salons": salons,
            }
            for name, salons in (("Hair", 2), ("Beard", 1))
        }
        with mock.patch.object(map_filters, "category_catalog", return_value=catalog):
            self.assertEqual(self.client.get(DRAWER_URL).json(), map_views.EXAMPLE)


class GuideTests(SimpleTestCase):
    """docs/MAP_FILTERS_API.md says what the code answers."""

    def setUp(self):
        from django.conf import settings

        with open(settings.BASE_DIR / "docs" / "MAP_FILTERS_API.md") as guide:
            self.text = guide.read()

    def json_blocks(self):
        import json

        return [json.loads(block.split("```", 1)[0])
                for block in self.text.split("```json\n")[1:]]

    def test_the_drawer_example_is_the_view_s_own(self):
        self.assertEqual(self.json_blocks()[0], map_views.EXAMPLE)

    def test_the_error_example_is_the_real_message(self):
        error = self.json_blocks()[1]["errors"][0]
        with self.assertRaises(ParamError) as caught:
            parse_map_filters(q("rating=7"))
        self.assertEqual(
            (error["field"], error["code"], error["message"]),
            (caught.exception.param, caught.exception.code, caught.exception.message),
        )

    def test_the_example_ids_are_the_real_category_ids(self):
        self.assertIn(map_filters.category_option_id("Hair"), self.text)
        self.assertIn(map_filters.category_option_id("Beard"), self.text)

    def test_no_em_dash(self):
        self.assertNotIn("—", self.text)


class OpenApiTests(SimpleTestCase):
    def setUp(self):
        self.schema = SchemaGenerator().get_schema(request=None, public=True)

    def test_the_drawer_is_documented(self):
        operation = self.schema["paths"][DRAWER_URL]["get"]
        self.assertIn("MAP_FILTER_V1", operation["description"])

    def test_the_map_documents_every_param_the_drawer_has(self):
        names = {p["name"] for p in self.schema["paths"][MAP_URL]["get"]["parameters"]}
        self.assertLessEqual(
            {"service_categories", "rating", "availability", "distance",
             "origin_latitude", "origin_longitude", "additional"},
            names,
        )
        # And the ones it took before.
        self.assertLessEqual(
            {"latitude", "longitude", "latitudeDelta", "longitudeDelta", "zoom", "limit", "category"},
            names,
        )


class SelectorTests(SimpleTestCase):
    """The selectors the map's filters added or widened, built and inspected."""

    def test_map_venues_is_its_queryset_capped(self):
        capped = selectors.map_venues(latitude=25.2, longitude=55.27, latitude_delta=0.1,
                                      longitude_delta=0.1, limit=50)
        self.assertEqual((capped.query.low_mark, capped.query.high_mark), (0, 50))
        self.assertEqual(selectors.map_venue_cap(None), selectors.MAP_VENUE_LIMIT)
        self.assertEqual(selectors.map_venue_cap(9999), selectors.MAP_VENUE_LIMIT)

    def test_with_distance_keeps_its_old_name_by_default(self):
        qs = selectors.map_venue_queryset()
        self.assertIn("distance_km", selectors.with_distance(qs, 25.2, 55.27).query.annotations)
        self.assertIn("other", selectors.with_distance(qs, 25.2, 55.27, name="other").query.annotations)

    def test_shift_rows_between_reads_this_branch_s_roster_for_the_days(self):
        qs_rows = mock.MagicMock()
        with mock.patch.object(selectors.Shift.objects, "filter", return_value=qs_rows) as found:
            selectors.shift_rows_between(TENANT, BRANCH, [STYLIST], TUESDAY, date(2026, 11, 9))
        self.assertEqual(found.call_args.kwargs, {
            "tenant_id": TENANT, "roster__branch_id": BRANCH, "staff_member_id__in": [STYLIST],
            "shift_date__gte": TUESDAY, "shift_date__lte": date(2026, 11, 9),
        })
        self.assertEqual(selectors.shift_rows_between(TENANT, BRANCH, [], TUESDAY, TUESDAY), [])

    def test_manual_states_between_reads_the_hand_set_days(self):
        with mock.patch.object(selectors.StorefrontStatus.objects, "filter") as found:
            found.return_value.values_list.return_value = [(TUESDAY, "CLOSED")]
            self.assertEqual(
                selectors.manual_states_between(SALON_A, TUESDAY, date(2026, 11, 9)),
                {TUESDAY: "CLOSED"},
            )
        self.assertEqual(found.call_args.kwargs, {
            "storefront_id": SALON_A, "source": "MANUAL",
            "applies_on__gte": TUESDAY, "applies_on__lte": date(2026, 11, 9),
        })

    def test_menu_timing_rows_is_the_tab_s_menu_with_its_timing(self):
        salon = types.SimpleNamespace(tenant_id=TENANT, branch_id=BRANCH)
        with mock.patch.object(selectors, "salon_services") as menu:
            menu.return_value.annotate.return_value.values.return_value = [{"id": CUT}]
            self.assertEqual(selectors.menu_timing_rows(salon), [{"id": CUT}])
        menu.assert_called_once_with(salon)
        self.assertIn("stage_minutes", menu.return_value.annotate.call_args.kwargs)
        self.assertEqual(
            menu.return_value.annotate.return_value.values.call_args.args,
            ("id", "category_0_id", "duration_minutes", "stage_minutes", "lead_time_minutes"),
        )

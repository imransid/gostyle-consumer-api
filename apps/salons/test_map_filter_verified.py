"""
Verified salons (MAP_FILTER_VERIFIED_V1, the audit's D2, Sarwar 2026-11-04):
our staff's yes/no per salon (models.SalonDiscoveryFlag), the drawer chip,
verified=true on every Map, Search and Results route, and is_verified in the
pins, the cards and the results.

salon_discovery_flag is our own table, so the rule runs for real against it
(on salon_country, our other table keyed by a uuid). The platform's tables
do not exist in the test database: their querysets are inspected, not run.
"""

import types
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from apps.salons import (
    map_filters,
    search_results,
    search_suggest,
    selectors,
    service_search_views,
    verified,
    views,
)
from apps.salons.admin import SalonDiscoveryFlagAdmin
from apps.salons.map_filters import MapFilters, parse_map_filters
from apps.salons.models import SalonCountry, SalonDiscoveryFlag
from apps.salons.params import ParamError, parse_map
from apps.salons.search_results import SALON_SORTS, parse_search
from apps.salons.serializers import MapVenueSerializer, SalonCardSerializer

ON = override_settings(MAP_FILTER_VERIFIED_V1=True)
A, B, C = (uuid.UUID(int=n) for n in (101, 102, 103))


def flag_reads(qs):
    """How often the query reads our flag table: 1 is the badge (SELECT), 2 the badge and the filter."""
    return str(qs.query).count('"salon_discovery_flag"')


def selected(qs):
    return set(qs.query.values_select) | set(qs.query.annotation_select)


def card_row(pk, is_verified=None):
    """A card row as the card serializer reads it, with every field it touches."""
    row = types.SimpleNamespace(
        pk=pk, id=pk, published_name="Del Capo", branch_name="Marina", category="male",
        logo_url=None, avg_rating=None, review_count=0, distance_km=None, has_story=False,
        gallery_urls=[], slug="del-capo", branch_id=uuid.uuid4(), tenant_id=uuid.uuid4(),
        branch_city="Dubai", lat=25.2, lng=55.27, cover_url=None, hijab_certified=False,
        deposit_mode=None, deposit_bps=None, cancel_window_hours=None,
        branch_timezone="Asia/Dubai", published_hours={}, manual_state=None,
    )
    if is_verified is not None:
        row.is_verified = is_verified
    return row


def venue(pk, is_verified=None):
    row = types.SimpleNamespace(pk=pk, id=pk, lat=25.2, lng=55.27, avg_rating=None, photo_url=None)
    if is_verified is not None:
        row.is_verified = is_verified
    return row


# ---------------------------------------------------------------------------
# The rule, for real on our tables
# ---------------------------------------------------------------------------


class RuleTests(TestCase):
    def test_a_salon_with_no_row_or_a_no_is_not_verified(self):
        for branch_id in (A, B, C):
            SalonCountry.objects.create(branch_id=branch_id, lat=0, lng=0, country_code="AE")
        SalonDiscoveryFlag.objects.create(salon_id=A, is_verified=True)
        SalonDiscoveryFlag.objects.create(salon_id=B, is_verified=False)
        # C has no row at all.
        found = dict(SalonCountry.objects.annotate(v=verified.expression()).values_list("branch_id", "v"))
        self.assertEqual(found, {A: True, B: False, C: False})

    def test_wanted_only_true_and_only_with_the_switch(self):
        self.assertFalse(verified.wanted(QueryDict("verified=true")))
        with self.settings(MAP_FILTER_VERIFIED_V1=True):
            for text in ("verified=true", "verified=1", "verified=YES", "verified=on"):
                self.assertTrue(verified.wanted(QueryDict(text)), text)
            for text in ("", "verified=", "verified=false", "verified=0", "verified=maybe"):
                self.assertFalse(verified.wanted(QueryDict(text)), text)

    def test_annotate_off_leaves_the_queryset_alone(self):
        qs = SalonCountry.objects.all()
        self.assertIs(verified.annotate(qs), qs)
        with self.settings(MAP_FILTER_VERIFIED_V1=True):
            self.assertIn("is_verified", verified.annotate(qs).query.annotations)


class SyncTests(TestCase):
    """sync_flags: a row for every public salon, names current, marks kept."""

    def test_rows_added_names_refreshed_marks_kept(self):
        SalonDiscoveryFlag.objects.create(salon_id=A, salon_name="Old Name", is_verified=True)
        SalonDiscoveryFlag.objects.create(salon_id=C, salon_name="Gone Salon", is_verified=True)
        added, renamed = verified.sync_flags({A: "Del Capo", B: "Lush Glow"})
        self.assertEqual((added, renamed), (1, 1))
        rows = {f.salon_id: (f.salon_name, f.is_verified) for f in SalonDiscoveryFlag.objects.all()}
        self.assertEqual(rows, {
            A: ("Del Capo", True),     # the mark survives the rename
            B: ("Lush Glow", False),   # new: not verified
            C: ("Gone Salon", True),   # no longer public: kept as it was
        })

    def test_twice_is_the_same(self):
        verified.sync_flags({A: "Del Capo"})
        self.assertEqual(verified.sync_flags({A: "Del Capo"}), (0, 0))
        self.assertEqual(SalonDiscoveryFlag.objects.count(), 1)


class AdminTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(phone="+971500000901", is_staff=True)
        self.page = SalonDiscoveryFlagAdmin(SalonDiscoveryFlag, admin.site)
        self.request = RequestFactory().post("/admin/")
        self.request.user = self.staff
        self.page.message_user = lambda *args, **kwargs: None

    def test_registered_with_our_page(self):
        self.assertIsInstance(admin.site._registry[SalonDiscoveryFlag], SalonDiscoveryFlagAdmin)

    def test_search_by_name_toggle_in_the_list_and_both_actions(self):
        self.assertIn("salon_name", self.page.search_fields)
        self.assertEqual(self.page.list_editable, ("is_verified",))
        self.assertEqual(
            [a.__name__ for a in (self.page.mark_verified, self.page.unmark_verified)],
            ["mark_verified", "unmark_verified"],
        )

    def test_mark_and_unmark_record_who(self):
        SalonDiscoveryFlag.objects.create(salon_id=A, salon_name="Del Capo")
        SalonDiscoveryFlag.objects.create(salon_id=B, salon_name="Lush Glow")
        self.page.mark_verified(self.request, SalonDiscoveryFlag.objects.all())
        self.assertEqual(
            set(SalonDiscoveryFlag.objects.values_list("is_verified", "updated_by")), {(True, self.staff.pk)}
        )
        self.page.unmark_verified(self.request, SalonDiscoveryFlag.objects.filter(salon_id=A))
        self.assertFalse(SalonDiscoveryFlag.objects.get(salon_id=A).is_verified)
        self.assertTrue(SalonDiscoveryFlag.objects.get(salon_id=B).is_verified)

    def test_saving_one_records_who(self):
        flag = SalonDiscoveryFlag.objects.create(salon_id=A, salon_name="Del Capo")
        flag.is_verified = True
        self.page.save_model(self.request, flag, None, True)
        flag.refresh_from_db()
        self.assertEqual((flag.is_verified, flag.updated_by_id), (True, self.staff.pk))

    def test_the_list_syncs_first_and_rows_cannot_be_added_or_deleted(self):
        with mock.patch.object(verified, "sync_flags") as sync, \
                mock.patch.object(admin.ModelAdmin, "changelist_view", return_value="page"):
            self.assertEqual(self.page.changelist_view(self.request), "page")
        sync.assert_called_once_with()
        self.assertFalse(self.page.has_add_permission(self.request))
        self.assertFalse(self.page.has_delete_permission(self.request))


# ---------------------------------------------------------------------------
# Switch off: exactly as before
# ---------------------------------------------------------------------------


def discover_queryset(text):
    view = views.SalonDiscoveryListView()
    view.request = Request(APIRequestFactory().get(f"/api/v1/discover?{text}"))
    view.format_kwarg = None
    return view.get_queryset()


class OffTests(SimpleTestCase):
    def test_no_annotation_anywhere(self):
        self.assertNotIn("is_verified", selectors.discoverable_salons().query.annotations)
        self.assertNotIn("is_verified", selectors.map_venue_queryset().query.annotations)
        self.assertNotIn("is_verified", selected(selectors.search_salons()))
        self.assertNotIn("is_verified", selected(search_suggest.salon_query("iron")))
        self.assertEqual(flag_reads(selectors.discoverable_salons()), 0)

    def test_no_chip_and_additional_verified_is_still_refused(self):
        with mock.patch.object(map_filters, "category_catalog", return_value={}):
            sections = {s["key"]: s for s in map_filters.drawer()["sections"]}
        self.assertEqual([o["id"] for o in sections["additional"]["options"]], ["open_now"])
        with self.assertRaises(ParamError):
            parse_map_filters(QueryDict("additional=verified"))
        with self.assertRaises(ParamError):
            parse_search(QueryDict("additional=verified"), SALON_SORTS, "rating")

    def test_verified_true_is_ignored_everywhere(self):
        self.assertEqual(parse_map_filters(QueryDict("verified=true")), MapFilters())
        self.assertFalse(parse_search(QueryDict("verified=true"), SALON_SORTS, "rating").verified)
        self.assertEqual(flag_reads(discover_queryset("verified=true")), 0)

    def test_no_key_in_any_payload(self):
        self.assertNotIn("is_verified", SalonCardSerializer(card_row(A, True)).data)
        self.assertNotIn("is_verified", MapVenueSerializer(venue(A, True)).data)
        result = search_results.service_result(
            types.SimpleNamespace(
                salon={"id": A, "tenant_id": A, "branch_id": A, "published_name": "X", "branch_name": "X",
                       "category": None, "avg_rating": None, "logo_url": None, "distance_km": None,
                       "is_verified": True},
                service={"id": B, "name": "Cut", "description": None, "price_minor": 100, "currency": "AED",
                         "duration_minutes": 30, "category_0_id": None, "created_at": None},
                branch_price_minor=None, branch_currency=None,
            ),
            {}, {},
        )
        self.assertNotIn("is_verified", result["salon"])


# ---------------------------------------------------------------------------
# Switch on
# ---------------------------------------------------------------------------


@ON
class OnQueryTests(SimpleTestCase):
    def test_the_mark_rides_in_every_salon_query(self):
        self.assertIn("is_verified", selectors.discoverable_salons().query.annotations)
        self.assertIn("is_verified", selectors.map_venue_queryset().query.annotations)
        self.assertIn("is_verified", selected(selectors.search_salons()))
        self.assertIn("is_verified", selected(search_suggest.salon_query("iron")))

    def test_one_query_the_mark_is_a_subquery_not_a_lookup_per_salon(self):
        sql = str(selectors.discoverable_salons().query)
        self.assertIn('"salon_discovery_flag"', sql)
        self.assertIn("EXISTS", sql)

    def test_the_drawer_offers_the_chip(self):
        with mock.patch.object(map_filters, "category_catalog", return_value={}):
            sections = {s["key"]: s for s in map_filters.drawer()["sections"]}
        self.assertEqual(
            sections["additional"]["options"],
            [{"id": "open_now", "label": "Open now"}, {"id": "verified", "label": "Verified salons"}],
        )
        self.assertEqual(sections["additional"]["default"], ["open_now"])

    def test_both_spellings_ask_for_it(self):
        for text in ("additional=verified", "verified=true", "additional=open_now,verified"):
            self.assertTrue(parse_map_filters(QueryDict(text)).verified, text)
            self.assertTrue(parse_search(QueryDict(text), SALON_SORTS, "rating").verified, text)
        for text in ("", "verified=false", "verified=maybe"):
            self.assertFalse(parse_map_filters(QueryDict(text)).verified, text)
            self.assertFalse(parse_search(QueryDict(text), SALON_SORTS, "rating").verified, text)

    def test_the_map_filters_on_it(self):
        with mock.patch.object(map_filters, "category_catalog", return_value={}):
            qs, _ = map_filters.filtered_queryset(parse_map(QueryDict("")), MapFilters(verified=True))
        self.assertEqual(flag_reads(qs), 2)
        self.assertTrue(MapFilters(verified=True).active)

    def test_the_search_filters_on_it(self):
        with mock.patch.object(map_filters, "category_catalog", return_value={}):
            qs, _ = search_results.salon_queryset(parse_search(QueryDict("verified=true"), SALON_SORTS, "rating"))
            plain, _ = search_results.salon_queryset(parse_search(QueryDict(""), SALON_SORTS, "rating"))
        self.assertEqual((flag_reads(qs), flag_reads(plain)), (2, 1))

    def test_discover_filters_on_it(self):
        self.assertEqual(flag_reads(discover_queryset("verified=true")), 2)
        self.assertEqual(flag_reads(discover_queryset("verified=false")), 1)

    def test_the_suggestions_filter_on_it(self):
        self.assertEqual(flag_reads(search_suggest.salon_query("iron", verified_only=True)), 2)
        self.assertEqual(flag_reads(search_suggest.salon_query("iron")), 1)


@ON
class OnPayloadTests(SimpleTestCase):
    def test_the_card_says_it_after_hijab_certified(self):
        data = SalonCardSerializer(card_row(A, True)).data
        keys = list(data)
        self.assertEqual(keys[keys.index("hijab_certified") + 1], "is_verified")
        self.assertIs(data["is_verified"], True)
        self.assertIs(SalonCardSerializer(card_row(B, False)).data["is_verified"], False)
        # No annotation read (no row): not verified.
        self.assertIs(SalonCardSerializer(card_row(C)).data["is_verified"], False)

    def test_the_pin_says_it(self):
        self.assertIs(MapVenueSerializer(venue(A, True)).data["is_verified"], True)
        self.assertIs(MapVenueSerializer(venue(B)).data["is_verified"], False)

    def test_the_favourites_list_keeps_its_own_fields(self):
        from apps.salons.serializers import FavouriteSalonSerializer

        self.assertNotIn("is_verified", FavouriteSalonSerializer(card_row(A, True)).data)

    def test_the_services_tab_s_salon_says_it(self):
        result = search_results.service_result(
            types.SimpleNamespace(
                salon={"id": A, "tenant_id": A, "branch_id": A, "published_name": "X", "branch_name": "X",
                       "category": None, "avg_rating": None, "logo_url": None, "distance_km": None,
                       "is_verified": True},
                service={"id": B, "name": "Cut", "description": None, "price_minor": 100, "currency": "AED",
                         "duration_minutes": 30, "category_0_id": None, "created_at": None},
                branch_price_minor=None, branch_currency=None,
            ),
            {}, {},
        )
        self.assertIs(result["salon"]["is_verified"], True)

    def test_a_suggested_salon_says_it(self):
        rows = [{"id": A, "published_name": "Iron Razor", "branch_name": "B", "logo_url": None, "is_verified": True}]

        class Rows(list):
            def __getitem__(self, item):
                return list(self)[item]

        with mock.patch.object(search_suggest, "salon_query", return_value=Rows(rows)):
            self.assertIs(search_suggest.salons("iron")[0]["is_verified"], True)


SALON_DICT = {
    "tenant_id": uuid.UUID(int=7), "published_name": "Del Capo", "branch_name": "B", "category": "male",
    "avg_rating": None, "review_count": 0,
}


@ON
@override_settings(SEARCH_POPULAR_V1=True)
class ServicesRouteTests(SimpleTestCase):
    """GET /services across salons: verified=true keeps verified salons' services."""

    def call(self, text=""):
        salons = [
            {**SALON_DICT, "id": A, "branch_id": A, "is_verified": True},
            {**SALON_DICT, "id": B, "branch_id": B, "tenant_id": uuid.UUID(int=8), "is_verified": False},
        ]
        services = [
            {"id": C, "tenant_id": t, "name": "Cut", "description": None, "price_minor": 100,
             "duration_minutes": 30, "category_0_id": None,
             "created_at": datetime(2026, 11, 1, tzinfo=dt_timezone.utc)}
            for t in (uuid.UUID(int=7), uuid.UUID(int=8))
        ]
        with ExitStack() as stack:
            def patch(name, **kw):
                return stack.enter_context(mock.patch.object(service_search_views, name, **kw))
            patch("search_salons", return_value=salons)
            patch("categories_for_tenants", return_value={})
            patch("menu_services", side_effect=lambda tenants, **kw: [s for s in services if s["tenant_id"] in tenants])
            patch("branch_service_rows", return_value={})
            patch("service_booking_counts", return_value={})
            return self.client.get(f"/api/v1/services?{text}").json()

    def test_verified_true_keeps_the_verified_salons_services(self):
        body = self.call("verified=true")
        self.assertEqual([r["salon"]["id"] for r in body["results"]], [str(A)])
        self.assertIs(body["results"][0]["salon"]["is_verified"], True)

    def test_without_it_both_and_each_says_its_mark(self):
        body = self.call()
        self.assertEqual({r["salon"]["id"]: r["salon"]["is_verified"] for r in body["results"]},
                         {str(A): True, str(B): False})


# ---------------------------------------------------------------------------
# N+1: the lists cost no query per salon
# ---------------------------------------------------------------------------


@ON
class NoQueryPerSalonTests(TestCase):
    """The mark is read off the list's own rows: 20 salons, zero extra queries."""

    def test_serializing_twenty_cards_and_pins_runs_no_query(self):
        cards = [card_row(uuid.uuid4(), i % 2 == 0) for i in range(20)]
        pins = [venue(uuid.uuid4(), i % 2 == 0) for i in range(20)]
        with self.assertNumQueries(0):
            data = SalonCardSerializer(cards, many=True).data
            pin_data = MapVenueSerializer(pins, many=True).data
        self.assertEqual(sum(row["is_verified"] for row in data), 10)
        self.assertEqual(sum(row["is_verified"] for row in pin_data), 10)

    @override_settings(MAP_FILTER_V1=True)
    def test_the_map_list_runs_no_query_beyond_its_own(self):
        pins = [venue(uuid.uuid4(), True) for _ in range(20)]
        with mock.patch.object(views, "map_venues", return_value=pins), self.assertNumQueries(0):
            body = self.client.get("/api/v1/discover/map?latitude=25.2&longitude=55.27").json()
        self.assertEqual(len(body["venues"]), 20)
        self.assertTrue(all(v["is_verified"] for v in body["venues"]))

    @override_settings(SEARCH_RESULTS_V1=True)
    def test_the_salons_tab_runs_no_query_beyond_its_own(self):
        cards = [card_row(uuid.uuid4(), True) for _ in range(20)]
        with mock.patch.object(search_results, "salons", return_value=cards), self.assertNumQueries(0):
            body = self.client.get("/api/v1/search/salons?verified=true&pageSize=20").json()
        self.assertEqual(body["count"], 20)
        self.assertTrue(all(r["is_verified"] for r in body["results"]))

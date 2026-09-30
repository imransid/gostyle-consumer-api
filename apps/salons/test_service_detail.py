"""
GET /api/v1/salon/<salon_id>/service/<service_id>, the Service Detail screen
(docs/service-detail-fe-contract.md, docs/SERVICE_DETAIL_AUDIT.md section 7).

S1: the route, the 404s and auth. Every request goes through the real URL
(the Django test client), because the point of S1 is what the URL layer does
with a bad id: our JSON 404, never Django's HTML page.

S2: the core fields, and the Services tab pinned before its price and chip
code moved to menu.py, shared with this screen.

S3: hero_url, gallery (at most 5) and gallery_count.

The selectors are mocked; no database.
"""

import types
import uuid
from decimal import Decimal
from unittest import mock

from django.db.models import Q
from django.test import SimpleTestCase
from django.urls import resolve, reverse
from rest_framework.test import APIRequestFactory

from apps.salons import menu, selectors
from apps.salons.service_detail import GALLERY_MAX, core_fields, photos
from apps.salons.service_detail_views import (
    NO_SALON,
    NO_SERVICE,
    SalonServiceDetailView,
    path_uuid,
)
from apps.salons.views import SalonServicesView

SALON = uuid.UUID("33333333-3333-3333-3333-333333333333")
TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
SERVICE = uuid.UUID("66666666-6666-6666-6666-666666666660")


def url(salon=SALON, service=SERVICE):
    return f"/api/v1/salon/{salon}/service/{service}"


class RouteTests(SimpleTestCase):
    """What the endpoint answers, through the URL layer."""

    def get(self, path, *, salon=True, service=True, **headers):
        found_salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT) if salon else None
        if service is True:
            service = service_row()
        with mock.patch(
            "apps.salons.service_detail_views.salon_profile", return_value=found_salon
        ) as salon_lookup, mock.patch(
            "apps.salons.service_detail_views.service_for_salon",
            return_value=service or None,
        ) as service_lookup, mock.patch(
            "apps.salons.service_detail_views.salon_categories", return_value=CATEGORIES
        ) as categories_lookup, mock.patch(
            "apps.salons.service_detail_views.service_photo_urls", return_value=([], [])
        ):
            self.salon_lookup = salon_lookup
            self.service_lookup = service_lookup
            self.categories_lookup = categories_lookup
            return self.client.get(path, **headers)

    def assert_our_404(self, response, detail):
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        body = response.json()
        self.assertEqual(body["code"], "not_found")
        self.assertEqual(body["detail"], detail)
        self.assertEqual(
            body["errors"], [{"field": None, "code": "not_found", "message": detail}]
        )

    def test_a_service_of_this_salon_answers_200_without_a_token(self):
        response = self.get(url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], str(SERVICE))
        self.assertEqual(response.json()["salon_id"], str(SALON))

    def test_the_selectors_get_the_parsed_ids(self):
        self.get(url())
        self.salon_lookup.assert_called_once_with(SALON)
        (salon, service_id), _ = self.service_lookup.call_args
        self.assertEqual(salon.id, SALON)
        self.assertEqual(service_id, SERVICE)

    def test_a_salon_id_that_is_not_a_uuid_is_our_json_404(self):
        response = self.get(url(salon="not-a-uuid"))
        self.assert_our_404(response, NO_SALON)
        self.salon_lookup.assert_not_called()
        self.service_lookup.assert_not_called()

    def test_a_service_id_that_is_not_a_uuid_is_our_json_404(self):
        response = self.get(url(service="also-not"))
        self.assert_our_404(response, NO_SERVICE)
        self.service_lookup.assert_not_called()

    def test_both_ids_bad_names_the_salon(self):
        self.assert_our_404(self.get(url(salon="x", service="y")), NO_SALON)

    def test_an_unknown_salon_is_404(self):
        self.assert_our_404(self.get(url(), salon=False), NO_SALON)
        self.service_lookup.assert_not_called()

    def test_a_service_not_found_at_this_salon_is_404(self):
        # Another tenant's service, a service never on sale, or no such id:
        # the selector answers None for all three (see ServiceForSalonTests).
        self.assert_our_404(self.get(url(), service=False), NO_SERVICE)

    def test_an_uppercase_uuid_is_accepted(self):
        response = self.get(url(salon=str(SALON).upper(), service=str(SERVICE).upper()))
        self.assertEqual(response.status_code, 200)
        self.salon_lookup.assert_called_once_with(SALON)

    def test_other_uuid_spellings_are_404(self):
        for spelling in (
            SALON.hex,                     # 32 hex digits, no dashes
            "{%s}" % SALON,                # braces
            "urn:uuid:%s" % SALON,         # a URN
            str(SALON)[:-1],               # one digit short
            str(SALON)[:-1] + "g",         # not hex
        ):
            with self.subTest(spelling=spelling):
                self.assert_our_404(self.get(url(salon=spelling)), NO_SALON)

    def test_a_bad_token_is_401_even_on_this_public_route(self):
        response = self.get(url(), HTTP_AUTHORIZATION="Bearer not.a.token")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "not_authenticated")
        self.salon_lookup.assert_not_called()

    def test_only_get(self):
        with mock.patch("apps.salons.service_detail_views.salon_profile"):
            response = self.client.post(url())
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["code"], "method_not_allowed")


class UrlTests(SimpleTestCase):
    """The new route next to the old ones."""

    def test_the_route_has_a_name(self):
        self.assertEqual(
            reverse("salon-service-detail", kwargs={"salon_id": SALON, "service_id": SERVICE}),
            url(),
        )

    def test_any_segment_reaches_the_view(self):
        match = resolve(url(salon="nope", service="nope"))
        self.assertIs(match.func.view_class, SalonServiceDetailView)
        self.assertEqual(match.kwargs, {"salon_id": "nope", "service_id": "nope"})

    def test_the_services_tab_route_is_unchanged(self):
        match = resolve(f"/api/v1/salon/{SALON}/services")
        self.assertIs(match.func.view_class, SalonServicesView)
        self.assertEqual(match.kwargs, {"salon_id": SALON})


class PathUuidTests(SimpleTestCase):
    def test_parses_the_dashed_form_in_any_case(self):
        self.assertEqual(path_uuid(str(SALON)), SALON)
        self.assertEqual(path_uuid(str(SALON).upper()), SALON)

    def test_anything_else_is_none(self):
        for value in (None, "", " %s" % SALON, SALON.hex, "{%s}" % SALON, "not-a-uuid"):
            with self.subTest(value=value):
                self.assertIsNone(path_uuid(value))


class ServiceForSalonTests(SimpleTestCase):
    """`selectors.service_for_salon`: which rows the detail screen may open."""

    def lookup(self):
        salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT)
        with mock.patch.object(selectors, "Service") as service:
            first = service.objects.filter.return_value.filter.return_value.first
            first.return_value = "row"
            answer = selectors.service_for_salon(salon, SERVICE)
        return service, answer

    def test_only_this_salon_s_tenant(self):
        # Exactly the id and the tenant: no status, deleted_at or online
        # booking filter, so a service pulled from sale is still found. No
        # branch filter either: every branch of the tenant sells one menu.
        service, answer = self.lookup()
        service.objects.filter.assert_called_once_with(id=SERVICE, tenant_id=TENANT)
        self.assertEqual(answer, "row")

    def test_only_a_service_that_was_on_sale_once(self):
        service, _ = self.lookup()
        service.objects.filter.return_value.filter.assert_called_once_with(
            Q(published_version__isnull=False)
            | Q(status__in=("PUBLISHED", "HIDDEN", "ARCHIVED"))
        )

    def test_without_a_version_only_the_three_draft_statuses_are_404(self):
        # Rafa, 2026-09-30: an old service with no version must never 404 just
        # because it was hidden or archived. These are the platform's six.
        opens = set(selectors.DETAIL_STATUSES_WITHOUT_VERSION)
        self.assertEqual(opens, {"PUBLISHED", "HIDDEN", "ARCHIVED"})
        for status in ("DRAFT", "PENDING_REVISION", "REVISION_REQUESTED"):
            with self.subTest(status=status):
                self.assertNotIn(status, opens)


# ---------------------------------------------------------------------------
# S2 fixtures: a tenant's categories and service rows, as the selectors give
# them.
# ---------------------------------------------------------------------------

PARENT = uuid.UUID("77777777-7777-7777-7777-777777777771")
FADES = uuid.UUID("77777777-7777-7777-7777-777777777772")
BEARD = uuid.UUID("77777777-7777-7777-7777-777777777773")
ORPHAN = uuid.UUID("77777777-7777-7777-7777-777777777774")
GONE = uuid.UUID("77777777-7777-7777-7777-7777777777ff")

CATEGORIES = {
    PARENT: {"id": PARENT, "name_en": "Haircut & Styling", "slug": None,
             "icon": "scissors", "parent_id": None, "sort_order": 0},
    FADES: {"id": FADES, "name_en": "Fades", "slug": None,
            "icon": None, "parent_id": PARENT, "sort_order": 1},
    BEARD: {"id": BEARD, "name_en": "Beard", "slug": None,
            "icon": "beard", "parent_id": None, "sort_order": 2},
    # Its parent was deleted, so it is not in the dict.
    ORPHAN: {"id": ORPHAN, "name_en": "Orphan", "slug": None,
             "icon": None, "parent_id": GONE, "sort_order": 3},
}


def service_row(service_id=SERVICE, **overrides):
    """A service row: the columns S2 reads."""
    return types.SimpleNamespace(**{
        "id": service_id,
        "tenant_id": TENANT,
        "name": "Signature Fade",
        "description": "Skin fade with a hot towel finish.",
        "price_minor": 12000,
        "duration_minutes": 45,
        "category_0_id": FADES,
        "status": "PUBLISHED",
        "deleted_at": None,
        "online_booking_enabled": True,
        "published_version": 1,
        **overrides,
    })


class ServicesTabTests(SimpleTestCase):
    """
    GET /salon/<id>/services, pinned BEFORE its price and chip code moved to
    shared helpers (S2), so the move provably changes nothing on the tab.
    """

    ROWS = [
        service_row(uuid.UUID(int=1), name="A Fade", category_0_id=FADES),
        service_row(uuid.UUID(int=2), name="B Beard", category_0_id=BEARD,
                    price_minor=8050, description=None, duration_minutes=20),
        service_row(uuid.UUID(int=3), name="C Loose", category_0_id=None, price_minor=1),
        service_row(uuid.UUID(int=4), name="D Orphan", category_0_id=ORPHAN),
        service_row(uuid.UUID(int=5), name="E Fade", category_0_id=FADES, price_minor=15000),
        service_row(uuid.UUID(int=6), name="F Deleted", category_0_id=GONE),
    ]

    def get(self, rows):
        salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT)
        with mock.patch("apps.salons.views.salon_profile", return_value=salon), \
                mock.patch("apps.salons.views.salon_categories", return_value=CATEGORIES), \
                mock.patch("apps.salons.views.salon_services", return_value=list(rows)):
            return SalonServicesView.as_view()(
                APIRequestFactory().get(f"/api/v1/salon/{SALON}/services"), salon_id=SALON
            )

    @staticmethod
    def line(row):
        return {
            "id": str(row.id), "name": row.name, "description": row.description,
            "price": Decimal(row.price_minor) / 100,
            "duration_min": row.duration_minutes, "duration_max": row.duration_minutes,
        }

    def test_chips_are_the_parents_in_first_seen_order_with_one_other(self):
        data = self.get(self.ROWS).data
        self.assertEqual(data["service_categories"], [
            {"id": "all", "label": "All"},
            {"id": str(PARENT), "label": "Haircut & Styling", "icon": "scissors"},
            {"id": str(BEARD), "label": "Beard", "icon": "beard"},
            {"id": "other", "label": "Other", "icon": None},
            {"id": str(ORPHAN), "label": "Orphan", "icon": None},
        ])

    def test_groups_are_the_own_categories_filed_under_their_chip(self):
        rows = self.ROWS
        data = self.get(rows).data
        self.assertEqual(data["service_groups"], [
            {"id": str(FADES), "category_id": str(PARENT), "name": "Fades",
             "services": [self.line(rows[0]), self.line(rows[4])]},
            {"id": str(BEARD), "category_id": str(BEARD), "name": "Beard",
             "services": [self.line(rows[1])]},
            {"id": "other", "category_id": "other", "name": "Other",
             "services": [self.line(rows[2])]},
            {"id": str(ORPHAN), "category_id": str(ORPHAN), "name": "Orphan",
             "services": [self.line(rows[3])]},
            {"id": "other", "category_id": "other", "name": "Other",
             "services": [self.line(rows[5])]},
        ])

    def test_a_branch_price_wins_when_the_selector_read_one(self):
        row = service_row(branch_price_minor=9900)
        line = self.get([row]).data["service_groups"][0]["services"][0]
        self.assertEqual(line["price"], Decimal("99.00"))

    def test_no_branch_price_falls_back_to_the_service_price(self):
        row = service_row(branch_price_minor=None)
        line = self.get([row]).data["service_groups"][0]["services"][0]
        self.assertEqual(line["price"], Decimal("120.00"))


class CoreFieldsTests(SimpleTestCase):
    """S2: name, description, price, durations, category and is_active."""

    def get(self, row):
        salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT)
        with mock.patch("apps.salons.service_detail_views.salon_profile", return_value=salon), \
                mock.patch("apps.salons.service_detail_views.service_for_salon", return_value=row), \
                mock.patch("apps.salons.service_detail_views.salon_categories",
                           return_value=CATEGORIES) as categories, \
                mock.patch("apps.salons.service_detail_views.service_photo_urls",
                           return_value=([], [])):
            self.categories_lookup = categories
            response = self.client.get(url(service=row.id))
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_the_whole_body(self):
        self.assertEqual(self.get(service_row()), {
            "id": str(SERVICE),
            "salon_id": str(SALON),
            "name": "Signature Fade",
            "description": "Skin fade with a hot towel finish.",
            "price": 120.0,
            "duration_min": 45,
            "duration_max": 45,
            "category": {"id": str(PARENT), "label": "Haircut & Styling"},
            "is_active": True,
            "hero_url": None,
            "gallery": [],
            "gallery_count": 0,
        })

    def test_categories_are_read_for_the_salon_s_tenant(self):
        self.get(service_row())
        self.categories_lookup.assert_called_once_with(TENANT)

    def test_the_price_is_the_tab_s_number_for_the_same_row(self):
        salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT)
        for price_minor, branch in [(12000, None), (8050, None), (1, None),
                                    (99999, None), (12000, 9900), (12000, 1)]:
            row = service_row(price_minor=price_minor, branch_price_minor=branch)
            with self.subTest(price_minor=price_minor, branch=branch):
                tab = SalonServicesView._service(row)["price"]
                self.assertEqual(core_fields(salon, row, CATEGORIES)["price"], tab)
                self.assertEqual(self.get(row)["price"], float(tab))

    def test_the_price_is_before_vat_in_major_units(self):
        self.assertEqual(self.get(service_row(price_minor=18050))["price"], 180.5)

    def test_duration_is_the_same_number_twice(self):
        body = self.get(service_row(duration_minutes=30))
        self.assertEqual((body["duration_min"], body["duration_max"]), (30, 30))

    def test_no_description_is_null(self):
        self.assertIsNone(self.get(service_row(description=None))["description"])

    def test_a_pulled_service_answers_200_with_is_active_false(self):
        for change in (
            {"status": "HIDDEN"},
            {"status": "ARCHIVED"},
            {"status": "DRAFT"},  # a published service sent back to draft
            {"deleted_at": "2026-09-01T00:00:00Z"},
            {"online_booking_enabled": False},
        ):
            with self.subTest(**change):
                self.assertIs(self.get(service_row(**change))["is_active"], False)

    def test_the_category_is_the_tab_s_chip(self):
        cases = [
            (FADES, {"id": str(PARENT), "label": "Haircut & Styling"}),   # its parent
            (PARENT, {"id": str(PARENT), "label": "Haircut & Styling"}),  # top level
            (BEARD, {"id": str(BEARD), "label": "Beard"}),
            (ORPHAN, {"id": str(ORPHAN), "label": "Orphan"}),             # parent deleted
            (None, {"id": "other", "label": "Other"}),                    # no category
            (GONE, {"id": "other", "label": "Other"}),                    # category deleted
        ]
        for category_id, chip in cases:
            with self.subTest(category_id=category_id):
                self.assertEqual(self.get(service_row(category_0_id=category_id))["category"], chip)

    def test_the_category_matches_the_tab_s_group_chip(self):
        # The id the tab files this service under is the id the screen gets.
        for category_id in (FADES, PARENT, BEARD, ORPHAN, None, GONE):
            with self.subTest(category_id=category_id):
                row = service_row(category_0_id=category_id)
                tab = ServicesTabTests().get([row]).data["service_groups"][0]["category_id"]
                self.assertEqual(self.get(row)["category"]["id"], tab)


class MenuHelperTests(SimpleTestCase):
    """menu.py: the three things the tab and the detail share."""

    def test_service_price(self):
        self.assertEqual(menu.service_price(service_row(price_minor=12000)), Decimal("120.00"))
        self.assertEqual(
            menu.service_price(service_row(price_minor=12000, branch_price_minor=9900)),
            Decimal("99.00"),
        )
        self.assertEqual(
            menu.service_price(service_row(price_minor=12000, branch_price_minor=None)),
            Decimal("120.00"),
        )

    def test_a_zero_branch_price_falls_back_to_the_service_price(self):
        # As the tab has always done (`or`). Pinned here so S7 (branch
        # prices) decides it on purpose rather than by accident.
        self.assertEqual(
            menu.service_price(service_row(price_minor=12000, branch_price_minor=0)),
            Decimal("120.00"),
        )

    def test_category_chip_is_a_fresh_dict(self):
        chip = menu.category_chip({}, None)
        chip["label"] = "changed"
        self.assertEqual(menu.category_chip({}, None)["label"], "Other")

    def test_is_on_menu_needs_all_three_rules(self):
        self.assertTrue(menu.is_on_menu(service_row()))
        for change in ({"status": "HIDDEN"}, {"deleted_at": "x"},
                       {"online_booking_enabled": False}):
            with self.subTest(**change):
                self.assertFalse(menu.is_on_menu(service_row(**change)))

    def test_is_on_menu_is_the_tab_s_filter(self):
        # If salon_services ever filters on something else, is_on_menu (and
        # so is_active here and on services-details) must change with it.
        storefront = types.SimpleNamespace(tenant_id=TENANT, branch_id=uuid.uuid4())
        with mock.patch.object(selectors, "Service") as service:
            selectors.salon_services(storefront)
        service.objects.filter.assert_called_once_with(
            tenant_id=TENANT,
            status="PUBLISHED",
            deleted_at__isnull=True,
            online_booking_enabled=True,
        )


# ---------------------------------------------------------------------------
# S3: photos
# ---------------------------------------------------------------------------

def photo(n, where="own"):
    return f"https://bucket.s3.me-central-1.amazonaws.com/{where}/{n}.jpg"


class PhotosTests(SimpleTestCase):
    """service_detail.photos: the two lists merged into the contract's fields."""

    def test_no_photos(self):
        self.assertEqual(photos([], []), {"hero_url": None, "gallery": [], "gallery_count": 0})

    def test_own_photos_come_first_then_linked(self):
        out = photos([photo(1), photo(2)], [photo(1, "linked")])
        self.assertEqual(out["gallery"], [photo(1), photo(2), photo(1, "linked")])

    def test_the_hero_is_the_first_photo_and_stays_in_the_gallery(self):
        out = photos([photo(1), photo(2)], [])
        self.assertEqual(out["hero_url"], photo(1))
        self.assertEqual(out["gallery"][0], photo(1))

    def test_only_linked_photos_give_the_hero_too(self):
        out = photos([], [photo(7, "linked")])
        self.assertEqual(out, {"hero_url": photo(7, "linked"),
                               "gallery": [photo(7, "linked")], "gallery_count": 1})

    def test_at_most_five_and_the_count_is_all_of_them(self):
        own = [photo(n) for n in range(4)]
        linked = [photo(n, "linked") for n in range(13)]
        out = photos(own, linked)
        self.assertEqual(GALLERY_MAX, 5)
        self.assertEqual(out["gallery"], own + [photo(0, "linked")])
        self.assertEqual(out["gallery_count"], 17)  # "+N" = 17 - 5 = 12

    def test_five_or_fewer_is_the_whole_list(self):
        own = [photo(n) for n in range(5)]
        out = photos(own, [])
        self.assertEqual((out["gallery"], out["gallery_count"]), (own, 5))

    def test_a_url_already_in_the_list_is_left_out_and_not_counted(self):
        out = photos([photo(1), photo(2), photo(1)], [photo(2), photo(3, "linked")])
        self.assertEqual(out["gallery"], [photo(1), photo(2), photo(3, "linked")])
        self.assertEqual(out["gallery_count"], 3)

    def test_a_blank_url_is_left_out(self):
        out = photos(["", photo(1)], [None, photo(2, "linked")])
        self.assertEqual(out["hero_url"], photo(1))
        self.assertEqual(out["gallery_count"], 2)


class ServicePhotoUrlsTests(SimpleTestCase):
    """selectors.service_photo_urls: which rows, in which order."""

    def read(self):
        salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT)
        with mock.patch.object(selectors, "ServiceMedia") as own, \
                mock.patch.object(selectors, "StorefrontMedia") as linked:
            own.objects.filter.return_value.order_by.return_value.values_list.return_value = [photo(1)]
            linked.objects.filter.return_value.order_by.return_value.values_list.return_value = [photo(2, "linked")]
            answer = selectors.service_photo_urls(salon, SERVICE)
        return own, linked, answer

    def test_answers_both_lists_as_lists(self):
        _, _, answer = self.read()
        self.assertEqual(answer, ([photo(1)], [photo(2, "linked")]))

    def test_own_photos_not_deleted_primary_then_sort_order_then_oldest(self):
        own, _, _ = self.read()
        own.objects.filter.assert_called_once_with(
            service_id=SERVICE, tenant_id=TENANT, deleted_at__isnull=True,
        )
        own.objects.filter.return_value.order_by.assert_called_once_with(
            "-is_primary", "sort_order", "created_at", "id",
        )
        own.objects.filter.return_value.order_by.return_value.values_list.assert_called_once_with(
            "url", flat=True,
        )

    def test_linked_photos_this_salon_s_public_approved_gallery_only(self):
        # Not another branch's storefront, not private, not waiting for (or
        # refused) approval, not deleted, not a cover, logo or story.
        _, linked, _ = self.read()
        linked.objects.filter.assert_called_once_with(
            storefront_id=SALON,
            linked_service_id=SERVICE,
            kind="GALLERY",
            is_public=True,
            moderation_status="APPROVED",
            deleted_at__isnull=True,
        )
        linked.objects.filter.return_value.order_by.assert_called_once_with(
            "sort_order", "created_at", "id",
        )


class PhotoFieldsTests(SimpleTestCase):
    """The three photo fields in the answer, through the URL."""

    def get(self, own, linked):
        salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT)
        with mock.patch("apps.salons.service_detail_views.salon_profile", return_value=salon), \
                mock.patch("apps.salons.service_detail_views.service_for_salon",
                           return_value=service_row()), \
                mock.patch("apps.salons.service_detail_views.salon_categories",
                           return_value=CATEGORIES), \
                mock.patch("apps.salons.service_detail_views.service_photo_urls",
                           return_value=(own, linked)) as reader:
            self.reader = reader
            return self.client.get(url()).json()

    def test_the_photos_are_read_for_this_salon_and_service(self):
        self.get([], [])
        (salon, service_id), _ = self.reader.call_args
        self.assertEqual((salon.id, service_id), (SALON, SERVICE))

    def test_seven_photos(self):
        own = [photo(n) for n in range(3)]
        linked = [photo(n, "linked") for n in range(4)]
        body = self.get(own, linked)
        self.assertEqual(body["hero_url"], photo(0))
        self.assertEqual(body["gallery"], own + linked[:2])
        self.assertEqual(body["gallery_count"], 7)

    def test_no_photos(self):
        body = self.get([], [])
        self.assertEqual((body["hero_url"], body["gallery"], body["gallery_count"]),
                         (None, [], 0))

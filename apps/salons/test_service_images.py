"""
SERVICE_IMAGES_V1: `images` on every list of services (docs/SERVICE_IMAGES_API.md).

The rule (service_images.py): the service's own photos, then this salon's
gallery photos tagged with it, blank and repeated URLs left out, at most 4;
two queries for a whole list. Then each list: the Services tab (and the old
GET /services?tenant_id=, which answers with it), GET /services across
salons, the Expert Profile's service_groups and the Packages tab's bundle
services. Off, each list is exactly as before and no photo is read.

The platform's tables are mocked; no database.
"""

import types
import uuid
from contextlib import ExitStack, contextmanager
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.salons import service_images, service_search, views
from apps.salons import test_expert_profile as ep
from apps.salons import test_package_detail as pd
from apps.salons import test_search_popular as sp

ON = {"SERVICE_IMAGES_V1": True}
OFF = {"SERVICE_IMAGES_V1": False}

SALON = uuid.UUID("33333333-3333-3333-3333-333333333333")
OTHER_SALON = uuid.UUID("33333333-3333-3333-3333-3333333333b2")
TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = uuid.UUID("11111111-1111-1111-1111-1111111111ee")
CUT = uuid.UUID("66666666-6666-6666-6666-666666666660")
BEARD = uuid.UUID("66666666-6666-6666-6666-666666666661")


@contextmanager
def photos(own=(), linked=()):
    """
    The two photo tables, mocked. `own`: (service id, tenant id, url) rows of
    service_media; `linked`: (storefront id, service id, url) rows of
    storefront_media. Each in the order the query would give them.
    """
    with mock.patch.object(service_images.ServiceMedia, "objects") as own_read, \
            mock.patch.object(service_images.StorefrontMedia, "objects") as linked_read:
        own_read.filter.return_value.order_by.return_value.values_list.return_value = list(own)
        linked_read.filter.return_value.order_by.return_value.values_list.return_value = list(linked)
        yield own_read, linked_read


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


class MergeTests(SimpleTestCase):

    def test_own_first_then_linked(self):
        self.assertEqual(service_images.merge(["a", "b"], ["c"]), ["a", "b", "c"])

    def test_a_blank_or_repeated_url_is_left_out(self):
        self.assertEqual(
            service_images.merge(["a", "", None, "a"], ["b", "a", "b"]), ["a", "b"]
        )

    def test_at_most_four(self):
        self.assertEqual(
            service_images.merge(["a", "b", "c"], ["d", "e", "f"]), ["a", "b", "c", "d"]
        )
        self.assertEqual(service_images.IMAGES, 4)

    def test_none_is_an_empty_list(self):
        self.assertEqual(service_images.merge([], []), [])


class ImagesForTests(SimpleTestCase):

    def test_nothing_wanted_reads_nothing(self):
        with photos() as (own, linked):
            self.assertEqual(service_images.images_for([]), {})
        own.filter.assert_not_called()
        linked.filter.assert_not_called()

    def test_two_queries_for_many_salons_and_services(self):
        wanted = [(SALON, TENANT, CUT), (SALON, TENANT, BEARD), (OTHER_SALON, OTHER_TENANT, CUT)]
        with photos(
            own=[(CUT, TENANT, "own-cut"), (CUT, OTHER_TENANT, "their-cut")],
            linked=[(SALON, CUT, "gallery-cut"), (OTHER_SALON, BEARD, "theirs-beard")],
        ) as (own, linked):
            found = service_images.images_for(wanted)

        self.assertEqual(found, {
            (SALON, CUT): ["own-cut", "gallery-cut"],
            # A service with no photo is there, empty.
            (SALON, BEARD): [],
            # The same service id, another business's photo, another salon's gallery.
            (OTHER_SALON, CUT): ["their-cut"],
        })
        own.filter.assert_called_once_with(service_id__in={CUT, BEARD}, deleted_at__isnull=True)
        own.filter.return_value.order_by.assert_called_once_with(
            "-is_primary", "sort_order", "created_at", "id"
        )
        linked.filter.assert_called_once_with(
            storefront_id__in={SALON, OTHER_SALON},
            linked_service_id__in={CUT, BEARD},
            kind="GALLERY",
            is_public=True,
            moderation_status="APPROVED",
            deleted_at__isnull=True,
        )
        linked.filter.return_value.order_by.assert_called_once_with("sort_order", "created_at", "id")

    def test_for_salon_is_by_service_id(self):
        salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT)
        with photos(own=[(CUT, TENANT, "own-cut")]):
            self.assertEqual(
                service_images.for_salon(salon, [CUT, BEARD]), {CUT: ["own-cut"], BEARD: []}
            )

    def test_for_rows_reads_the_search_rows(self):
        rows = sp.rows_of(salons=(sp.A1,), services=[sp.MENU[2]])
        with photos(linked=[(sp.SALON_A1, sp.GENTS, "gallery")]):
            self.assertEqual(
                service_images.for_rows(rows), {(sp.SALON_A1, sp.GENTS): ["gallery"]}
            )


# ---------------------------------------------------------------------------
# The Services tab, and the old GET /services?tenant_id= that answers with it
# ---------------------------------------------------------------------------


def tab_line(service, price, images=None):
    line = ep.tab_line(service, price)
    if images is not None:
        line["images"] = images
    return line


class ServicesTab:

    salon = types.SimpleNamespace(id=SALON, tenant_id=TENANT, branch_id=None)

    def tab(self, path=None, own=(), linked=()):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(views, "salon_profile", return_value=self.salon))
            stack.enter_context(mock.patch.object(
                views, "salon_categories", return_value=ep.MENU_CATEGORIES))
            stack.enter_context(mock.patch.object(
                views, "salon_services", return_value=[ep.BUZZ, ep.TRIM, ep.LOOSE]))
            self.own, self.linked = stack.enter_context(photos(own, linked))
            return self.client.get(path or f"/api/v1/salon/{SALON}/services").json()


class ServicesTabTests(ServicesTab, SimpleTestCase):

    @override_settings(**ON)
    def test_on_every_row_has_its_photos_after_duration_max(self):
        body = self.tab(
            own=[(ep.BUZZ.id, TENANT, "buzz-1"), (ep.BUZZ.id, TENANT, "buzz-2")],
            linked=[(SALON, ep.LOOSE.id, "loose-gallery")],
        )
        rows = [row for group in body["service_groups"] for row in group["services"]]
        self.assertEqual(rows, [
            tab_line(ep.BUZZ, 120.0, ["buzz-1", "buzz-2"]),
            tab_line(ep.TRIM, 80.5, []),
            tab_line(ep.LOOSE, 50.0, ["loose-gallery"]),
        ])
        self.assertEqual(list(rows[0])[-2:], ["duration_max", "images"])
        # One read each for the whole menu.
        self.own.filter.assert_called_once()
        self.linked.filter.assert_called_once()
        self.assertEqual(
            self.own.filter.call_args.kwargs["service_id__in"], {ep.BUZZ.id, ep.TRIM.id, ep.LOOSE.id}
        )

    @override_settings(**OFF)
    def test_off_the_rows_as_before_and_no_photo_read(self):
        body = self.tab(own=[(ep.BUZZ.id, TENANT, "buzz-1")])
        rows = [row for group in body["service_groups"] for row in group["services"]]
        self.assertEqual(rows, [
            tab_line(ep.BUZZ, 120.0), tab_line(ep.TRIM, 80.5), tab_line(ep.LOOSE, 50.0),
        ])
        self.own.filter.assert_not_called()
        self.linked.filter.assert_not_called()


class OldMenuRouteTests(ServicesTab, SimpleTestCase):
    """GET /services?tenant_id= answers with the tab, photos and all."""

    def old_menu(self, **kwargs):
        with mock.patch.object(views, "discoverable_salons") as lookup:
            lookup.return_value.filter.return_value.first.return_value = self.salon
            return self.tab(path=f"/api/v1/services?tenant_id={TENANT}", **kwargs)

    @override_settings(SEARCH_POPULAR_V1=True, **ON)
    def test_on(self):
        body = self.old_menu(own=[(ep.TRIM.id, TENANT, "trim")])
        rows = [row for group in body["service_groups"] for row in group["services"]]
        self.assertEqual([row["images"] for row in rows], [[], ["trim"], []])

    @override_settings(SEARCH_POPULAR_V1=True, **OFF)
    def test_off(self):
        body = self.old_menu(own=[(ep.TRIM.id, TENANT, "trim")])
        rows = [row for group in body["service_groups"] for row in group["services"]]
        self.assertTrue(all("images" not in row for row in rows))


# ---------------------------------------------------------------------------
# GET /services across salons
# ---------------------------------------------------------------------------


class ResultTests(SimpleTestCase):

    def test_no_images_no_key(self):
        row = sp.rows_of(salons=(sp.A1,), services=[sp.MENU[2]])[0]
        self.assertNotIn("images", service_search.result(row, sp.CATEGORIES))

    def test_images_after_duration_max_with_or_without_currency(self):
        row = sp.rows_of(salons=(sp.A1,), services=[sp.MENU[2]])[0]
        for with_currency in (False, True):
            with self.subTest(with_currency=with_currency):
                found = service_search.result(
                    row, sp.CATEGORIES, with_currency=with_currency, images=["a"]
                )
                self.assertEqual(found["images"], ["a"])
                keys = list(found)
                self.assertEqual(keys[keys.index("duration_max") + 1], "images")


@override_settings(SEARCH_POPULAR_V1=True)
class ServicesAcrossSalonsTests(sp.ServicesAcrossSalons, SimpleTestCase):

    @override_settings(**ON)
    def test_on_each_result_has_its_salon_s_photos(self):
        with photos(
            own=[(sp.GENTS, sp.TENANT_A, "gents-own")],
            linked=[(sp.SALON_A2, sp.GENTS, "a2-gallery"), (sp.SALON_B, sp.FADE, "fade")],
        ) as (own, linked):
            body = self.call("pageSize=50").json()
        found = {(r["salon"]["id"], r["id"]): r["images"] for r in body["results"]}
        self.assertEqual(found[(str(sp.SALON_A1), str(sp.GENTS))], ["gents-own"])
        self.assertEqual(found[(str(sp.SALON_A2), str(sp.GENTS))], ["gents-own", "a2-gallery"])
        self.assertEqual(found[(str(sp.SALON_B), str(sp.FADE))], ["fade"])
        self.assertEqual(found[(str(sp.SALON_A1), str(sp.ROOT))], [])
        own.filter.assert_called_once()
        linked.filter.assert_called_once()

    @override_settings(**ON)
    def test_on_only_the_page_s_services_are_read(self):
        with photos() as (own, _):
            body = self.call("pageSize=1").json()
        self.assertEqual(
            own.filter.call_args.kwargs["service_id__in"], {uuid.UUID(body["results"][0]["id"])}
        )

    @override_settings(**OFF)
    def test_off_no_key_and_no_photo_read(self):
        with photos(own=[(sp.GENTS, sp.TENANT_A, "gents-own")]) as (own, linked):
            body = self.call().json()
        self.assertTrue(all("images" not in r for r in body["results"]))
        own.filter.assert_not_called()
        linked.filter.assert_not_called()


# ---------------------------------------------------------------------------
# The Expert Profile's service_groups
# ---------------------------------------------------------------------------


class ExpertProfileTests(ep.Seams, SimpleTestCase):

    @override_settings(**ON)
    def test_on_the_rows_have_photos_and_only_this_stylist_s_services_are_read(self):
        with photos(
            own=[(ep.BUZZ.id, ep.TENANT, "buzz")],
            linked=[(ep.SALON, ep.TINT.id, "tint-gallery")],
        ) as (own, _):
            body = self.call(services=ep.MENU, covered=[ep.BUZZ, ep.TINT, ep.LOOSE]).json()
        self.assertEqual(body["service_groups"], [
            ep.group(ep.CUTS, "Precision Cuts", tab_line(ep.BUZZ, 120.0, ["buzz"])),
            ep.group(ep.COLOUR, "Colour", tab_line(ep.TINT, 250.0, ["tint-gallery"])),
            ep.group(None, "Other", tab_line(ep.LOOSE, 50.0, [])),
        ])
        self.assertEqual(
            own.filter.call_args.kwargs["service_id__in"], {ep.BUZZ.id, ep.TINT.id, ep.LOOSE.id}
        )

    @override_settings(**ON)
    def test_on_a_stylist_who_does_nothing_reads_no_photo(self):
        with photos() as (own, _):
            body = self.call(services=ep.MENU, covered=[]).json()
        self.assertEqual(body["service_groups"], [])
        own.filter.assert_not_called()

    @override_settings(**OFF)
    def test_off_as_before(self):
        with photos(own=[(ep.BUZZ.id, ep.TENANT, "buzz")]) as (own, _):
            body = self.call(services=ep.MENU, covered=[ep.BUZZ]).json()
        self.assertEqual(body["service_groups"], [
            ep.group(ep.CUTS, "Precision Cuts", tab_line(ep.BUZZ, 120.0)),
        ])
        own.filter.assert_not_called()


# ---------------------------------------------------------------------------
# The Packages tab's bundle services (PACKAGES_LIVE_RULES_V1)
# ---------------------------------------------------------------------------


@override_settings(PACKAGES_LIVE_RULES_V1=True, **pd.BOOKING_OFF)
class PackagesTabTests(pd.Reads, SimpleTestCase):

    def tab(self, rows, own=(), linked=()):
        found = types.SimpleNamespace(id=pd.SALON, tenant_id=pd.TENANT, branch_id=pd.BRANCH)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(views, "salon_profile", return_value=found))
            stack.enter_context(mock.patch.object(views, "live_salon_packages", return_value=list(rows)))
            self.reads(stack)
            self.own, self.linked = stack.enter_context(photos(own, linked))
            return self.client.get(pd.tab_url()).json()

    @override_settings(**ON)
    def test_on_each_bundle_service_has_photos_between_duration_max_and_quantity(self):
        body = self.tab(
            [pd.package_row()],
            own=[(pd.CUT, pd.TENANT, "cut")],
            linked=[(pd.SALON, pd.TOWEL, "towel-gallery")],
        )
        services = body["bundles"][0]["services"]
        self.assertEqual(
            [(s["id"], s["images"]) for s in services],
            [(str(pd.CUT), ["cut"]), (str(pd.BEARD), []), (str(pd.TOWEL), ["towel-gallery"])],
        )
        self.assertEqual(list(services[0])[-3:], ["duration_max", "images", "quantity"])
        # One read each for the whole tab.
        self.own.filter.assert_called_once()
        self.assertEqual(
            self.own.filter.call_args.kwargs["service_id__in"], {pd.CUT, pd.BEARD, pd.TOWEL}
        )

    @override_settings(**ON)
    def test_on_no_bundle_reads_no_photo(self):
        body = self.tab([])
        self.assertEqual(body["bundles"], [])
        self.own.filter.assert_not_called()

    @override_settings(**OFF)
    def test_off_as_before(self):
        body = self.tab([pd.package_row()], own=[(pd.CUT, pd.TENANT, "cut")])
        services = body["bundles"][0]["services"]
        self.assertTrue(all("images" not in s for s in services))
        self.assertEqual(list(services[0])[-2:], ["duration_max", "quantity"])
        self.own.filter.assert_not_called()


# ---------------------------------------------------------------------------
# GET /search/services keeps its photos, from the same rule
# ---------------------------------------------------------------------------


class SearchServicesTests(SimpleTestCase):

    def test_search_results_reads_the_shared_rule(self):
        from apps.salons import search_results

        rows = sp.rows_of(salons=(sp.A1,), services=[sp.MENU[2]])
        with mock.patch.object(service_images, "for_rows", return_value={"x": 1}) as shared:
            self.assertEqual(search_results.images_for(rows), {"x": 1})
        shared.assert_called_once_with(rows)

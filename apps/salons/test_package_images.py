"""
PACKAGE_IMAGE_V1: `image_url` on every bundle of the Packages tab
(docs/PACKAGE_IMAGES_API.md, the mobile team's salon-services-packages.md).

The rule (package_images.py): the package's own photos
(service_package_media), primary first, then sort order, then oldest, then
id; not deleted, this business's own, never a blank URL; the first one, or
null. One query for the whole tab. Then the tab, on both of its rules
(PACKAGES_LIVE_RULES_V1 on and off). Off, every bundle is exactly as before
and no photo is read.

Also the brief's other asks, pinned where they are already built:
`quantity` on each bundle service, and services-details' `image_url` picked
by the same rule and order as the lists' `images`, so it is their first.

The platform's tables are mocked; no database.
"""

import types
import uuid
from contextlib import ExitStack, contextmanager
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.salons import package_images, search_results, selectors, views
from apps.salons import test_package_detail as pd

ON = {"PACKAGE_IMAGE_V1": True}
OFF = {"PACKAGE_IMAGE_V1": False}

SALON = types.SimpleNamespace(id=pd.SALON, tenant_id=pd.TENANT, branch_id=pd.BRANCH)
PACKAGE_3 = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3")


@contextmanager
def covers(rows=()):
    """
    service_package_media, mocked. `rows`: (package id, url), in the order
    the query would give them.
    """
    with mock.patch.object(package_images.ServicePackageMedia, "objects") as read:
        read.filter.return_value.order_by.return_value.values_list.return_value = list(rows)
        yield read


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


class ForPackagesTests(SimpleTestCase):

    def test_no_package_reads_nothing(self):
        with covers() as read:
            self.assertEqual(package_images.for_packages(SALON, []), {})
        read.filter.assert_not_called()

    def test_one_query_this_business_s_photos_not_deleted_primary_then_sort_order(self):
        with covers() as read:
            package_images.for_packages(SALON, [pd.PACKAGE, pd.PACKAGE_2, pd.PACKAGE])
        read.filter.assert_called_once_with(
            package_id__in={pd.PACKAGE, pd.PACKAGE_2},
            tenant_id=pd.TENANT,
            deleted_at__isnull=True,
        )
        read.filter.return_value.order_by.assert_called_once_with(
            "-is_primary", "sort_order", "created_at", "id"
        )
        read.filter.return_value.order_by.return_value.values_list.assert_called_once_with(
            "package_id", "url"
        )

    def test_the_first_photo_of_each_and_none_without_one(self):
        rows = [
            (pd.PACKAGE, "https://cdn.example/p1-main.jpg"),
            (pd.PACKAGE, "https://cdn.example/p1-second.jpg"),
            (pd.PACKAGE_2, "https://cdn.example/p2.jpg"),
        ]
        with covers(rows):
            found = package_images.for_packages(SALON, [pd.PACKAGE, pd.PACKAGE_2, PACKAGE_3])
        self.assertEqual(found, {
            pd.PACKAGE: "https://cdn.example/p1-main.jpg",
            pd.PACKAGE_2: "https://cdn.example/p2.jpg",
            PACKAGE_3: None,
        })

    def test_a_blank_url_is_no_photo(self):
        rows = [(pd.PACKAGE, ""), (pd.PACKAGE, "https://cdn.example/p1.jpg"), (pd.PACKAGE_2, "")]
        with covers(rows):
            found = package_images.for_packages(SALON, [pd.PACKAGE, pd.PACKAGE_2])
        self.assertEqual(found, {pd.PACKAGE: "https://cdn.example/p1.jpg", pd.PACKAGE_2: None})

    def test_the_switch(self):
        with override_settings(**ON):
            self.assertTrue(package_images.is_on())
        with override_settings(**OFF):
            self.assertFalse(package_images.is_on())


# ---------------------------------------------------------------------------
# The Packages tab on the platform's rules (PACKAGES_LIVE_RULES_V1 on, live)
# ---------------------------------------------------------------------------


@override_settings(PACKAGES_LIVE_RULES_V1=True, SERVICE_IMAGES_V1=False, **pd.BOOKING_OFF)
class LiveTabTests(pd.Reads, SimpleTestCase):

    def tab(self, rows, photos=()):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(views, "salon_profile", return_value=SALON))
            stack.enter_context(mock.patch.object(views, "live_salon_packages", return_value=list(rows)))
            self.reads(stack)
            self.read = stack.enter_context(covers(photos))
            return self.client.get(pd.tab_url()).json()

    @override_settings(**ON)
    def test_on_each_bundle_has_its_photo_right_after_theme(self):
        body = self.tab(
            [pd.package_row(), pd.package_row(pd.PACKAGE_2, published_snapshot=pd.snap(name="Zed"))],
            photos=[(pd.PACKAGE, "https://cdn.example/full-groom.jpg")],
        )
        bundles = body["bundles"]
        self.assertEqual(
            [(b["id"], b["image_url"]) for b in bundles],
            [(str(pd.PACKAGE), "https://cdn.example/full-groom.jpg"), (str(pd.PACKAGE_2), None)],
        )
        keys = list(bundles[0])
        self.assertEqual(keys[keys.index("theme") + 1], "image_url")
        # Nothing else on the bundle moves: theme stays, services keep their
        # id, name and quantity.
        self.assertEqual(bundles[0]["theme"], "gold")
        self.assertEqual(
            [(s["id"], s["name"], s["quantity"]) for s in bundles[0]["services"]],
            [(str(pd.CUT), "The Gentleman's Cut", 1), (str(pd.BEARD), "Beard Trim & Shape", 1),
             (str(pd.TOWEL), "Hot Towel Shave", 1)],
        )

    @override_settings(**ON)
    def test_on_one_read_for_the_listed_bundles_only(self):
        hidden = pd.package_row(PACKAGE_3, online_booking_enabled=False)
        self.tab([pd.package_row(), pd.package_row(pd.PACKAGE_2), hidden])
        self.read.filter.assert_called_once()
        self.assertEqual(
            self.read.filter.call_args.kwargs["package_id__in"], {pd.PACKAGE, pd.PACKAGE_2}
        )

    @override_settings(**ON)
    def test_on_no_bundle_reads_no_photo(self):
        self.assertEqual(self.tab([])["bundles"], [])
        self.read.filter.assert_not_called()

    @override_settings(**OFF)
    def test_off_as_before_and_no_photo_read(self):
        body = self.tab([pd.package_row()], photos=[(pd.PACKAGE, "https://cdn.example/x.jpg")])
        bundle = body["bundles"][0]
        self.assertNotIn("image_url", bundle)
        self.assertEqual(list(bundle)[-2:], ["theme", "services"])
        self.read.filter.assert_not_called()

    @override_settings(**ON, SERVICE_IMAGES_V1=True, PACKAGE_BOOKING_V1=True)
    def test_on_with_the_other_switches_every_key_in_its_place(self):
        with mock.patch.object(views.service_images, "for_salon", return_value={}):
            body = self.tab([pd.package_row()], photos=[(pd.PACKAGE, "https://cdn.example/p.jpg")])
        bundle = body["bundles"][0]
        self.assertEqual(list(bundle)[-4:], ["theme", "image_url", "services", "can_book"])
        self.assertEqual(
            list(bundle["services"][0])[-3:], ["duration_max", "images", "quantity"]
        )

    def test_quantity_is_how_many_of_that_service_the_bundle_covers(self):
        row = pd.package_row(published_snapshot=pd.snap(items=[
            pd.snap_item(pd.CUT, quantity=6), pd.snap_item(pd.BEARD, quantity=6), pd.snap_item(pd.TOWEL),
        ]))
        services = self.tab([row])["bundles"][0]["services"]
        self.assertEqual([s["quantity"] for s in services], [6, 6, 1])


@override_settings(PACKAGES_LIVE_RULES_V1=True, SERVICE_IMAGES_V1=True, **ON)
class SearchReadsNoPhotoTests(pd.Reads, SimpleTestCase):
    """
    The searches ask the tab's rules whether a salon lists a package, for
    every salon (search_results.tab_has_packages): no photo is read there.
    """

    def test_no_photo_read(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(views, "live_salon_packages", return_value=[pd.package_row()]))
            self.reads(stack)
            read = stack.enter_context(covers([(pd.PACKAGE, "https://cdn.example/p.jpg")]))
            service_photos = stack.enter_context(mock.patch.object(views.service_images, "for_salon"))
            self.assertTrue(search_results.tab_has_packages(SALON))
        read.filter.assert_not_called()
        service_photos.assert_not_called()


# ---------------------------------------------------------------------------
# The Packages tab on the old rules (PACKAGES_LIVE_RULES_V1 off)
# ---------------------------------------------------------------------------


@override_settings(PACKAGES_LIVE_RULES_V1=False, **pd.BOOKING_OFF)
class OldTabTests(pd.OldTabSeams, SimpleTestCase):

    def old_tab(self, rows, photos=()):
        with covers(photos) as read:
            self.read = read
            return self.tab(rows).json()

    @override_settings(**ON)
    def test_on_each_bundle_has_its_photo_right_after_theme(self):
        body = self.old_tab(
            [pd.old_tab_row(), pd.old_tab_row(pd.PACKAGE_2, name="B")],
            photos=[(pd.PACKAGE_2, "https://cdn.example/b.jpg")],
        )
        self.assertEqual(
            [(b["id"], b["image_url"]) for b in body["bundles"]],
            [(str(pd.PACKAGE), None), (str(pd.PACKAGE_2), "https://cdn.example/b.jpg")],
        )
        self.assertEqual(list(body["bundles"][0])[-2:], ["theme", "image_url"])
        self.read.filter.assert_called_once()
        self.assertEqual(
            self.read.filter.call_args.kwargs["package_id__in"], {pd.PACKAGE, pd.PACKAGE_2}
        )

    @override_settings(**ON, PACKAGE_BOOKING_V1=True)
    def test_on_with_booking_on_can_book_stays_last(self):
        body = self.old_tab([pd.old_tab_row()])
        self.assertEqual(list(body["bundles"][0])[-3:], ["theme", "image_url", "can_book"])

    @override_settings(**ON)
    def test_on_no_bundle_reads_no_photo(self):
        self.assertEqual(self.old_tab([])["bundles"], [])
        self.read.filter.assert_not_called()

    @override_settings(**OFF)
    def test_off_as_before_and_no_photo_read(self):
        body = self.old_tab([pd.old_tab_row()], photos=[(pd.PACKAGE, "https://cdn.example/x.jpg")])
        self.assertNotIn("image_url", body["bundles"][0])
        self.assertEqual(list(body["bundles"][0])[-1], "theme")
        self.read.filter.assert_not_called()


# ---------------------------------------------------------------------------
# services-details' image_url is the first of the lists' `images`
# ---------------------------------------------------------------------------


class ServicesDetailsImageTests(SimpleTestCase):
    """
    selectors.services_by_ids picks `image_url` from service_media by the
    lists' own rule (service_images.images_for): not deleted, no blank URL,
    primary first, then sort order, then oldest, then id.
    """

    def sql(self):
        query = selectors.services_by_ids([uuid.uuid4()]).query
        sql, params = query.sql_with_params()
        return sql, params

    def test_same_order_as_the_lists(self):
        sql, _ = self.sql()
        photo = sql[sql.index('"service_media"'):]
        self.assertIn(
            'ORDER BY U0."is_primary" DESC, U0."sort_order" ASC, U0."created_at" ASC, U0."id" ASC',
            photo,
        )

    def test_a_blank_url_is_no_image(self):
        sql, params = self.sql()
        photo = sql[sql.index('"service_media"'):]
        self.assertIn('NOT (U0."url" = %s', photo)
        self.assertIn("", params)

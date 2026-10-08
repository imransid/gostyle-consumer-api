"""
Other services' tables in the admin are read-only, and stay in their own
databases.

  1. **Never writable.** apps/accounts/admin.py registers every other model with
     full write access, so a read-only app it did not skip would get add, change
     and delete buttons over another service's rows.

  2. **Off until configured.** Without its database the admin page would only
     crash, so booking_data, review_data and push_data are left out. Tests run
     without those databases, as CI does.

  3. **Routed, never migrated.** Reads of those apps go to their own database,
     and nothing ever migrates into one.
"""

from django.apps import apps
from django.contrib import admin
from django.db import models
from django.test import SimpleTestCase

from apps.platform_data.models import BranchMembershipRows, Tenant
from apps.push_data.models import Device
from config.db_router import READ_ONLY_DATABASES, ReadOnlyDataRouter
from config.read_only_admin import (
    READ_ONLY_APPS,
    ListOnlyAdmin,
    ReadOnlyAdmin,
)


class ReadOnlyAdminTests(SimpleTestCase):
    def test_every_read_only_model_in_the_admin_is_read_only(self):
        registered = [
            (model, model_admin)
            for model, model_admin in admin.site._registry.items()
            if model._meta.app_label in READ_ONLY_APPS
        ]

        self.assertTrue(registered)
        for model, model_admin in registered:
            with self.subTest(model=model._meta.label):
                self.assertIsInstance(model_admin, ReadOnlyAdmin)

    def test_platform_tables_are_all_in_the_admin(self):
        for model in apps.get_app_config("platform_data").get_models():
            if isinstance(model._meta.pk, models.CompositePrimaryKey):
                continue
            with self.subTest(model=model.__name__):
                self.assertIn(model, admin.site._registry)

    def test_list_only_twin_has_no_detail_link_or_selection(self):
        model_admin = admin.site._registry[BranchMembershipRows]

        self.assertIsInstance(model_admin, ListOnlyAdmin)
        self.assertIsNone(model_admin.list_display_links)
        self.assertIsNone(model_admin.actions)

    def test_apps_without_their_database_are_left_out(self):
        for app_label in READ_ONLY_DATABASES:
            for model in apps.get_app_config(app_label).get_models():
                with self.subTest(model=model._meta.label):
                    self.assertNotIn(model, admin.site._registry)


class ReadOnlyDataRouterTests(SimpleTestCase):
    router = ReadOnlyDataRouter()

    def test_reads_and_writes_go_to_the_service_database(self):
        self.assertEqual(self.router.db_for_read(Device), "push")
        self.assertEqual(self.router.db_for_write(Device), "push")

    def test_our_own_and_platform_models_are_not_routed(self):
        self.assertIsNone(self.router.db_for_read(Tenant))

    def test_nothing_migrates_into_a_service_database(self):
        self.assertFalse(self.router.allow_migrate("push", "accounts"))
        self.assertFalse(self.router.allow_migrate("default", "push_data"))
        self.assertIsNone(self.router.allow_migrate("default", "accounts"))

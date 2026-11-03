"""
display_name and account_number on /auth/me (Personal Details' "Display name",
Settings' "Account ID").

The tests that matter most are the ones that pin what must NOT change: the
name salons see is still full_name, a PATCH cannot set the number, and an
account keeps its number for life.
"""

import importlib
import re
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.accounts import account_numbers
from apps.accounts.models import ConsumerAccount
from apps.accounts.tests.test_auth import AuthTestCase

ME = "/api/v1/auth/me"
FORMAT = re.compile(r"^GS-\d{4}-\d{4}$")


class AccountNumberTests(AuthTestCase):
    def test_a_new_account_has_one_in_the_format(self):
        self.register_and_authenticate()

        number = self.client.get(ME).json()["account_number"]
        self.assertRegex(number, FORMAT)
        # Stored, not made up on each read.
        self.assertEqual(ConsumerAccount.objects.get().account_number, number)
        self.assertEqual(self.client.get(ME).json()["account_number"], number)

    def test_create_user_gives_one_too(self):
        account = get_user_model().objects.create_user(phone="+971500000701")
        self.assertRegex(account.account_number, FORMAT)

    def test_two_accounts_never_share_one(self):
        users = get_user_model().objects
        first = users.create_user(phone="+971500000702")
        # The next draw repeats the first account's number, then a free one.
        with mock.patch.object(
            account_numbers, "make", side_effect=[first.account_number, "GS-0000-0001"]
        ):
            second = users.create_user(phone="+971500000703")
        self.assertEqual(second.account_number, "GS-0000-0001")

    def test_a_purged_accounts_number_is_not_given_again(self):
        users = get_user_model().objects
        gone = users.create_user(phone="+971500000704")
        gone.purge()
        with mock.patch.object(
            account_numbers, "make", side_effect=[gone.account_number, "GS-0000-0002"]
        ):
            self.assertEqual(account_numbers.new(), "GS-0000-0002")

    def test_new_gives_up_when_every_draw_is_taken(self):
        taken = get_user_model().objects.create_user(phone="+971500000705")
        with mock.patch.object(account_numbers, "make", return_value=taken.account_number):
            with self.assertRaises(RuntimeError):
                account_numbers.new()

    def test_a_row_from_older_code_gets_one_on_its_first_read(self):
        self.register_and_authenticate()
        ConsumerAccount.objects.update(account_number=None)

        number = self.client.get(ME).json()["account_number"]
        self.assertRegex(number, FORMAT)
        self.assertEqual(ConsumerAccount.objects.get().account_number, number)
        self.assertEqual(self.client.get(ME).json()["account_number"], number)

    def test_ensure_never_replaces_a_number_set_meanwhile(self):
        account = get_user_model().objects.create_user(phone="+971500000706")
        stale = ConsumerAccount.objects.get(pk=account.pk)
        stale.account_number = None  # this copy was read before the fill
        self.assertEqual(account_numbers.ensure(stale), account.account_number)
        account.refresh_from_db()
        self.assertEqual(stale.account_number, account.account_number)

    def test_a_patch_cannot_set_it(self):
        self.register_and_authenticate()
        before = ConsumerAccount.objects.get().account_number

        resp = self.client.patch(ME, {"account_number": "GS-0000-0000"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["account_number"], before)
        self.assertEqual(ConsumerAccount.objects.get().account_number, before)

    def test_the_purge_keeps_it(self):
        account = get_user_model().objects.create_user(phone="+971500000707")
        number = account.account_number
        account.purge()
        self.assertEqual(ConsumerAccount.all_objects.get(pk=account.pk).account_number, number)


class FillMigrationTests(TestCase):
    """accounts/0031: every account that existed gets a number, all different."""

    def test_it_fills_only_the_missing_ones(self):
        users = get_user_model().objects
        kept = users.create_user(phone="+971500000801")
        for n in range(2, 6):
            users.create_user(phone=f"+97150000080{n}")
        gone = users.create_user(phone="+971500000809")
        gone.purge()
        ConsumerAccount.all_objects.exclude(pk=kept.pk).update(account_number=None)

        migration = importlib.import_module("apps.accounts.migrations.0031_fill_account_numbers")
        migration.fill(django_apps, None)

        numbers = list(ConsumerAccount.all_objects.values_list("account_number", flat=True))
        self.assertEqual(len(numbers), 6)
        self.assertTrue(all(FORMAT.match(n) for n in numbers))
        self.assertEqual(len(set(numbers)), 6)
        self.assertIn(kept.account_number, numbers)


class DisplayNameTests(AuthTestCase):
    def patch(self, value):
        return self.client.patch(ME, {"display_name": value}, format="json")

    def test_unset_reads_null(self):
        self.register_and_authenticate()
        self.assertIsNone(self.client.get(ME).json()["display_name"])

    def test_a_patch_saves_it_trimmed(self):
        self.register_and_authenticate()

        resp = self.patch("  Kevin R.  ")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["display_name"], "Kevin R.")
        self.assertEqual(ConsumerAccount.objects.get().display_name, "Kevin R.")
        # The full name is not touched.
        self.assertEqual(resp.json()["full_name"], "Kevin Rogers")

    def test_empty_and_null_both_clear_it(self):
        self.register_and_authenticate()

        for clear in ("", None, "   "):
            self.patch("Kevin R.")
            resp = self.patch(clear)
            self.assertEqual(resp.status_code, 200, clear)
            self.assertIsNone(resp.json()["display_name"], clear)
            self.assertEqual(ConsumerAccount.objects.get().display_name, "", clear)

    def test_more_than_40_characters_is_refused(self):
        self.register_and_authenticate()

        resp = self.patch("x" * 41)
        self.assertEqual(resp.status_code, 422, resp.data)
        self.assertIn(
            ("display_name", "max_length"),
            [(e["field"], e["code"]) for e in resp.data["errors"]],
        )
        self.assertEqual(self.patch("x" * 40).status_code, 200)

    def test_leaving_it_out_keeps_it(self):
        self.register_and_authenticate()
        self.patch("Kevin R.")

        resp = self.client.patch(ME, {"location": "Dubai"}, format="json")
        self.assertEqual(resp.json()["display_name"], "Kevin R.")

    def test_the_purge_clears_it(self):
        account = get_user_model().objects.create_user(phone="+971500000708")
        ConsumerAccount.objects.filter(pk=account.pk).update(display_name="Sara A.")
        account.refresh_from_db()
        account.purge()
        self.assertEqual(ConsumerAccount.all_objects.get(pk=account.pk).display_name, "")


@override_settings(INTERNAL_GRPC_KEY="k-123")
class SalonsStillSeeTheFullNameTests(TestCase):
    """The display name is the app's only. booking-api's desk still gets the
    name accounts/privacy.py gives it, from full_name."""

    def ask(self, account):
        from apps.grpc_gen.servicer import ConsumerDirectoryServicer
        from apps.grpc_gen.tests import _context

        request = mock.Mock(consumer_id=str(account.pk))
        with mock.patch("apps.grpc_gen.servicer.close_old_connections"):
            return ConsumerDirectoryServicer().GetConsumerContact(request, _context("k-123"))

    def test_the_desk_gets_the_full_name(self):
        account = get_user_model().objects.create_user(
            phone="+971500000709", full_name="Sara Ahmed", display_name="Sasa"
        )
        self.assertEqual(self.ask(account).full_name, "Sara Ahmed")

    def test_and_the_short_one_when_the_name_is_hidden(self):
        account = get_user_model().objects.create_user(
            phone="+971500000710",
            full_name="Sara Ahmed",
            display_name="Sasa",
            show_name_to_salons=False,
        )
        self.assertEqual(self.ask(account).full_name, "Sara A.")

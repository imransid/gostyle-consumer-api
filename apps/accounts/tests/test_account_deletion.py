"""
Account deletion: POST /auth/me/delete and /auth/me/delete/cancel, the
purge_due_accounts command, and the ConsumerAccount admin.

Two dates, two meanings:

  * purge_at, set by the request: "deletion scheduled". The account still
    works, so its owner can sign in, see the date on /auth/me and cancel.
  * deleted_at, set by the purge: personal data cleared, phone and email
    freed, no sign-in of any kind.

So the command purges rows with purge_at in the past and deleted_at still
null. A row with deleted_at set is one it has already done.
"""

import uuid
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.models import (
    ConsumerAccount,
    Device,
    Favourite,
    FavouriteStylist,
    NotificationPreference,
    Session,
)
from apps.accounts.tests.test_auth import PASSWORD, AuthTestCase

DELETE = "/api/v1/auth/me/delete"
CANCEL = "/api/v1/auth/me/delete/cancel"
ME = "/api/v1/auth/me"
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/token/refresh"
ADMIN_LIST = "/admin/accounts/consumeraccount/"


class DeletionTestCase(AuthTestCase):
    def setUp(self):
        super().setUp()
        # Real tokens from register, not force_authenticate: the 401s after a
        # purge must come from the real token checks.
        self.tokens = self.register_and_authenticate().data
        self.account = ConsumerAccount.objects.get(phone=self.phone)

    def request_deletion(self, **overrides):
        payload = {"password": PASSWORD, "reason": "privacy", "note": "Moving abroad."}
        payload.update(overrides)
        return self.client.post(DELETE, payload, format="json")

    def make_due(self, account):
        """Move purge_at into the past, as if the 30 days had run out."""
        ConsumerAccount.all_objects.filter(pk=account.pk).update(
            purge_at=timezone.now() - timedelta(minutes=1)
        )

    def purge(self, *args):
        out = StringIO()
        call_command("purge_due_accounts", *args, stdout=out)
        return out.getvalue()


class DeleteRequestTests(DeletionTestCase):
    def test_the_right_password_schedules_the_purge(self):
        before = timezone.now()
        resp = self.request_deletion()

        self.assertEqual(resp.status_code, 200, resp.data)
        self.account.refresh_from_db()
        # The answer is the value that was saved, not one from before the save.
        self.assertEqual(parse_datetime(resp.json()["purge_at"]), self.account.purge_at)
        self.assertAlmostEqual(
            self.account.purge_at,
            before + timedelta(days=ConsumerAccount.PURGE_AFTER_DAYS),
            delta=timedelta(seconds=30),
        )
        self.assertEqual(self.account.deletion_reason, "privacy")
        self.assertEqual(self.account.deletion_note, "Moving abroad.")

        # Scheduled, not deleted: the account still works.
        self.assertIsNone(self.account.deleted_at)
        self.assertTrue(self.account.is_active)
        self.assertEqual(self.client.get(ME).status_code, 200)

    def test_reason_and_note_are_optional(self):
        resp = self.client.post(DELETE, {"password": PASSWORD}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        self.account.refresh_from_db()
        self.assertEqual(self.account.deletion_reason, "other")
        self.assertEqual(self.account.deletion_note, "")

    def test_a_wrong_password_is_403_and_schedules_nothing(self):
        resp = self.request_deletion(password="Wr0ng!Pass")

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            resp.json()["errors"],
            [{"field": "password", "code": "wrong_password", "message": "Wrong password."}],
        )
        self.account.refresh_from_db()
        self.assertIsNone(self.account.purge_at)

    def test_no_token_is_401(self):
        self.anonymous()

        resp = self.request_deletion()
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "not_authenticated")
        self.assertEqual(self.client.post(CANCEL).status_code, 401)

        self.account.refresh_from_db()
        self.assertIsNone(self.account.purge_at)

    def test_an_unknown_reason_is_422(self):
        """Not 400: every field error in this API is 422, with the field and
        a code (apps/accounts/exceptions.py). The app reads it that way.
        """
        resp = self.request_deletion(reason="bored")

        self.assertEqual(resp.status_code, 422)
        self.assertEqual(
            [(e["field"], e["code"]) for e in resp.json()["errors"]],
            [("reason", "invalid_choice")],
        )
        self.account.refresh_from_db()
        self.assertIsNone(self.account.purge_at)

    def test_a_note_over_1000_characters_is_refused_not_cut(self):
        resp = self.request_deletion(note="x" * 1001)

        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["errors"][0]["field"], "note")


class CancelDeletionTests(DeletionTestCase):
    def test_cancel_clears_the_schedule(self):
        self.request_deletion()

        resp = self.client.post(CANCEL)

        self.assertEqual(resp.status_code, 204)
        self.account.refresh_from_db()
        self.assertIsNone(self.account.purge_at)
        self.assertIsNone(self.account.deleted_at)
        self.assertEqual(self.account.deletion_reason, "")
        self.assertEqual(self.account.deletion_note, "")

    def test_cancel_with_nothing_scheduled_is_fine(self):
        self.assertEqual(self.client.post(CANCEL).status_code, 204)


class MeDeletionFieldsTests(DeletionTestCase):
    def test_me_shows_the_schedule(self):
        me = self.client.get(ME).json()
        self.assertIsNone(me["purge_at"])
        self.assertIsNone(me["deleted_at"])

        purge_at = self.request_deletion().json()["purge_at"]
        me = self.client.get(ME).json()
        self.assertEqual(me["purge_at"], purge_at)
        self.assertIsNone(me["deleted_at"])

        self.client.post(CANCEL)
        self.assertIsNone(self.client.get(ME).json()["purge_at"])

    def test_a_patch_cannot_set_them(self):
        resp = self.client.patch(
            ME,
            {"purge_at": "2026-01-01T00:00:00Z", "deleted_at": "2026-01-01T00:00:00Z"},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.account.refresh_from_db()
        self.assertIsNone(self.account.purge_at)
        self.assertIsNone(self.account.deleted_at)


class PurgeCommandTests(DeletionTestCase):
    def other_account(self, phone):
        return ConsumerAccount.objects.create(phone=phone, full_name="Ann")

    def test_purges_the_past_and_skips_the_future(self):
        self.request_deletion()
        self.make_due(self.account)
        later = self.other_account("+8801733333333")
        later.request_deletion()  # 30 days from now
        untouched = self.other_account("+8801744444444")

        self.assertIn("Purged 1 account(s).", self.purge())

        gone = ConsumerAccount.all_objects.get(pk=self.account.pk)
        self.assertIsNotNone(gone.deleted_at)
        self.assertFalse(gone.is_active)
        self.assertFalse(gone.has_usable_password())
        self.assertEqual((gone.phone, gone.email, gone.full_name), (None, None, ""))
        self.assertEqual(gone.deletion_reason, "privacy")
        self.assertFalse(ConsumerAccount.objects.filter(pk=gone.pk).exists())

        later.refresh_from_db()
        self.assertIsNone(later.deleted_at)
        self.assertEqual(later.phone, "+8801733333333")
        self.assertGreater(later.purge_at, timezone.now())

        untouched.refresh_from_db()
        self.assertIsNone(untouched.deleted_at)

    def test_running_twice_is_safe(self):
        self.request_deletion()
        self.make_due(self.account)

        self.assertIn("Purged 1 account(s).", self.purge())
        first = ConsumerAccount.all_objects.get(pk=self.account.pk).deleted_at

        self.assertIn("Purged 0 account(s).", self.purge())
        self.assertEqual(ConsumerAccount.all_objects.get(pk=self.account.pk).deleted_at, first)

    def test_dry_run_purges_nothing(self):
        self.request_deletion()
        self.make_due(self.account)

        self.assertIn("Dry run: 1 account(s) due, none purged.", self.purge("--dry-run"))

        self.account.refresh_from_db()
        self.assertIsNone(self.account.deleted_at)
        self.assertEqual(self.account.phone, self.phone)

    def test_the_purge_removes_devices_favourites_and_preferences(self):
        Device.objects.create(account=self.account, push_token="fcm-1", platform="ios")
        Favourite.objects.create(account=self.account, storefront_id=uuid.uuid4())
        FavouriteStylist.objects.create(account=self.account, staff_id=uuid.uuid4())
        NotificationPreference.for_account(self.account)
        Session.objects.create(account=self.account, device_name="Test Device")
        self.request_deletion()
        self.make_due(self.account)

        self.purge()

        for model in (Device, Favourite, FavouriteStylist, NotificationPreference, Session):
            with self.subTest(model=model.__name__):
                self.assertFalse(model.objects.filter(account=self.account).exists())


class AfterPurgeTests(DeletionTestCase):
    def purge_now(self):
        self.request_deletion()
        self.make_due(self.account)
        self.purge()

    def test_the_phone_can_register_again(self):
        self.purge_now()
        self.anonymous()

        resp = self.register()

        self.assertEqual(resp.status_code, 201, resp.data)
        fresh = ConsumerAccount.objects.get(phone=self.phone)
        self.assertNotEqual(fresh.pk, self.account.pk)
        # The old row stays, with no contact on it.
        self.assertIsNone(ConsumerAccount.all_objects.get(pk=self.account.pk).phone)

    def test_a_purged_account_cannot_authenticate(self):
        self.verify_own_contact()
        login = {"destination_type": "phone", "destination": self.phone, "password": PASSWORD}
        # Signing in worked before the purge, so the refusals below are its doing.
        self.assertEqual(self.client.post(LOGIN, login, format="json").status_code, 200)

        self.purge_now()

        # The access token it already holds.
        self.assertEqual(self.client.get(ME).status_code, 401)
        # Its refresh token: 401, not the 500 a hidden row would give.
        resp = self.client.post(REFRESH, {"refresh": self.tokens["refresh"]}, format="json")
        self.assertEqual(resp.status_code, 401)
        # Its phone and password.
        self.anonymous()
        resp = self.client.post(LOGIN, login, format="json")
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["detail"], "Invalid credentials.")


class DeletionAdminTests(DeletionTestCase):
    def setUp(self):
        super().setUp()
        staff = ConsumerAccount.objects.create_superuser(
            phone="+8801799999999", password=PASSWORD
        )
        self.admin = Client()
        self.admin.force_login(staff)

        self.request_deletion()  # self.account: pending
        self.purged = ConsumerAccount.objects.create(phone="+8801755555555")
        self.purged.request_deletion()
        self.make_due(self.purged)
        self.purge()

    def listed(self, **params):
        resp = self.admin.get(ADMIN_LIST, params)
        self.assertEqual(resp.status_code, 200)
        return {row.pk for row in resp.context["cl"].result_list}

    def test_purged_accounts_are_listed(self):
        listed = self.listed()

        self.assertIn(self.purged.pk, listed)
        self.assertIn(self.account.pk, listed)
        page = self.admin.get(f"{ADMIN_LIST}{self.purged.pk}/change/")
        self.assertEqual(page.status_code, 200)

    def test_the_deletion_filter(self):
        self.assertEqual(self.listed(deletion="pending"), {self.account.pk})
        self.assertEqual(self.listed(deletion="purged"), {self.purged.pk})

    def test_the_deletion_columns_are_shown_and_read_only(self):
        columns = self.admin.get(ADMIN_LIST).context["cl"].list_display
        for column in ("deleted_at", "purge_at", "deletion_reason"):
            self.assertIn(column, columns)

        page = self.admin.get(f"{ADMIN_LIST}{self.account.pk}/change/")
        form = page.context["adminform"].form
        for field in ("deleted_at", "purge_at", "deletion_reason", "deletion_note"):
            self.assertNotIn(field, form.fields)

    def test_nothing_in_the_admin_deletes(self):
        self.assertIsNone(self.admin.get(ADMIN_LIST).context["action_form"])

        resp = self.admin.post(f"{ADMIN_LIST}{self.account.pk}/delete/", {"post": "yes"})

        self.assertEqual(resp.status_code, 403)
        self.assertTrue(ConsumerAccount.objects.filter(pk=self.account.pk).exists())

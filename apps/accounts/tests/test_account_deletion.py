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
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.models import (
    ConsumerAccount,
    Device,
    Favourite,
    FavouriteStylist,
    LoginChallenge,
    NotificationPreference,
    TwoFactorPurpose,
    UserAuthenticator,
    VerificationCode,
    VerificationToken,
)
from apps.accounts.deletion import BOOKINGS_WAIT, purge_due
from apps.accounts.tests.test_auth import PASSWORD, AuthTestCase
from apps.notifications.models import Notification
from apps.salons.account_deletion import Report, Unreachable
from apps.salons.booking_api import BookingApiUnavailable

DELETE = "/api/v1/auth/me/delete"
CANCEL = "/api/v1/auth/me/delete/cancel"
SUMMARY = "/api/v1/auth/me/delete/summary"
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
        # The purge cancels bookings in booking-api first. Not here: that
        # part has its own tests (apps/salons/test_account_deletion.py).
        patcher = mock.patch(
            "apps.salons.account_deletion.cancel_bookings_to_come", return_value=Report()
        )
        self.cancel_bookings = patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_no_verified_email_means_no_confirmation_named(self):
        """The Scheduled screen says where the email went. An account with no
        verified email gets none, and the answer must not name one."""
        ConsumerAccount.objects.filter(pk=self.account.pk).update(email="kevin@example.com")

        resp = self.request_deletion()

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIsNone(resp.json()["confirmation_sent_to"])
        self.assertEqual(len(mail.outbox), 0)

    def test_the_confirmation_names_the_verified_email_it_went_to(self):
        ConsumerAccount.objects.filter(pk=self.account.pk).update(
            email="kevin@example.com", email_verified_at=timezone.now()
        )

        with self.captureOnCommitCallbacks(execute=True):
            resp = self.request_deletion()

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.json()["confirmation_sent_to"], "kevin@example.com")
        self.assertEqual([m.to for m in mail.outbox], [["kevin@example.com"]])

    def test_asking_again_keeps_the_first_date(self):
        first = self.request_deletion().json()["purge_at"]
        ConsumerAccount.objects.filter(pk=self.account.pk).update(
            purge_at=parse_datetime(first) - timedelta(days=10)
        )
        moved = ConsumerAccount.objects.get(pk=self.account.pk).purge_at

        resp = self.request_deletion(reason="not_using", note="")

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(parse_datetime(resp.json()["purge_at"]), moved)
        self.account.refresh_from_db()
        self.assertEqual(self.account.purge_at, moved)
        # The newest reason and note are kept.
        self.assertEqual(self.account.deletion_reason, "not_using")
        self.assertEqual(self.account.deletion_note, "")

    def test_after_a_cancel_a_new_request_starts_30_days_again(self):
        self.request_deletion()
        self.client.post(CANCEL)

        before = timezone.now()
        self.request_deletion()

        self.account.refresh_from_db()
        self.assertAlmostEqual(
            self.account.purge_at,
            before + timedelta(days=ConsumerAccount.PURGE_AFTER_DAYS),
            delta=timedelta(seconds=30),
        )


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


class DeletionSummaryTests(DeletionTestCase):
    def summary(self, answer=(200, {"counts": {"upcoming": 3, "recurring": 1, "archive": 9}})):
        with mock.patch("apps.salons.booking_api.list_bookings", return_value=answer) as ask:
            resp = self.client.get(SUMMARY)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json(), ask

    def test_the_numbers_for_the_warning_screen(self):
        Favourite.objects.create(account=self.account, storefront_id=uuid.uuid4())
        FavouriteStylist.objects.create(account=self.account, staff_id=uuid.uuid4())

        body, ask = self.summary()

        self.assertEqual(
            body,
            {"purge_after_days": 30, "purge_at": None, "upcoming_bookings": 3, "favourites": 2},
        )
        # The caller's own token, and the Upcoming number only (one row).
        query = ask.call_args.args[0]
        self.assertIn("filter=upcoming", query)
        self.assertIn("pageSize=1", query)
        self.assertEqual(
            ask.call_args.kwargs["authorization"], f"Bearer {self.tokens['access']}"
        )

    def test_only_real_things_are_sent(self):
        """No wallet, saved card, discount card, points or gallery exist: the
        app hides those rows because the keys are not here."""
        body, _ = self.summary()

        for missing in ("wallet", "points", "tier", "cards", "discount_card", "gallery"):
            with self.subTest(key=missing):
                self.assertFalse(any(missing in key for key in body))

    def test_a_scheduled_deletion_shows_its_date(self):
        purge_at = self.request_deletion().json()["purge_at"]

        body, _ = self.summary()

        self.assertEqual(body["purge_at"], purge_at)

    def test_booking_api_down_is_null_not_an_error(self):
        with mock.patch(
            "apps.salons.booking_api.list_bookings", side_effect=BookingApiUnavailable("down")
        ):
            resp = self.client.get(SUMMARY)

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["upcoming_bookings"])

    def test_a_refusal_or_an_odd_answer_is_null(self):
        for answer in ((401, {"code": "UNAUTHENTICATED"}), (200, {"counts": {}}), (200, None),
                       (200, {"counts": {"upcoming": True}})):
            with self.subTest(answer=answer):
                body, _ = self.summary(answer)
                self.assertIsNone(body["upcoming_bookings"])

    def test_no_token_is_401(self):
        self.anonymous()
        self.assertEqual(self.client.get(SUMMARY).status_code, 401)


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
        self.request_deletion()
        self.make_due(self.account)

        self.purge()

        for model in (Device, Favourite, FavouriteStylist, NotificationPreference):
            with self.subTest(model=model.__name__):
                self.assertFalse(model.objects.filter(account=self.account).exists())

    def test_the_purge_removes_2_step_verification(self):
        ConsumerAccount.objects.filter(pk=self.account.pk).update(
            two_factor_enabled=True, two_factor_changed_at=timezone.now()
        )
        later = timezone.now() + timedelta(minutes=5)
        UserAuthenticator.objects.create(
            user=self.account, secret_encrypted=b"x", status=UserAuthenticator.Status.ACTIVE
        )
        VerificationCode.objects.create(
            user=self.account,
            method="email",
            purpose=TwoFactorPurpose.TWO_STEP_OFF,
            code_hash="h",
            expires_at=later,
        )
        VerificationToken.objects.create(
            token_hash="t",
            user=self.account,
            session_id=uuid.uuid4(),
            purpose=TwoFactorPurpose.AUTHENTICATOR_REMOVE,
            expires_at=later,
        )
        LoginChallenge.objects.create(token_hash="c", user=self.account, expires_at=later)
        self.request_deletion()
        self.make_due(self.account)

        self.purge()

        gone = ConsumerAccount.all_objects.get(pk=self.account.pk)
        self.assertEqual((gone.two_factor_enabled, gone.two_factor_changed_at), (False, None))
        for model in (UserAuthenticator, VerificationCode, VerificationToken, LoginChallenge):
            with self.subTest(model=model.__name__):
                self.assertFalse(model.objects.filter(user=self.account).exists())


class PurgeCancelsBookingsTests(DeletionTestCase):
    def setUp(self):
        super().setUp()
        self.request_deletion()

    def test_bookings_are_cancelled_before_the_purge(self):
        seen = {}

        def cancel(account):
            # Still the customer: the purge has not run yet.
            seen["phone"] = ConsumerAccount.all_objects.get(pk=account.pk).phone
            report = Report(cancelled=2)
            report.leave("party", "g1", "a place in someone else's party")
            return report

        self.cancel_bookings.side_effect = cancel
        self.make_due(self.account)

        out = self.purge()

        self.assertEqual(seen["phone"], self.phone)
        self.assertIn("Purged 1 account(s).", out)
        self.assertIn("Bookings cancelled: 2, left as they were: 1.", out)
        self.assertIsNotNone(ConsumerAccount.all_objects.get(pk=self.account.pk).deleted_at)

    def test_booking_api_down_makes_the_purge_wait(self):
        self.cancel_bookings.side_effect = Unreachable("booking-api down")
        self.make_due(self.account)

        out = self.purge()

        self.assertIn("Purged 0 account(s).", out)
        self.assertIn("1 account(s) wait for booking-api", out)
        self.account.refresh_from_db()
        self.assertIsNone(self.account.deleted_at)
        self.assertEqual(self.account.phone, self.phone)

    def test_not_for_ever_the_purge_goes_ahead_after_the_wait(self):
        self.cancel_bookings.side_effect = Unreachable("booking-api down")
        ConsumerAccount.objects.filter(pk=self.account.pk).update(
            purge_at=timezone.now() - BOOKINGS_WAIT - timedelta(minutes=1)
        )

        run = purge_due()

        self.assertEqual((run.purged, run.waiting), (1, 0))
        self.assertIsNotNone(ConsumerAccount.all_objects.get(pk=self.account.pk).deleted_at)

    def test_not_due_is_not_asked(self):
        self.purge()
        self.cancel_bookings.assert_not_called()

    def test_a_failing_cancel_does_not_stop_the_others(self):
        other = ConsumerAccount.objects.create(phone="+8801766666666")
        other.request_deletion()
        self.make_due(self.account)
        self.make_due(other)
        self.cancel_bookings.side_effect = [RuntimeError("boom"), Report()]

        run = purge_due()

        self.assertEqual((run.purged, run.failed), (1, 1))

    def test_the_purge_removes_the_notification_center(self):
        Notification.objects.create(
            account=self.account, category="orders", type="booking.cancelled",
            title="Booking cancelled", body="Your visit at Glow on 21 Nov",
            source="booking", dedupe_key="b1",
        )
        self.make_due(self.account)

        self.purge()

        self.assertFalse(Notification.objects.filter(account_id=self.account.pk).exists())


class WorkerPurgeTests(DeletionTestCase):
    """The notification worker runs the purge every hour while
    ACCOUNT_PURGE_V1 is on, and never while it is off."""

    def run_worker_once(self):
        from apps.notifications.management.commands import notification_worker

        command = notification_worker.Command()

        def stop_after_one_loop(*args, **kwargs):
            command.stopping = True
            return {}

        # PURGE_EVERY 0: the hourly block runs on the first loop whatever the
        # machine's monotonic clock reads.
        with mock.patch.object(notification_worker.worker, "run_once", side_effect=stop_after_one_loop), \
                mock.patch.object(notification_worker, "PURGE_EVERY", 0), \
                mock.patch.object(notification_worker, "close_old_connections"), \
                mock.patch.object(notification_worker.signal, "signal"):
            command.handle(once=False, interval=0)

    def setUp(self):
        super().setUp()
        self.request_deletion()
        self.make_due(self.account)

    @override_settings(ACCOUNT_PURGE_V1=False)
    def test_off_nothing_is_purged(self):
        self.run_worker_once()

        self.assertIsNone(ConsumerAccount.all_objects.get(pk=self.account.pk).deleted_at)
        self.cancel_bookings.assert_not_called()

    @override_settings(ACCOUNT_PURGE_V1=True)
    def test_on_the_due_account_is_purged(self):
        self.run_worker_once()

        self.assertIsNotNone(ConsumerAccount.all_objects.get(pk=self.account.pk).deleted_at)
        self.cancel_bookings.assert_called_once()


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

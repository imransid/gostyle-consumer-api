"""
Password age and new sign-in alerts on /me/security
(docs/SIGN_IN_ALERTS_API.md): password_changed_at and sign_in_alerts on
GET /me/security, and PATCH /me/security for the switch.
"""

import importlib
from datetime import timedelta
from unittest.mock import patch

from django.apps import apps as django_apps
from django.core import mail
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import services, sessions
from apps.accounts.models import ConsumerAccount, Purpose, UserSession, Verification

from .test_two_factor import EMAIL, IPAD, PASSWORD, PHONE, UAE_TIME, SecurityApiTestCase

backfill = importlib.import_module(
    "apps.accounts.migrations.0027_password_changed_at_sign_in_alerts"
).backfill_password_changed_at

NEW_PASSWORD = "An0ther-Str0ng-Pass!"
OTHER_PHONE = "+971509876543"
OTHER_EMAIL = "someone.else@gmail.com"


class FrozenClockTestCase(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        clock = patch("django.utils.timezone.now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def make_account(self, phone=PHONE, email=None, password=PASSWORD):
        account = ConsumerAccount(phone=phone, email=email)
        account.set_password(password)
        account.save()
        return account


# --- password_changed_at -------------------------------------------------------------


class PasswordChangedAtTests(FrozenClockTestCase):
    def test_register_sets_it(self):
        services.register(PHONE, "phone", "Kevin Rogers", PASSWORD)
        account = ConsumerAccount.objects.get(phone=PHONE)
        self.assertEqual(account.password_changed_at, self.now)

    def test_a_change_sets_it(self):
        account = self.make_account()
        self.now += timedelta(days=30)
        services.change_password(account, PASSWORD, NEW_PASSWORD)
        account.refresh_from_db()
        self.assertEqual(account.password_changed_at, self.now)
        self.assertTrue(account.check_password(NEW_PASSWORD))

    def test_a_wrong_old_password_leaves_it(self):
        account = self.make_account()
        with self.assertRaises(Exception):
            services.change_password(account, "wrong", NEW_PASSWORD)
        account.refresh_from_db()
        self.assertIsNone(account.password_changed_at)

    def test_a_reset_sets_it(self):
        account = self.make_account()
        self.now += timedelta(days=30)
        token = Verification.issue(PHONE, "phone", Purpose.PASSWORD_RESET)
        services.reset_password(str(token.id), NEW_PASSWORD)
        account.refresh_from_db()
        self.assertEqual(account.password_changed_at, self.now)

    def test_createsuperuser_sets_it_only_with_a_password(self):
        staff = ConsumerAccount.all_objects.create_superuser(PHONE, password=PASSWORD)
        self.assertEqual(staff.password_changed_at, self.now)
        staff = ConsumerAccount.all_objects.create_superuser(OTHER_PHONE)
        self.assertIsNone(staff.password_changed_at)

    def test_a_purge_clears_it_with_the_password(self):
        account = self.make_account()
        ConsumerAccount.objects.filter(pk=account.pk).update(password_changed_at=self.now)
        account.refresh_from_db()
        account.purge()
        account = ConsumerAccount.all_objects.get(pk=account.pk)
        self.assertFalse(account.has_usable_password())
        self.assertIsNone(account.password_changed_at)


class BackfillTests(FrozenClockTestCase):
    """Migration 0027: the last password reset after the account was
    created, else when it was created."""

    def make_account(self, phone=PHONE, email=None, password=PASSWORD, days_old=200):
        account = super().make_account(phone, email, password)
        created = self.now - timedelta(days=days_old)
        ConsumerAccount.all_objects.filter(pk=account.pk).update(created_at=created)
        return account

    def reset(self, destination, days_ago, used=True):
        when = self.now - timedelta(days=days_ago)
        Verification.objects.create(
            destination=destination,
            destination_type="email" if "@" in destination else "phone",
            purpose=Purpose.PASSWORD_RESET,
            expires_at=when + timedelta(minutes=10),
            consumed_at=when if used else None,
        )

    def backfilled(self, account):
        backfill(django_apps, None)
        return ConsumerAccount.all_objects.get(pk=account.pk).password_changed_at

    def test_never_reset_is_when_it_was_created(self):
        account = self.make_account()
        self.assertEqual(self.backfilled(account), self.now - timedelta(days=200))

    def test_the_last_reset_for_its_phone_or_email(self):
        account = self.make_account(email=EMAIL)
        self.reset(PHONE, days_ago=120)
        self.reset(EMAIL, days_ago=90)
        self.reset(OTHER_PHONE, days_ago=10)  # someone else's
        self.reset(PHONE, days_ago=5, used=False)  # asked for, never used
        self.assertEqual(self.backfilled(account), self.now - timedelta(days=90))

    def test_a_reset_from_before_the_account_is_not_its(self):
        account = self.make_account(days_old=30)
        self.reset(PHONE, days_ago=60)  # an earlier account with this phone
        self.assertEqual(self.backfilled(account), self.now - timedelta(days=30))

    def test_no_password_stays_null(self):
        account = self.make_account()
        account.set_unusable_password()
        account.save()
        self.assertIsNone(self.backfilled(account))

    def test_a_time_already_set_is_kept(self):
        account = self.make_account()
        ConsumerAccount.objects.filter(pk=account.pk).update(password_changed_at=self.now)
        self.assertEqual(self.backfilled(account), self.now)


class StorageTests(TestCase):
    def test_sign_in_alerts_are_on_for_a_new_account_and_in_the_table(self):
        account = ConsumerAccount.objects.create_user(phone=PHONE)
        self.assertTrue(account.sign_in_alerts)
        # Also for a row written by code from before the column.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_default FROM information_schema.columns"
                " WHERE table_name = 'consumer_account' AND column_name = 'sign_in_alerts'"
            )
            self.assertEqual(cursor.fetchone()[0], "true")


# --- GET and PATCH /me/security ------------------------------------------------------

NOT_SIGNED_IN = {
    "detail": "Please sign in again.",
    "code": "not_authenticated",
    "errors": [{"field": None, "code": "not_authenticated", "message": "Please sign in again."}],
}


class SecurityReadTests(SecurityApiTestCase):
    def test_when_the_password_was_set_in_uae_time_and_the_switch(self):
        changed = timezone.now().replace(microsecond=123456)
        ConsumerAccount.objects.filter(pk=self.account.pk).update(password_changed_at=changed)
        body = self.call("get", "").json()
        self.assertRegex(body["password_changed_at"], UAE_TIME)
        self.assertEqual(
            body["password_changed_at"],
            changed.astimezone(timezone.get_fixed_timezone(240)).replace(microsecond=0).isoformat(),
        )
        self.assertIs(body["sign_in_alerts"], True)

    def test_a_password_with_no_time_is_when_the_account_was_created(self):
        ConsumerAccount.objects.filter(pk=self.account.pk).update(password_changed_at=None)
        created = self.account.created_at.astimezone(timezone.get_fixed_timezone(240))
        self.assertEqual(
            self.call("get", "").json()["password_changed_at"],
            created.replace(microsecond=0).isoformat(),
        )

    def test_no_password_is_null(self):
        ConsumerAccount.objects.filter(pk=self.account.pk).update(
            password="!", password_changed_at=timezone.now()
        )
        self.assertIsNone(self.call("get", "").json()["password_changed_at"])

    def test_both_are_always_there(self):
        body = self.call("get", "").json()
        self.assertEqual(
            list(body), ["two_factor_enabled", "methods", "password_changed_at", "sign_in_alerts"]
        )


class SecurityChangeTests(SecurityApiTestCase):
    def patch_security(self, body, client=None):
        return self.call("patch", "", body, client)

    def alerts(self):
        return ConsumerAccount.objects.get(pk=self.account.pk).sign_in_alerts

    def test_turning_them_off_answers_the_whole_get(self):
        UserSession.objects.update(location="Dubai")
        resp = self.patch_security({"sign_in_alerts": False})
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertIs(body["sign_in_alerts"], False)
        self.assertEqual(body, self.call("get", "").json())
        self.assertFalse(self.alerts())

    def test_turning_them_off_emails_the_account_once(self):
        UserSession.objects.update(location="Dubai")
        self.patch_security({"sign_in_alerts": False})
        self.patch_security({"sign_in_alerts": False})
        (msg,) = mail.outbox
        self.assertEqual((msg.to, msg.subject), ([EMAIL], "Go Style: sign-in alerts turned off"))
        self.assertTrue(
            msg.body.startswith(
                "Sign-in alerts were turned off for your Go Style account on iPhone 15 in Dubai."
                "\n\nIf this was not you, reset your password and contact support straight away."
            ),
            msg.body,
        )

    def test_turning_them_back_on_sends_nothing(self):
        self.patch_security({"sign_in_alerts": False})
        mail.outbox.clear()
        resp = self.patch_security({"sign_in_alerts": True})
        self.assertEqual((resp.status_code, resp.json()["sign_in_alerts"]), (200, True))
        self.assertTrue(self.alerts())
        self.assertEqual(mail.outbox, [])

    def test_the_value_it_has_is_200_and_nothing_else(self):
        resp = self.patch_security({"sign_in_alerts": True})
        self.assertEqual((resp.status_code, resp.json()["sign_in_alerts"]), (200, True))
        self.assertEqual(mail.outbox, [])

    def test_a_phone_only_account_is_not_emailed(self):
        ConsumerAccount.objects.filter(pk=self.account.pk).update(
            email=None, email_verified_at=None
        )
        self.assertEqual(self.patch_security({"sign_in_alerts": False}).status_code, 200)
        self.assertEqual(mail.outbox, [])

    def test_needs_no_verify_its_you_and_no_app_version(self):
        client = self.login({k: v for k, v in IPAD.items() if k != "HTTP_X_APP_VERSION"})
        self.assertEqual(self.patch_security({"sign_in_alerts": False}, client).status_code, 200)

    def test_missing_is_required(self):
        resp = self.patch_security({})
        self.assertFieldError(resp, "sign_in_alerts", "required")
        self.assertEqual(resp.json()["errors"][0]["message"], "This field is required.")

    def test_only_true_or_false(self):
        for value in ("true", "false", 1, 0, None, "", [], {}):
            with self.subTest(value=value):
                resp = self.patch_security({"sign_in_alerts": value})
                self.assertFieldError(resp, "sign_in_alerts", "invalid")
                self.assertEqual(resp.json()["errors"][0]["message"], "Must be a boolean.")
        self.assertTrue(self.alerts())

    def test_any_other_field_is_read_only_and_nothing_is_saved(self):
        for field, value in (
            ("two_factor_enabled", True),
            ("methods", {}),
            ("password_changed_at", None),
            ("nickname", "Kev"),
        ):
            with self.subTest(field=field):
                resp = self.patch_security({"sign_in_alerts": False, field: value})
                self.assertFieldError(resp, field, "read_only")
                self.assertEqual(
                    resp.json(),
                    {
                        "detail": "Please correct the highlighted fields.",
                        "code": "validation_error",
                        "errors": [
                            {
                                "field": field,
                                "code": "read_only",
                                "message": "This can’t be changed here.",
                            }
                        ],
                    },
                )
        account = ConsumerAccount.objects.get(pk=self.account.pk)
        self.assertEqual((account.sign_in_alerts, account.two_factor_enabled), (True, False))
        self.assertEqual(mail.outbox, [])

    def test_every_wrong_field_is_named(self):
        body = {"two_factor_enabled": False, "sign_in_alerts": "no", "methods": None}
        resp = self.patch_security(body)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(
            [(e["field"], e["code"]) for e in resp.json()["errors"]],
            [
                ("sign_in_alerts", "invalid"),
                ("two_factor_enabled", "read_only"),
                ("methods", "read_only"),
            ],
        )
        body = {"two_factor_enabled": False}
        self.assertEqual(
            [(e["field"], e["code"]) for e in self.patch_security(body).json()["errors"]],
            [("sign_in_alerts", "required"), ("two_factor_enabled", "read_only")],
        )


class SecurityAuthTests(SecurityApiTestCase):
    def test_no_token_or_an_expired_one_is_please_sign_in_again(self):
        stranger = APIClient()
        expired = APIClient()
        expired.credentials(HTTP_AUTHORIZATION="Bearer not-a-token")
        for client in (stranger, expired):
            for method in ("get", "patch"):
                with self.subTest(method=method, token=bool(client._credentials)):
                    resp = self.call(method, "", {"sign_in_alerts": False}, client)
                    self.assertEqual((resp.status_code, resp.json()), (401, NOT_SIGNED_IN))
                    self.assertIn("WWW-Authenticate", resp.headers)

    def test_a_signed_out_device_is_still_session_revoked(self):
        sessions.end(UserSession.objects.filter(pk=self.phone.sid))
        resp = self.call("patch", "", {"sign_in_alerts": False})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "session_revoked")

    def test_the_other_2_step_routes_keep_their_401(self):
        resp = self.call("post", "/verify/send", {}, APIClient())
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["detail"], "Authentication credentials were not provided.")

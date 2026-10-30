"""
Password age and new sign-in alerts on /me/security
(docs/SIGN_IN_ALERTS_API.md): password_changed_at and sign_in_alerts on
GET /me/security, and PATCH /me/security for the switch.
"""

import importlib
import json
import re
import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

from django.apps import apps as django_apps
from django.conf import settings
from django.core import mail
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import push_api, services, sessions, two_factor, two_factor_views
from apps.accounts.models import (
    ConsumerAccount,
    NotificationPreference,
    Purpose,
    UserSession,
    Verification,
)
from apps.accounts.notifications import send_new_sign_in_notice, send_new_sign_in_push

from .test_two_factor import (
    EMAIL,
    IPAD,
    PASSWORD,
    PHONE,
    UAE_TIME,
    SecurityApiTestCase,
    SignInTestCase,
)
from .test_account_emails import GALAXY, NO_HEADERS
from .test_sessions import SessionTestCase

backfill = importlib.import_module(
    "apps.accounts.migrations.0027_password_changed_at_sign_in_alerts"
).backfill_password_changed_at

NEW_PASSWORD = "An0ther-Str0ng-Pass!"
OTHER_PHONE = "+971509876543"
PHONE_ACCOUNT_ID = uuid.UUID("6f1c2b7e-3d4a-4c1f-8e2b-6a0d5c9b1e47")


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
        UserSession.objects.update(location="Dubai, UAE")
        resp = self.patch_security({"sign_in_alerts": False})
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertIs(body["sign_in_alerts"], False)
        self.assertEqual(body, self.call("get", "").json())
        self.assertFalse(self.alerts())

    def test_turning_them_off_emails_the_account_once(self):
        UserSession.objects.update(location="Dubai, UAE")
        self.patch_security({"sign_in_alerts": False})
        self.patch_security({"sign_in_alerts": False})
        (msg,) = mail.outbox
        self.assertEqual((msg.to, msg.subject), ([EMAIL], "Go Style: sign-in alerts turned off"))
        self.assertTrue(
            msg.body.startswith(
                "Sign-in alerts were turned off for your Go Style account"
                " on iPhone 15 in Dubai, UAE."
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


# --- the new sign-in alert ------------------------------------------------------------

NEW_SIGN_IN = "Go Style: new sign-in to your account"


class TwoStepSignInAlertTests(SignInTestCase):
    """A sign-in is a token pair from POST /auth/login or /auth/2fa/verify:
    with 2-step on, the code, not the password."""

    def alerts(self):
        return [m for m in mail.outbox if m.subject == NEW_SIGN_IN]

    def test_told_once_the_code_passes_not_at_the_password(self):
        challenge = self.challenge()
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.finish(challenge, self.sign_in_code(challenge)).status_code, 200)
        (msg,) = self.alerts()
        self.assertIn("signed in to on iPad", msg.body)

    def test_a_remembered_device_signing_in_again_is_not_news(self):
        self.signed_in()
        mail.outbox.clear()
        resp = self.sign_in()
        self.assertEqual(set(resp.json()), {"access", "refresh"})
        self.assertEqual(self.alerts(), [])

    def test_switched_off_nothing(self):
        self.call("patch", "", {"sign_in_alerts": False})
        self.signed_in()
        self.assertEqual(self.alerts(), [])


@override_settings(OTP_SENDER="live", PUSH_API_KEY="test-key")
class NewSignInPushTests(SessionTestCase):
    """The alert by push too, to the account's phones (push service)."""

    def setUp(self):
        super().setUp()
        ConsumerAccount.objects.filter(pk=self.account.pk).update(
            email=EMAIL, email_verified_at=timezone.now()
        )
        patcher = patch.object(push_api, "send_to_user", return_value=(202, {"queued": 1}))
        self.push = patcher.start()
        self.addCleanup(patcher.stop)

    def sign_in(self, device=GALAXY):
        with self.captureOnCommitCallbacks(execute=True):
            return self.login(device)

    def test_a_new_device_is_pushed_with_its_name_and_place(self):
        client = self.sign_in()
        sid = str(self.sid(client))
        self.push.assert_called_once_with(
            user_id=self.account.pk,
            event_id=f"new-sign-in:{sid}",
            title="New sign-in to your account",
            body=(
                "Your Go Style account was just signed in to on Galaxy S23 Ultra in Dubai, UAE. "
                "If this was not you, change your password."
            ),
            data={"type": "new_sign_in", "sessionId": sid},
        )
        self.assertEqual([m.subject for m in mail.outbox], [NEW_SIGN_IN])

    def test_no_news_no_push(self):
        self.sign_in()
        self.sign_in()
        self.assertEqual(self.push.call_count, 1)

    def test_alerts_off_no_push_and_no_email(self):
        ConsumerAccount.objects.filter(pk=self.account.pk).update(sign_in_alerts=False)
        self.sign_in()
        self.push.assert_not_called()
        self.assertEqual(mail.outbox, [])

    def test_push_off_in_the_preferences_still_emails(self):
        NotificationPreference.objects.create(account=self.account, push=False)
        self.sign_in()
        self.push.assert_not_called()
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(PUSH_API_KEY="")
    def test_no_push_key_no_push(self):
        self.sign_in()
        self.push.assert_not_called()

    def test_a_push_service_that_fails_does_not_fail_the_sign_in(self):
        for failure in (push_api.PushApiUnavailable("down"), ConnectionResetError("reset")):
            self.push.side_effect = failure
            with self.subTest(failure=failure), self.assertLogs(
                "apps.accounts.notifications", "WARNING"
            ) as logs:
                self.sign_in(NO_HEADERS)  # news every time
            self.assertIn("new sign-in push not sent", logs.output[0])
        self.assertEqual(len(mail.outbox), 2)

    def test_a_refused_push_is_logged(self):
        self.push.return_value = (401, {"message": "Unauthorized"})
        with self.assertLogs("apps.accounts.notifications", "WARNING") as logs:
            self.sign_in()
        self.assertIn("refused the new sign-in push (401)", logs.output[0])


class PushApiTests(SimpleTestCase):
    def test_send_to_user_posts_the_push_services_body(self):
        with patch.object(push_api, "_send", return_value=(202, {})) as send:
            push_api.send_to_user(
                user_id=PHONE_ACCOUNT_ID, event_id="e", title="t", body="b", data={"k": "v"}
            )
        send.assert_called_once_with(
            "POST",
            "/notifications/user",
            {
                "userId": str(PHONE_ACCOUNT_ID),
                "eventId": "e",
                "title": "t",
                "body": "b",
                "data": {"k": "v"},
            },
        )


# --- the FE guide ---------------------------------------------------------------------

FE_GUIDE = settings.BASE_DIR / "docs" / "SIGN_IN_ALERTS_API.md"
UAE = dt_timezone(timedelta(hours=4))


@override_settings(PUSH_API_KEY="test-key")
class SignInAlertsDocTests(SecurityApiTestCase):
    """docs/SIGN_IN_ALERTS_API.md promises every JSON block is a real
    answer. This builds the brief's account (2-step on, verified email and
    phone, password set 14 June 2026), sends the same requests and must get
    the same bodies. The emails and the push are the real words too."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = FE_GUIDE.read_text(encoding="utf-8")
        cls.blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", cls.text, re.S)]

    def setUp(self):
        super().setUp()
        ConsumerAccount.objects.filter(pk=self.account.pk).update(
            two_factor_enabled=True,
            password_changed_at=datetime(2026, 6, 14, 10, 22, 5, tzinfo=UAE),
        )

    def test_get(self):
        self.assertEqual(self.call("get", "").json(), self.blocks[0])

    def test_patch(self):
        self.assertIn('{"sign_in_alerts": false}', self.text)
        resp = self.call("patch", "", {"sign_in_alerts": False})
        self.assertEqual((resp.status_code, resp.json()), (200, self.blocks[1]))

    def test_the_error_examples(self):
        self.assertIn('{"sign_in_alerts": false, "two_factor_enabled": false}', self.text)
        resp = self.call("patch", "", {"sign_in_alerts": False, "two_factor_enabled": False})
        self.assertEqual((resp.status_code, resp.json()), (422, self.blocks[2]))
        resp = self.call("patch", "", {"sign_in_alerts": False}, APIClient())
        self.assertEqual((resp.status_code, resp.json()), (401, self.blocks[3]))

    def test_no_other_json_blocks(self):
        self.assertEqual(len(self.blocks), 4)

    def test_every_error_message_is_on_the_page(self):
        for message in (
            "This field is required.",
            "Must be a boolean.",
            two_factor_views.READ_ONLY,
            two_factor.NotSignedIn.default_detail,
        ):
            with self.subTest(message=message):
                self.assertIn(f"| {message} |", self.text)

    def test_the_emails_and_the_push_are_on_the_page(self):
        subject, what = two_factor._NOTICES[two_factor.SIGN_IN_ALERTS_OFF]
        self.assertIn(
            f"| {subject} | {what} on iPhone 15 in Dubai, UAE. If this was not you, reset your "
            "password and contact support straight away. |",
            self.text,
        )

        session = UserSession(device_name="iPhone 15", location="Dubai, UAE")
        send_new_sign_in_notice(self.account, session)
        (msg,) = mail.outbox
        self.assertIn(f'Subject "{msg.subject}"', self.text)
        text = msg.body.split("\n\nAutomated message")[0].replace("\n\n", " ")
        self.assertIn(f'Text: "{text}"', self.text)

        with patch.object(push_api, "send_to_user", return_value=(202, {})) as push:
            send_new_sign_in_push(self.account, session)
        sent = push.call_args.kwargs
        self.assertIn(f'Title "{sent["title"]}". Body "{sent["body"]}"', self.text)
        self.assertEqual(sent["data"]["type"], "new_sign_in")
        self.assertIn("Data `type`: `new_sign_in`, `sessionId`", self.text)

"""
The account emails that are not codes and not 2-step notices: the welcome,
the new sign-in alert, the phone number changed, the deletion cancelled and
the deletion done (apps/accounts/notifications.py). Each goes only to a
verified email, once, after the change it reports is committed.

The test runner keeps every message in mail.outbox; nothing leaves.
TEST_EMAIL is the address the project owner uses to check mail by hand.
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import services, sessions
from apps.accounts.models import ConsumerAccount, OtpCode, Purpose

from .test_auth import AuthTestCase
from .test_sendgrid_backend import TEST_EMAIL
from .test_sessions import CHROME, IPHONE, PASSWORD, SessionTestCase

GALAXY = {
    "HTTP_X_DEVICE_ID": "a1b2c3d4e5f60718",
    "HTTP_X_DEVICE_NAME": "Galaxy S23 Ultra",
    "HTTP_X_DEVICE_TYPE": "phone",
    "HTTP_X_PLATFORM": "android",
    "REMOTE_ADDR": "94.200.14.31",
}
NO_HEADERS = {"REMOTE_ADDR": "5.195.0.1"}


def subjects():
    return [m.subject for m in mail.outbox]


@override_settings(OTP_SENDER="live")
class WelcomeTests(AuthTestCase):
    def verify(self, destination_type, destination):
        """Ask for a register code and send it back, as the app does."""
        with self.captureOnCommitCallbacks(execute=True):
            self.request_otp(destination_type, destination, "register")
        code = (
            mail.outbox[-1].body.split("Your code: ")[1][:6]
            if destination_type == "email"
            else self.sent_phone_code
        )
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.submit_code(code, destination_type, destination, "register")
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_verifying_the_email_welcomes_by_first_name(self):
        self.register_and_authenticate("email", TEST_EMAIL)

        self.verify("email", TEST_EMAIL)

        self.assertEqual(subjects(), ["Verify your email address", "Welcome to GoStyle"])
        welcome = mail.outbox[-1]
        self.assertEqual(welcome.to, [TEST_EMAIL])
        self.assertTrue(welcome.body.startswith("Your GoStyle account is verified"))
        (html, _), = welcome.alternatives
        self.assertIn("Welcome, Kevin", html)
        # Its preview line is a welcome, not a security notice.
        self.assertIn("Your GoStyle account is verified and ready to use.", html.split("</div>")[0])

    def test_only_the_first_time(self):
        self.register_and_authenticate("email", TEST_EMAIL)
        self.verify("email", TEST_EMAIL)
        mail.outbox.clear()
        cache.clear()  # past the resend cooldown

        self.verify("email", TEST_EMAIL)

        self.assertEqual(subjects(), ["Verify your email address"])

    def test_an_account_verified_by_its_phone_gets_none(self):
        self.register_and_authenticate("phone")

        with patch("apps.accounts.notifications.WhatsAppSender.send") as whatsapp:
            with self.captureOnCommitCallbacks(execute=True):
                self.request_otp("phone", self.phone, "register")
            self.sent_phone_code = whatsapp.call_args.args[1]
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.submit_code(self.sent_phone_code, "phone", self.phone, "register")

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(mail.outbox, [])

    def test_a_wrong_code_welcomes_nobody(self):
        self.register_and_authenticate("email", TEST_EMAIL)
        with self.captureOnCommitCallbacks(execute=True):
            self.request_otp("email", TEST_EMAIL, "register")
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            self.submit_code("000000", "email", TEST_EMAIL, "register")

        self.assertEqual(mail.outbox, [])


@override_settings(OTP_SENDER="live")
class NewSignInTests(SessionTestCase):
    def setUp(self):
        super().setUp()
        ConsumerAccount.objects.filter(pk=self.account.pk).update(
            email=TEST_EMAIL, email_verified_at=timezone.now()
        )

    def sign_in(self, device):
        with self.captureOnCommitCallbacks(execute=True):
            return self.login(device)

    def test_registering_is_not_news(self):
        body = {
            "destination_type": "phone",
            "destination": "+971509876543",
            "full_name": "Kevin Rogers",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
            "gender": "male",
        }
        with patch.object(sessions, "send_new_sign_in_notice") as notice:
            with self.captureOnCommitCallbacks(execute=True):
                resp = APIClient().post("/api/v1/auth/register", body, format="json", **IPHONE)
            self.assertEqual(resp.status_code, 201, resp.content)
            notice.assert_not_called()

            self.sign_in(IPHONE)
            notice.assert_called_once()

    def test_a_device_with_no_live_session_is_news(self):
        self.sign_in(IPHONE)

        (msg,) = mail.outbox
        self.assertIn("signed in to on iPhone 15 in Dubai, UAE.", msg.body)

    def test_the_same_device_while_signed_in_is_not_news(self):
        self.sign_in(IPHONE)
        self.sign_in(IPHONE)

        self.assertEqual(len(mail.outbox), 1)

    def test_a_new_device_is_named_with_its_place(self):
        self.sign_in(IPHONE)
        mail.outbox.clear()

        self.sign_in(GALAXY)

        (msg,) = mail.outbox
        self.assertEqual((msg.to, msg.subject), ([TEST_EMAIL], "Go Style: new sign-in to your account"))
        self.assertTrue(
            msg.body.startswith(
                "Your Go Style account was just signed in to"
                " on Galaxy S23 Ultra in Dubai, UAE."
            ),
            msg.body,
        )
        (html, _), = msg.alternatives
        self.assertIn("New sign-in to your account", html)

    def test_a_device_signed_out_is_new_again(self):
        self.sign_in(GALAXY)
        self.account.sessions.update(ended_at=timezone.now())

        self.sign_in(GALAXY)

        self.assertEqual(len(mail.outbox), 2)

    def test_a_device_whose_sign_in_ran_out_is_new_again(self):
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

        self.sign_in(GALAXY)
        OutstandingToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        self.sign_in(GALAXY)

        self.assertEqual(len(mail.outbox), 2)

    def test_a_browser_is_news_every_time(self):
        self.sign_in(CHROME)
        self.sign_in(CHROME)

        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("on Chrome on macOS in Abu Dhabi, UAE.", mail.outbox[1].body)

    def test_a_client_with_no_device_id_is_news_every_time(self):
        self.sign_in(NO_HEADERS)
        self.sign_in(NO_HEADERS)

        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("signed in to on an unknown device", mail.outbox[1].body)

    def test_an_unverified_email_is_not_written_to(self):
        ConsumerAccount.objects.filter(pk=self.account.pk).update(email_verified_at=None)
        self.sign_in(IPHONE)

        self.sign_in(GALAXY)

        self.assertEqual(mail.outbox, [])

    def test_a_wrong_password_sends_nothing(self):
        body = {"destination_type": "phone", "destination": self.account.phone, "password": "x"}

        with self.captureOnCommitCallbacks(execute=True):
            APIClient().post("/api/v1/auth/login", body, format="json", **GALAXY)

        self.assertEqual(mail.outbox, [])

    def test_a_token_refresh_is_not_a_sign_in(self):
        client = self.older_client()  # its refresh opens a session

        with self.captureOnCommitCallbacks(execute=True):
            resp = APIClient().post(
                "/api/v1/auth/token/refresh",
                {"refresh": client.tokens["refresh"]},
                format="json",
                **GALAXY,
            )

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.account.sessions.count(), 1)
        self.assertEqual(mail.outbox, [])

    def test_sign_in_alerts_turned_off_send_nothing(self):
        ConsumerAccount.objects.filter(pk=self.account.pk).update(sign_in_alerts=False)

        self.sign_in(GALAXY)
        self.sign_in(CHROME)

        self.assertEqual(mail.outbox, [])

    @override_settings(DEVICE_SESSIONS_V1=False)
    def test_without_sessions_there_is_nothing_to_compare(self):
        self.sign_in(IPHONE)
        self.sign_in(GALAXY)

        self.assertEqual(mail.outbox, [])


@override_settings(OTP_SENDER="live")
class PhoneChangedTests(AuthTestCase):
    NEW_PHONE = "+8801799999999"

    def setUp(self):
        super().setUp()
        self.register(destination_type="phone")
        self.account = ConsumerAccount.objects.get(phone=self.phone)

    def change_phone(self):
        otp = OtpCode(
            destination=self.NEW_PHONE, destination_type="phone", purpose=Purpose.CHANGE_CONTACT
        )
        otp.set_code("123456", ttl_seconds=600)
        otp.save()
        with patch.object(services, "_live_change_token"):
            with self.captureOnCommitCallbacks(execute=True):
                services.confirm_contact_change(
                    self.account, "token", self.NEW_PHONE, "phone", "123456", "127.0.0.1"
                )

    def test_the_verified_email_hears_of_it(self):
        ConsumerAccount.objects.filter(pk=self.account.pk).update(
            email=TEST_EMAIL, email_verified_at=timezone.now()
        )
        self.account.refresh_from_db()

        self.change_phone()

        (msg,) = mail.outbox
        self.assertEqual((msg.to, msg.subject), ([TEST_EMAIL], "Your Go Style account details changed"))
        self.assertTrue(
            msg.body.startswith("The phone number on your Go Style account was just changed.")
        )
        self.assertEqual(ConsumerAccount.objects.get(pk=self.account.pk).phone, self.NEW_PHONE)

    def test_no_verified_email_no_notice(self):
        self.change_phone()

        self.assertEqual(mail.outbox, [])

    def test_a_changed_email_tells_the_old_address_only(self):
        ConsumerAccount.objects.filter(pk=self.account.pk).update(
            email=TEST_EMAIL, email_verified_at=timezone.now()
        )
        self.account.refresh_from_db()
        otp = OtpCode(
            destination="new@example.com", destination_type="email", purpose=Purpose.CHANGE_CONTACT
        )
        otp.set_code("123456", ttl_seconds=600)
        otp.save()

        with patch.object(services, "_live_change_token"):
            with self.captureOnCommitCallbacks(execute=True):
                services.confirm_contact_change(
                    self.account, "token", "new@example.com", "email", "123456", "127.0.0.1"
                )

        (msg,) = mail.outbox
        self.assertEqual((msg.to, msg.subject), ([TEST_EMAIL], "Your Go Style account details changed"))
        self.assertTrue(msg.body.startswith("The email on your Go Style account was just changed."))
        self.assertEqual(ConsumerAccount.objects.get(pk=self.account.pk).email, "new@example.com")


@override_settings(OTP_SENDER="live")
class DeletionEmailTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.register_and_authenticate("email", TEST_EMAIL)
        ConsumerAccount.objects.filter(email=TEST_EMAIL).update(email_verified_at=timezone.now())

    def post(self, path, body=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(path, body or {}, format="json")

    def test_cancelling_a_scheduled_deletion_says_so(self):
        self.post("/api/v1/auth/me/delete", {"password": PASSWORD})
        mail.outbox.clear()

        resp = self.post("/api/v1/auth/me/delete/cancel")

        self.assertEqual(resp.status_code, 204)
        self.assertEqual(subjects(), ["Go Style: your account will not be deleted"])
        self.assertEqual(mail.outbox[0].to, [TEST_EMAIL])

    def test_cancelling_when_nothing_is_scheduled_sends_nothing(self):
        resp = self.post("/api/v1/auth/me/delete/cancel")

        self.assertEqual(resp.status_code, 204)
        self.assertEqual(mail.outbox, [])

    def purge_due(self):
        ConsumerAccount.objects.filter(email=TEST_EMAIL).update(
            purge_at=timezone.now() - timedelta(minutes=1)
        )
        with self.captureOnCommitCallbacks(execute=True):
            call_command("purge_due_accounts", stdout=StringIO())

    def test_the_purge_tells_the_address_the_account_had(self):
        pk = ConsumerAccount.objects.get(email=TEST_EMAIL).pk

        self.purge_due()

        self.assertEqual(subjects(), ["Go Style: your account has been deleted"])
        self.assertEqual(mail.outbox[0].to, [TEST_EMAIL])
        # The address went with the account; the email is the last of it.
        self.assertIsNone(ConsumerAccount.all_objects.get(pk=pk).email)

    def test_an_unverified_address_is_not_told(self):
        ConsumerAccount.objects.filter(email=TEST_EMAIL).update(email_verified_at=None)

        self.purge_due()

        self.assertEqual(mail.outbox, [])

    def test_a_purge_that_fails_sends_nothing(self):
        ConsumerAccount.objects.filter(email=TEST_EMAIL).update(
            purge_at=timezone.now() - timedelta(minutes=1)
        )
        with patch.object(ConsumerAccount, "set_unusable_password", side_effect=RuntimeError):
            with self.assertLogs("apps.accounts.management.commands.purge_due_accounts", "ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    with self.assertRaises(Exception):
                        call_command("purge_due_accounts", stdout=StringIO())

        self.assertEqual(mail.outbox, [])

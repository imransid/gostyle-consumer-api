"""
Tests that an OTP request over the real endpoints becomes the right email.

Three things are pinned down:

  * Each OTP purpose gets its own template and subject (register, password
    reset, and the generic one for everything else), greeting the account
    holder by first name.
  * The code in the email is the one that was stored: the email is not a
    decoration around a different number.
  * A send SendGrid refuses is raised, not swallowed, and nothing else (SMTP,
    another backend) picks it up.

Django's test runner swaps EMAIL_BACKEND for the in-memory one, so `mail.outbox`
is what was sent unless a test installs the SendGrid backend, and then the SDK
client class is replaced: nothing here reaches SendGrid.
TEST_EMAIL is the address the project owner uses to check mail by hand.
"""

import re
from unittest import mock

from django.core import mail
from django.test import override_settings

from apps.accounts.models import ConsumerAccount, OtpCode
from apps.accounts.sendgrid_backend import SendGridError

from . import test_auth
from .test_sendgrid_backend import BACKEND, FAKE_KEY, TEST_EMAIL, fake_response, sdk_error

SENDER = "GoStyle <noreply@gostyle.uk>"
CODE_IN_TEXT = re.compile(r"Your code: (\d{6})")


@override_settings(OTP_SENDER="live", DEFAULT_FROM_EMAIL=SENDER)
class EmailOtpDeliveryTests(test_auth.AuthTestCase):
    def setUp(self):
        super().setUp()
        self.email = TEST_EMAIL

    def request_and_deliver(self, purpose):
        """POST /auth/otp/request as the signed-in account and run the
        on-commit hook that actually sends."""
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.request_otp("email", self.email, purpose)
        self.assertEqual(resp.status_code, 200, resp.data)

    def forgot_and_deliver(self, destination):
        self.anonymous()
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                "/api/v1/auth/password/forgot",
                {"destination_type": "email", "destination": destination},
                format="json",
            )

    def only_message(self):
        self.assertEqual(len(mail.outbox), 1, "exactly one email should have gone out")
        return mail.outbox[0]

    def html_of(self, message):
        (html, mimetype), = message.alternatives
        self.assertEqual(mimetype, "text/html")
        return html

    # --- the three templates ---------------------------------------------
    def test_register_code_uses_the_registration_template_and_greets_by_first_name(self):
        self.register_and_authenticate("email", self.email)

        self.request_and_deliver("register")

        message = self.only_message()
        self.assertEqual(message.to, [self.email])
        self.assertEqual(message.from_email, SENDER)
        self.assertEqual(message.subject, "Verify your email address")
        self.assertTrue(message.body.startswith("Hi Kevin,"))
        self.assertIn("Hi Kevin,", self.html_of(message))
        self.assertIn("finish creating your account", self.html_of(message))

    def test_password_reset_code_uses_the_reset_template(self):
        self.register(destination_type="email", destination=self.email)

        resp = self.forgot_and_deliver(self.email)

        self.assertEqual(resp.status_code, 200)
        message = self.only_message()
        self.assertEqual(message.to, [self.email])
        self.assertEqual(message.subject, "Reset your password")
        self.assertIn("Hi Kevin,", self.html_of(message))
        self.assertIn("reset the password", self.html_of(message))
        self.assertNotIn("signing up", self.html_of(message))

    def test_any_other_purpose_gets_the_generic_template(self):
        self.register_and_authenticate("email", self.email)

        self.request_and_deliver("login")

        message = self.only_message()
        self.assertEqual(message.subject, "Your verification code")
        self.assertIn("Confirm it’s you", self.html_of(message))

    # --- the code ---------------------------------------------------------
    def test_the_code_in_both_parts_is_the_one_that_was_stored(self):
        self.register_and_authenticate("email", self.email)

        self.request_and_deliver("register")

        message = self.only_message()
        code = CODE_IN_TEXT.search(message.body).group(1)
        self.assertIn(code, self.html_of(message))
        stored = OtpCode.objects.get(destination=self.email, purpose="register", consumed_at__isnull=True)
        self.assertTrue(stored.check_code(code))

    def test_the_email_states_the_real_lifetime_of_the_code(self):
        self.register_and_authenticate("email", self.email)

        self.request_and_deliver("register")

        message = self.only_message()
        stored = OtpCode.objects.get(destination=self.email, purpose="register", consumed_at__isnull=True)
        minutes = round((stored.expires_at - stored.created_at).total_seconds() / 60)
        self.assertIn(f"expires in {minutes} minutes", message.body)

    # --- who gets nothing --------------------------------------------------
    def test_a_password_reset_for_an_unknown_address_sends_nothing_and_answers_the_same(self):
        self.register(destination_type="email", destination=self.email)
        known = self.forgot_and_deliver(self.email)
        mail.outbox.clear()

        unknown = self.forgot_and_deliver("nobody@example.com")

        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(unknown.status_code, known.status_code)
        self.assertEqual(unknown.data, known.data)

    def test_a_phone_code_never_becomes_an_email(self):
        self.register_and_authenticate("phone")

        with mock.patch("apps.accounts.notifications.WhatsAppSender.send") as whatsapp:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.request_otp("phone", self.phone, "register")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        whatsapp.assert_called_once()
        # The phone path is told the purpose too and takes it without complaint;
        # it has no greeting to give.
        self.assertEqual(whatsapp.call_args.kwargs["purpose"], "register")
        self.assertIsNone(whatsapp.call_args.kwargs["first_name"])

    def test_the_console_sender_still_takes_the_new_arguments(self):
        self.register_and_authenticate("email", self.email)

        with override_settings(OTP_SENDER="console"):
            with mock.patch("builtins.print") as printed:
                with self.captureOnCommitCallbacks(execute=True):
                    resp = self.request_otp("email", self.email, "register")

        self.assertEqual(resp.status_code, 200)
        printed.assert_called_once()
        self.assertEqual(len(mail.outbox), 0)

    def test_a_name_with_markup_cannot_reach_the_html(self):
        self.register(
            destination_type="email", destination=self.email, full_name="<b>Eve</b> Mallory"
        )

        self.forgot_and_deliver(self.email)

        html = self.html_of(self.only_message())
        self.assertNotIn("<b>Eve</b>", html)
        self.assertIn("&lt;b&gt;Eve&lt;/b&gt;", html)

    def test_an_account_with_no_name_gets_the_nameless_greeting(self):
        self.register(destination_type="email", destination=self.email)
        ConsumerAccount.objects.filter(email=self.email).update(full_name="")

        self.forgot_and_deliver(self.email)

        message = self.only_message()
        self.assertTrue(message.body.startswith("Reset your password"))


@override_settings(
    OTP_SENDER="live",
    EMAIL_BACKEND=BACKEND,
    SENDGRID_API_KEY=FAKE_KEY,
    DEFAULT_FROM_EMAIL=SENDER,
)
class SendGridOtpDeliveryTests(test_auth.AuthTestCase):
    """The same flows with the SendGrid backend installed and its SDK stubbed:
    the whole path from endpoint to the request body SendGrid would receive."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("apps.accounts.sendgrid_backend.SendGridAPIClient")
        self.client_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.sdk = self.client_class.return_value
        self.sdk.send.return_value = fake_response()

    def test_a_register_code_reaches_sendgrid_addressed_to_the_account(self):
        self.register_and_authenticate("email", TEST_EMAIL)

        with self.captureOnCommitCallbacks(execute=True):
            resp = self.request_otp("email", TEST_EMAIL, "register")

        self.assertEqual(resp.status_code, 200)
        self.sdk.send.assert_called_once()
        payload = self.sdk.send.call_args.args[0]
        self.assertEqual(payload["from"], {"email": "noreply@gostyle.uk", "name": "GoStyle"})
        self.assertEqual(payload["personalizations"], [{"to": [{"email": TEST_EMAIL}]}])
        self.assertEqual(payload["subject"], "Verify your email address")
        self.assertEqual([c["type"] for c in payload["content"]], ["text/plain", "text/html"])
        code = CODE_IN_TEXT.search(payload["content"][0]["value"]).group(1)
        self.assertIn(code, payload["content"][1]["value"])
        # The locmem outbox is not in play: this really went to the SendGrid backend.
        self.assertEqual(len(mail.outbox), 0)

    def test_a_reset_code_reaches_sendgrid_with_the_reset_subject(self):
        self.register(destination_type="email", destination=TEST_EMAIL)
        self.anonymous()

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                "/api/v1/auth/password/forgot",
                {"destination_type": "email", "destination": TEST_EMAIL},
                format="json",
            )

        self.assertEqual(self.sdk.send.call_args.args[0]["subject"], "Reset your password")

    def test_a_refused_send_is_raised_and_nothing_else_picks_it_up(self):
        self.sdk.send.side_effect = sdk_error(
            403, {"errors": [{"message": "The from address does not match a verified Sender Identity.", "field": "from"}]}
        )
        self.register_and_authenticate("email", TEST_EMAIL)

        with mock.patch("smtplib.SMTP") as smtp, mock.patch("smtplib.SMTP_SSL") as smtp_ssl:
            with self.assertRaisesRegex(SendGridError, "HTTP 403"):
                with self.captureOnCommitCallbacks(execute=True):
                    self.request_otp("email", TEST_EMAIL, "register")

        self.assertEqual(self.sdk.send.call_count, 1, "a refusal is not retried here")
        smtp.assert_not_called()
        smtp_ssl.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(SENDGRID_API_KEY="")
    def test_a_missing_key_fails_loudly_instead_of_dropping_the_code(self):
        from django.core.exceptions import ImproperlyConfigured

        self.register_and_authenticate("email", TEST_EMAIL)

        with self.assertRaises(ImproperlyConfigured):
            with self.captureOnCommitCallbacks(execute=True):
                self.request_otp("email", TEST_EMAIL, "register")

        self.client_class.assert_not_called()

    def test_a_security_notice_reaches_sendgrid_as_text_and_html(self):
        from django.utils import timezone

        self.register_and_authenticate("email", TEST_EMAIL)
        ConsumerAccount.objects.filter(email=TEST_EMAIL).update(email_verified_at=timezone.now())

        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                "/api/v1/auth/password/change",
                {"old_password": test_auth.PASSWORD, "new_password": "An0ther-Str0ng-Pass!"},
                format="json",
            )

        self.assertEqual(resp.status_code, 200)
        payload = self.sdk.send.call_args.args[0]
        self.assertEqual(payload["subject"], "Go Style: your password was changed")
        self.assertEqual(payload["personalizations"], [{"to": [{"email": TEST_EMAIL}]}])
        self.assertEqual([c["type"] for c in payload["content"]], ["text/plain", "text/html"])

    def test_a_refused_notice_does_not_fail_the_change_it_reports(self):
        from django.utils import timezone

        self.sdk.send.side_effect = sdk_error(500, {"errors": [{"message": "boom"}]})
        self.register_and_authenticate("email", TEST_EMAIL)
        ConsumerAccount.objects.filter(email=TEST_EMAIL).update(email_verified_at=timezone.now())

        with self.assertLogs("apps.accounts.sendgrid_backend", level="ERROR"):
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(
                    "/api/v1/auth/password/change",
                    {"old_password": test_auth.PASSWORD, "new_password": "An0ther-Str0ng-Pass!"},
                    format="json",
                )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            ConsumerAccount.objects.get(email=TEST_EMAIL).check_password("An0ther-Str0ng-Pass!")
        )

"""
`manage.py send_test_email`: one real template to one address, to see it and
prove the SendGrid setup. Here the SDK client is replaced; nothing leaves.
"""

from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from .test_sendgrid_backend import BACKEND, FAKE_KEY, TEST_EMAIL, fake_response, sdk_error


@override_settings(EMAIL_BACKEND=BACKEND, SENDGRID_API_KEY=FAKE_KEY)
class SendTestEmailTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch("apps.accounts.sendgrid_backend.SendGridAPIClient")
        self.sdk = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.sdk.send.return_value = fake_response()

    def run_command(self, *args):
        out = StringIO()
        call_command("send_test_email", "--to", TEST_EMAIL, *args, stdout=out)
        return out.getvalue()

    def payload(self):
        return self.sdk.send.call_args.args[0]

    def test_the_default_is_the_register_code_with_the_placeholder(self):
        out = self.run_command()

        self.assertIn("accepted for processing, not delivered", out)
        self.assertEqual(self.payload()["subject"], "Verify your email address")
        self.assertEqual(self.payload()["personalizations"], [{"to": [{"email": TEST_EMAIL}]}])
        self.assertIn("Your code: 000000", self.payload()["content"][0]["value"])

    def test_a_first_name_greets(self):
        self.run_command("--template", "password_reset", "--first-name", "Sara")

        self.assertTrue(self.payload()["content"][0]["value"].startswith("Hi Sara,"))

    def test_every_template_sends(self):
        from apps.accounts.management.commands.send_test_email import (
            CODE_TEMPLATES,
            NOTICE_TEMPLATES,
        )

        for template in [*CODE_TEMPLATES, *NOTICE_TEMPLATES]:
            with self.subTest(template):
                self.run_command("--template", template)
                types = [c["type"] for c in self.payload()["content"]]
                self.assertEqual(types, ["text/plain", "text/html"])

        self.assertEqual(self.sdk.send.call_count, len(CODE_TEMPLATES) + len(NOTICE_TEMPLATES))

    def test_two_step_codes_carry_their_own_subjects(self):
        self.run_command("--template", "two_factor_sign_in")
        self.assertEqual(self.payload()["subject"], "Your Go Style sign-in code")

    def test_a_refusal_is_a_command_error_with_sendgrids_reason(self):
        self.sdk.send.side_effect = sdk_error(
            403, {"errors": [{"message": "The from address does not match a verified Sender Identity."}]}
        )

        for template in ("register", "notice_password_changed"):
            with self.subTest(template):
                with self.assertLogs("apps.accounts.sendgrid_backend", level="ERROR"):
                    with self.assertRaisesRegex(CommandError, "verified Sender Identity"):
                        self.run_command("--template", template)

    @override_settings(SENDGRID_API_KEY="")
    def test_a_missing_key_is_a_command_error_naming_it(self):
        with self.assertRaisesRegex(CommandError, "SENDGRID_API_KEY"):
            self.run_command()

    def test_a_bad_address_sends_nothing(self):
        with self.assertRaisesRegex(CommandError, "not a valid email"):
            call_command("send_test_email", "--to", "not-an-address", stdout=StringIO())

        self.sdk.send.assert_not_called()

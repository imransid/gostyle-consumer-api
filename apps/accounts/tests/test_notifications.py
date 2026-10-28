"""
The senders and notices in apps/accounts/notifications.py. The test runner's
email backend keeps every message in mail.outbox; nothing leaves.

TEST_EMAIL is the address the project owner uses to check mail by hand.
"""

import io
import json
from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from apps.accounts import services
from apps.accounts.models import ConsumerAccount
from apps.accounts.notifications import (
    ConsoleSender,
    EmailSender,
    WhatsAppSender,
    send_contact_change_notice,
    send_deletion_scheduled_notice,
    send_password_changed_notice,
    send_security_notice,
    verified_email,
)

from .test_auth import PASSWORD, AuthTestCase
from .test_sendgrid_backend import TEST_EMAIL

EMAIL = TEST_EMAIL
PHONE = "+971501234567"
FOOTER = "Automated message from GoStyle. Replies to this address are not read."


def html_of(msg):
    (html, mimetype), = msg.alternatives
    assert mimetype == "text/html", mimetype
    return html


class EmailSenderTests(SimpleTestCase):
    def test_says_how_long_the_code_really_lasts(self):
        self.assertEqual(services.OTP_TTL_SECONDS, 600)
        EmailSender().send(EMAIL, "123456", "email")
        (msg,) = mail.outbox
        self.assertEqual(msg.subject, "Your verification code")
        self.assertIn("Your code: 123456\nIt expires in 10 minutes.", msg.body)
        html = html_of(msg)
        self.assertIn("Expires in 10 minutes", html)
        self.assertIn("It expires in 10 minutes.", html)  # the inbox preview line
        self.assertNotIn("5 minutes", msg.body + html)

    def test_a_subject_and_minutes_of_its_own(self):
        EmailSender().send(
            EMAIL, "123456", "email", subject="Your Go Style security code", minutes=7
        )
        (msg,) = mail.outbox
        self.assertEqual(msg.subject, "Your Go Style security code")
        self.assertIn("It expires in 7 minutes.", msg.body)
        self.assertIn("Expires in 7 minutes", html_of(msg))
        # The generic words: the subject is the only thing 2-step changes.
        self.assertIn("Confirm it’s you", html_of(msg))

    def test_the_purpose_picks_the_template_and_the_name_greets(self):
        EmailSender().send(EMAIL, "123456", "email", purpose="password_reset", first_name="Sara")
        (msg,) = mail.outbox
        self.assertEqual(msg.subject, "Reset your password")
        self.assertTrue(msg.body.startswith("Hi Sara,"))
        self.assertIn("reset the password", html_of(msg))

    def test_it_comes_from_the_sendgrid_sender(self):
        EmailSender().send(EMAIL, "123456", "email")
        self.assertEqual(
            mail.outbox[0].from_email,
            f"{settings.SENDGRID_FROM_NAME} <{settings.SENDGRID_FROM_EMAIL}>",
        )


class OtherSendersTests(SimpleTestCase):
    def test_the_console_takes_every_argument_and_prints_the_code(self):
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            ConsoleSender().send(
                EMAIL, "123456", "email", subject="S", minutes=7, purpose="register", first_name="K"
            )
        self.assertIn(f">>> OTP for {EMAIL} [email]: 123456", out.getvalue())

    @override_settings(
        WHATSAPP_PHONE_NUMBER_ID="1234",
        WHATSAPP_ACCESS_TOKEN="token",
        WHATSAPP_TEMPLATE_NAME="otp",
        WHATSAPP_TEMPLATE_LANGUAGE="en",
    )
    def test_whatsapp_takes_every_argument_and_sends_the_same_template(self):
        bodies = []
        for kwargs in ({}, {"subject": "S", "minutes": 7, "purpose": "register", "first_name": "K"}):
            with patch("urllib.request.urlopen") as urlopen:
                WhatsAppSender().send(PHONE, "123456", "phone", **kwargs)
            bodies.append(json.loads(urlopen.call_args.args[0].data))
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[0]["to"], "971501234567")


@override_settings(OTP_SENDER="live")
class SecurityNoticeTests(SimpleTestCase):
    def test_emails_the_message_with_the_branded_html(self):
        send_security_notice(
            EMAIL,
            "Go Style: authenticator app removed",
            "It was removed.\n\nIf this was not you, contact support.",
            headline="Authenticator app removed",
        )
        (msg,) = mail.outbox
        self.assertEqual(
            (msg.to, msg.subject, msg.body),
            (
                [EMAIL],
                "Go Style: authenticator app removed",
                f"It was removed.\n\nIf this was not you, contact support.\n\n{FOOTER}",
            ),
        )
        html = html_of(msg)
        self.assertIn("Authenticator app removed", html)
        self.assertIn("It was removed.", html)
        self.assertIn("If this was not you, contact support.", html)

    def test_the_headline_defaults_to_the_subject(self):
        send_security_notice(EMAIL, "Something changed", "It was removed.")
        self.assertIn("Something changed", html_of(mail.outbox[0]))

    def test_a_refused_send_is_swallowed_the_change_is_already_made(self):
        with patch("django.core.mail.EmailMultiAlternatives.send") as send:
            send_security_notice(EMAIL, "S", "M")
        send.assert_called_once_with(fail_silently=True)

    @override_settings(OTP_SENDER="console")
    def test_the_console_prints_it_instead(self):
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            send_security_notice(EMAIL, "S", "It was removed.")
        self.assertIn(f">>> NOTICE to {EMAIL}: It was removed.", out.getvalue())
        self.assertEqual(mail.outbox, [])


@override_settings(OTP_SENDER="live")
class ContactChangeNoticeTests(SimpleTestCase):
    def test_emails_the_old_address(self):
        send_contact_change_notice(EMAIL, "email", "email")
        (msg,) = mail.outbox
        self.assertEqual((msg.to, msg.subject), ([EMAIL], "Your Go Style account details changed"))
        self.assertTrue(msg.body.startswith("The email on your Go Style account was just changed."))
        self.assertIn("Your account details changed", html_of(msg))

    def test_a_phone_gets_nothing(self):
        send_contact_change_notice(PHONE, "phone", "phone")
        self.assertEqual(mail.outbox, [])

    @override_settings(OTP_SENDER="console")
    def test_the_console_prints_it_instead(self):
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            send_contact_change_notice(EMAIL, "email", "email")
        self.assertIn(f">>> NOTICE to {EMAIL}: your email was changed", out.getvalue())
        self.assertEqual(mail.outbox, [])


@override_settings(OTP_SENDER="live")
class AccountNoticeTests(SimpleTestCase):
    """The two notices that take the account: only ever to a verified email."""

    def account(self, verified=True, **fields):
        return ConsumerAccount(
            email=EMAIL,
            email_verified_at=timezone.now() if verified else None,
            **fields,
        )

    def test_verified_email_is_only_a_verified_one(self):
        self.assertEqual(verified_email(self.account()), EMAIL)
        self.assertIsNone(verified_email(self.account(verified=False)))
        self.assertIsNone(verified_email(ConsumerAccount(phone=PHONE)))

    def test_password_changed(self):
        send_password_changed_notice(self.account())
        (msg,) = mail.outbox
        self.assertEqual((msg.to, msg.subject), ([EMAIL], "Go Style: your password was changed"))
        self.assertTrue(msg.body.startswith("The password for your Go Style account was just changed."))
        self.assertIn("Your password was changed", html_of(msg))

    def test_password_changed_skips_an_unverified_email(self):
        send_password_changed_notice(self.account(verified=False))
        self.assertEqual(mail.outbox, [])

    @override_settings(TIME_ZONE="UTC")
    def test_deletion_scheduled_says_when(self):
        purge_at = datetime(2026, 11, 27, 9, 30, tzinfo=dt_timezone.utc)
        send_deletion_scheduled_notice(self.account(purge_at=purge_at))
        (msg,) = mail.outbox
        self.assertEqual(msg.subject, "Go Style: your account is scheduled for deletion")
        self.assertIn("will be deleted on 27 November 2026.", msg.body)
        self.assertIn("will be deleted on 27 November 2026.", html_of(msg))

    def test_deletion_scheduled_skips_an_unverified_email_and_no_date(self):
        send_deletion_scheduled_notice(self.account(verified=False, purge_at=timezone.now()))
        send_deletion_scheduled_notice(self.account(purge_at=None))
        self.assertEqual(mail.outbox, [])


@override_settings(OTP_SENDER="live")
class AccountNoticeFlowTests(AuthTestCase):
    """Through the real endpoints: each change sends its one notice."""

    def setUp(self):
        super().setUp()
        self.register_and_authenticate("email", EMAIL)
        ConsumerAccount.objects.filter(email=EMAIL).update(email_verified_at=timezone.now())

    def subjects(self):
        return [m.subject for m in mail.outbox]

    def test_changing_the_password_sends_the_notice(self):
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                "/api/v1/auth/password/change",
                {"old_password": PASSWORD, "new_password": "An0ther-Str0ng-Pass!"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self.subjects(), ["Go Style: your password was changed"])
        self.assertEqual(mail.outbox[0].to, [EMAIL])

    def test_a_wrong_old_password_sends_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                "/api/v1/auth/password/change",
                {"old_password": "wrong-password", "new_password": "An0ther-Str0ng-Pass!"},
                format="json",
            )
        self.assertNotEqual(resp.status_code, 200)
        self.assertEqual(mail.outbox, [])

    def test_resetting_the_password_sends_the_code_then_the_notice(self):
        self.anonymous()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                "/api/v1/auth/password/forgot",
                {"destination_type": "email", "destination": EMAIL},
                format="json",
            )
        code = mail.outbox[0].body.split("Your code: ")[1][:6]
        token = self.client.post(
            "/api/v1/auth/password/verify",
            {"destination_type": "email", "destination": EMAIL, "code": code},
            format="json",
        ).data["reset_token"]
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                "/api/v1/auth/password/reset",
                {"reset_token": token, "new_password": "An0ther-Str0ng-Pass!"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(
            self.subjects(), ["Reset your password", "Go Style: your password was changed"]
        )

    def test_scheduling_a_deletion_sends_the_notice(self):
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                "/api/v1/auth/me/delete", {"password": PASSWORD, "reason": "privacy"}, format="json"
            )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self.subjects(), ["Go Style: your account is scheduled for deletion"])
        purge_at = timezone.localtime(ConsumerAccount.objects.get(email=EMAIL).purge_at)
        self.assertIn(f"deleted on {purge_at.day} {purge_at:%B %Y}.", mail.outbox[0].body)

    def test_a_wrong_password_schedules_nothing_and_sends_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post("/api/v1/auth/me/delete", {"password": "wrong"}, format="json")
        self.assertEqual(mail.outbox, [])

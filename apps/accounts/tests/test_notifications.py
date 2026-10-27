"""
The senders and notices in apps/accounts/notifications.py. The test runner's
email backend keeps every message in mail.outbox; nothing leaves.
"""

import io
import json
from unittest.mock import patch

from django.core import mail
from django.test import SimpleTestCase, override_settings

from apps.accounts import services
from apps.accounts.notifications import (
    ConsoleSender,
    EmailSender,
    WhatsAppSender,
    send_security_notice,
)

EMAIL = "kevin.new@gmail.com"
PHONE = "+971501234567"


class EmailSenderTests(SimpleTestCase):
    def test_says_how_long_the_code_really_lasts(self):
        self.assertEqual(services.OTP_TTL_SECONDS, 600)
        EmailSender().send(EMAIL, "123456", "email")
        (msg,) = mail.outbox
        self.assertEqual(msg.subject, "Your Go Style verification code")
        self.assertIn("Your verification code is 123456. It expires in 10 minutes.", msg.body)
        html, mimetype = msg.alternatives[0]
        self.assertEqual(mimetype, "text/html")
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
        self.assertIn("Expires in 7 minutes", msg.alternatives[0][0])


class OtherSendersTests(SimpleTestCase):
    def test_the_console_takes_a_subject_and_minutes_and_prints_the_code(self):
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            ConsoleSender().send(EMAIL, "123456", "email", subject="S", minutes=7)
        self.assertIn(f">>> OTP for {EMAIL} [email]: 123456", out.getvalue())

    @override_settings(
        WHATSAPP_PHONE_NUMBER_ID="1234",
        WHATSAPP_ACCESS_TOKEN="token",
        WHATSAPP_TEMPLATE_NAME="otp",
        WHATSAPP_TEMPLATE_LANGUAGE="en",
    )
    def test_whatsapp_takes_a_subject_and_minutes_and_sends_the_same_template(self):
        bodies = []
        for kwargs in ({}, {"subject": "S", "minutes": 7}):
            with patch("urllib.request.urlopen") as urlopen:
                WhatsAppSender().send(PHONE, "123456", "phone", **kwargs)
            bodies.append(json.loads(urlopen.call_args.args[0].data))
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[0]["to"], "971501234567")


class SecurityNoticeTests(SimpleTestCase):
    @override_settings(OTP_SENDER="smtp")
    def test_emails_the_message(self):
        send_security_notice(EMAIL, "Go Style: authenticator app removed", "It was removed.")
        (msg,) = mail.outbox
        self.assertEqual(
            (msg.to, msg.subject, msg.body),
            ([EMAIL], "Go Style: authenticator app removed", "It was removed."),
        )

    @override_settings(OTP_SENDER="console")
    def test_the_console_prints_it_instead(self):
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            send_security_notice(EMAIL, "S", "It was removed.")
        self.assertIn(f">>> NOTICE to {EMAIL}: It was removed.", out.getvalue())
        self.assertEqual(mail.outbox, [])

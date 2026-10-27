"""
The senders and notices in apps/accounts/notifications.py. The test runner's
email backend keeps every message in mail.outbox; nothing leaves.
"""

from django.core import mail
from django.test import SimpleTestCase

from apps.accounts import services
from apps.accounts.notifications import EmailSender

EMAIL = "kevin.new@gmail.com"


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

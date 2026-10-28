"""
Tests for the SendGrid email backend (apps/accounts/sendgrid_backend.py).

NOTHING HERE REACHES THE NETWORK. Every test either replaces the SDK client
class or, in `RealSdkTests`, runs the real SDK against a fake urllib opener, so
the request is built and the answer parsed by the real code but never sent.
`SENDGRID_API_KEY` is always a made-up value, whatever is in .env.

TEST_EMAIL is the address the project owner uses to check mail by hand.
"""

import base64
import email.message
import io
import json
import urllib.error
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.core.mail import (
    BadHeaderError,
    EmailMessage,
    EmailMultiAlternatives,
    get_connection,
    send_mail,
)
from django.test import SimpleTestCase, override_settings

from apps.accounts.sendgrid_backend import (
    SEND_TIMEOUT_SECONDS,
    SendGridEmailBackend,
    SendGridError,
    build_payload,
)

TEST_EMAIL = "talhajubair100.bd@gmail.com"
FAKE_KEY = "SG.not-a-real-key-for-tests"
BACKEND = "apps.accounts.sendgrid_backend.SendGridEmailBackend"
SENDER = "GoStyle <noreply@gostyle.uk>"

def sdk_error(status, body=None, reason="error"):
    """The exception the SDK raises for an HTTP error answer."""
    from python_http_client.exceptions import HTTPError

    raw = json.dumps(body).encode() if body is not None else b""
    return HTTPError(status, reason, raw, {})


def fake_response(message_id="msg-123"):
    response = mock.Mock(status_code=202)
    response.headers = {"X-Message-Id": message_id}
    return response


@override_settings(
    SENDGRID_API_KEY=FAKE_KEY,
    DEFAULT_FROM_EMAIL=SENDER,
    EMAIL_BACKEND=BACKEND,
)
class BackendTestCase(SimpleTestCase):
    """Replaces the SDK client class, so no test can build a real one."""

    def setUp(self):
        patcher = mock.patch("apps.accounts.sendgrid_backend.SendGridAPIClient")
        self.client_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.sdk = self.client_class.return_value
        self.sdk.send.return_value = fake_response()

    def sent_payload(self, call=0):
        return self.sdk.send.call_args_list[call].args[0]


class SendingTests(BackendTestCase):
    def test_send_mail_goes_through_the_backend_and_counts_one(self):
        sent = send_mail("Hello", "Plain body", None, [TEST_EMAIL])

        self.assertEqual(sent, 1)
        self.sdk.send.assert_called_once()

    def test_the_payload_is_what_the_message_says(self):
        send_mail("Your verification code", "Plain body", None, [TEST_EMAIL])

        p = self.sent_payload()
        self.assertEqual(p["from"], {"email": "noreply@gostyle.uk", "name": "GoStyle"})
        self.assertEqual(p["subject"], "Your verification code")
        self.assertEqual(p["personalizations"], [{"to": [{"email": TEST_EMAIL}]}])
        self.assertEqual(p["content"], [{"type": "text/plain", "value": "Plain body"}])

    def test_the_client_is_made_with_the_key_and_a_timeout(self):
        send_mail("Hello", "Body", None, [TEST_EMAIL])

        self.client_class.assert_called_once_with(api_key=FAKE_KEY)
        self.assertEqual(self.sdk.client.timeout, SEND_TIMEOUT_SECONDS)

    def test_click_tracking_is_off_so_links_go_out_as_written(self):
        send_mail("Hello", "Body", None, [TEST_EMAIL])

        self.assertEqual(
            self.sent_payload()["tracking_settings"],
            {"click_tracking": {"enable": False, "enable_text": False}},
        )

    def test_text_and_html_alternatives_both_go_text_first(self):
        msg = EmailMultiAlternatives("Hi", "Plain", None, [TEST_EMAIL])
        msg.attach_alternative("<p>Rich</p>", "text/html")
        msg.send()

        self.assertEqual(
            self.sent_payload()["content"],
            [
                {"type": "text/plain", "value": "Plain"},
                {"type": "text/html", "value": "<p>Rich</p>"},
            ],
        )

    def test_html_added_before_the_plain_body_is_still_sent_second(self):
        msg = EmailMultiAlternatives("Hi", "", None, [TEST_EMAIL])
        msg.attach_alternative("<p>Rich</p>", "text/html")
        msg.body = "Plain"
        msg.send()

        self.assertEqual(
            [c["type"] for c in self.sent_payload()["content"]],
            ["text/plain", "text/html"],
        )

    def test_an_html_only_message_drops_the_empty_plain_part(self):
        msg = EmailMultiAlternatives("Hi", "", None, [TEST_EMAIL])
        msg.attach_alternative("<p>Rich</p>", "text/html")
        msg.send()

        self.assertEqual(
            self.sent_payload()["content"], [{"type": "text/html", "value": "<p>Rich</p>"}]
        )

    def test_a_body_with_html_content_subtype_is_sent_as_html(self):
        msg = EmailMessage("Hi", "<b>Bold</b>", None, [TEST_EMAIL])
        msg.content_subtype = "html"
        msg.send()

        self.assertEqual(
            self.sent_payload()["content"], [{"type": "text/html", "value": "<b>Bold</b>"}]
        )

    def test_unicode_survives(self):
        send_mail("Café — GoStyle", "Crème brûlée", None, [TEST_EMAIL])

        p = self.sent_payload()
        self.assertEqual(p["subject"], "Café — GoStyle")
        self.assertEqual(p["content"][0]["value"], "Crème brûlée")

    def test_the_message_id_is_logged_but_not_the_recipient_or_subject(self):
        with self.assertLogs("apps.accounts.sendgrid_backend", level="INFO") as logs:
            send_mail("Secret subject", "Body with 123456", None, [TEST_EMAIL])

        text = "\n".join(logs.output)
        self.assertIn("msg-123", text)
        for private in (TEST_EMAIL, "Secret subject", "123456", FAKE_KEY):
            self.assertNotIn(private, text)

    def test_nothing_to_send_is_zero_and_calls_nothing(self):
        backend = get_connection()

        self.assertEqual(backend.send_messages([]), 0)
        self.client_class.assert_not_called()

    def test_a_message_with_no_recipients_is_not_sent(self):
        self.assertEqual(EmailMessage("Hi", "Body", None, []).send(), 0)
        self.assertEqual(get_connection().send_messages([EmailMessage("Hi", "Body")]), 0)
        self.sdk.send.assert_not_called()

    def test_several_messages_on_one_connection_are_counted_each(self):
        messages = [
            EmailMessage("One", "a", None, [TEST_EMAIL]),
            EmailMessage("Two", "b", None, [TEST_EMAIL]),
        ]

        self.assertEqual(get_connection().send_messages(messages), 2)
        self.assertEqual(self.sdk.send.call_count, 2)
        # One client for the whole batch, not one per message.
        self.client_class.assert_called_once()

    def test_the_connection_is_a_context_manager(self):
        with get_connection() as connection:
            connection.send_messages([EmailMessage("One", "a", None, [TEST_EMAIL])])

        self.assertIsNone(connection._client)


class RecipientTests(BackendTestCase):
    def test_multiple_recipients_share_one_message(self):
        EmailMessage("Hi", "Body", None, [TEST_EMAIL, "b@example.com"]).send()

        self.assertEqual(self.sdk.send.call_count, 1)
        self.assertEqual(
            self.sent_payload()["personalizations"][0]["to"],
            [{"email": TEST_EMAIL}, {"email": "b@example.com"}],
        )

    def test_cc_and_bcc_are_carried(self):
        EmailMessage(
            "Hi", "Body", None, [TEST_EMAIL], cc=["c@example.com"], bcc=["d@example.com"]
        ).send()

        personalization = self.sent_payload()["personalizations"][0]
        self.assertEqual(personalization["cc"], [{"email": "c@example.com"}])
        self.assertEqual(personalization["bcc"], [{"email": "d@example.com"}])

    def test_a_display_name_is_split_from_the_address(self):
        EmailMessage("Hi", "Body", "Sender Name <sender@gostyle.uk>", ["Kevin R <k@example.com>"]).send()

        p = self.sent_payload()
        self.assertEqual(p["from"], {"email": "sender@gostyle.uk", "name": "Sender Name"})
        self.assertEqual(p["personalizations"][0]["to"], [{"email": "k@example.com", "name": "Kevin R"}])

    def test_an_address_listed_twice_is_sent_once_which_sendgrid_requires(self):
        EmailMessage(
            "Hi",
            "Body",
            None,
            [TEST_EMAIL],
            cc=[TEST_EMAIL.upper(), "c@example.com"],
            bcc=["C@example.com"],
        ).send()

        personalization = self.sent_payload()["personalizations"][0]
        self.assertEqual(personalization["to"], [{"email": TEST_EMAIL}])
        self.assertEqual(personalization["cc"], [{"email": "c@example.com"}])
        self.assertNotIn("bcc", personalization)

    def test_one_reply_to_is_a_reply_to(self):
        EmailMessage("Hi", "Body", None, [TEST_EMAIL], reply_to=["help@gostyle.uk"]).send()

        p = self.sent_payload()
        self.assertEqual(p["reply_to"], {"email": "help@gostyle.uk"})
        self.assertNotIn("reply_to_list", p)

    def test_several_reply_tos_are_a_list(self):
        EmailMessage(
            "Hi", "Body", None, [TEST_EMAIL], reply_to=["a@gostyle.uk", "b@gostyle.uk"]
        ).send()

        p = self.sent_payload()
        self.assertEqual(p["reply_to_list"], [{"email": "a@gostyle.uk"}, {"email": "b@gostyle.uk"}])
        self.assertNotIn("reply_to", p)

    def test_a_reply_to_header_overrides_the_attribute_as_in_django(self):
        EmailMessage(
            "Hi",
            "Body",
            None,
            [TEST_EMAIL],
            reply_to=["attr@gostyle.uk"],
            headers={"Reply-To": "Header Name <header@gostyle.uk>"},
        ).send()

        p = self.sent_payload()
        self.assertEqual(p["reply_to"], {"email": "header@gostyle.uk", "name": "Header Name"})
        self.assertNotIn("Reply-To", p.get("headers", {}))

    def test_no_reply_to_means_none_in_the_payload(self):
        EmailMessage("Hi", "Body", None, [TEST_EMAIL]).send()

        self.assertNotIn("reply_to", self.sent_payload())

    def test_a_non_address_is_refused_rather_than_sent_blank(self):
        with self.assertRaises(SendGridError):
            build_payload(EmailMessage("Hi", "Body", None, ["not an address"]))


class HeaderTests(BackendTestCase):
    def test_custom_headers_pass_through(self):
        EmailMessage(
            "Hi", "Body", None, [TEST_EMAIL], headers={"X-Campaign": "otp", "List-Unsubscribe": "<mailto:u@gostyle.uk>"}
        ).send()

        self.assertEqual(
            self.sent_payload()["headers"],
            {"X-Campaign": "otp", "List-Unsubscribe": "<mailto:u@gostyle.uk>"},
        )

    def test_headers_sendgrid_refuses_are_left_out(self):
        EmailMessage(
            "Hi",
            "Body",
            None,
            [TEST_EMAIL],
            headers={"Content-Type": "text/x", "To": "x@example.com", "X-Keep": "1"},
        ).send()

        self.assertEqual(self.sent_payload()["headers"], {"X-Keep": "1"})

    def test_a_line_break_in_the_subject_is_refused(self):
        with self.assertRaises(BadHeaderError):
            send_mail("Hi\nBcc: victim@example.com", "Body", None, [TEST_EMAIL])

        self.sdk.send.assert_not_called()


class AttachmentTests(BackendTestCase):
    def test_binary_and_text_attachments_are_base64_with_their_names_and_types(self):
        msg = EmailMessage("Hi", "Body", None, [TEST_EMAIL])
        msg.attach("receipt.pdf", b"%PDF-1.4 binary \xff", "application/pdf")
        msg.attach("notes.csv", "a,b\n1,2\n", "text/csv")
        msg.send()

        pdf, csv = self.sent_payload()["attachments"]
        self.assertEqual(pdf["filename"], "receipt.pdf")
        self.assertEqual(pdf["type"], "application/pdf")
        self.assertEqual(pdf["disposition"], "attachment")
        self.assertEqual(base64.b64decode(pdf["content"]), b"%PDF-1.4 binary \xff")
        self.assertEqual(csv["type"], "text/csv")
        self.assertEqual(base64.b64decode(csv["content"]), b"a,b\n1,2\n")

    def test_an_inline_mime_part_keeps_its_content_id(self):
        part = email.message.MIMEPart()
        part.set_content(
            b"\x89PNG-bytes",
            maintype="image",
            subtype="png",
            disposition="inline",
            cid="<logo1@gostyle>",
            filename="logo.png",
        )
        msg = EmailMessage("Hi", "Body", None, [TEST_EMAIL])
        msg.attach(part)
        msg.send()

        (att,) = self.sent_payload()["attachments"]
        self.assertEqual(att["disposition"], "inline")
        self.assertEqual(att["content_id"], "logo1@gostyle")
        self.assertEqual(att["type"], "image/png")
        self.assertEqual(base64.b64decode(att["content"]), b"\x89PNG-bytes")

    def test_no_attachments_means_no_key(self):
        EmailMessage("Hi", "Body", None, [TEST_EMAIL]).send()

        self.assertNotIn("attachments", self.sent_payload())


class MissingKeyTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch("apps.accounts.sendgrid_backend.SendGridAPIClient")
        self.client_class = patcher.start()
        self.addCleanup(patcher.stop)

    @override_settings(SENDGRID_API_KEY="", EMAIL_BACKEND=BACKEND)
    def test_an_unset_key_raises_and_sends_nothing(self):
        with self.assertRaisesRegex(ImproperlyConfigured, "SENDGRID_API_KEY"):
            send_mail("Hi", "Body", None, [TEST_EMAIL])

        self.client_class.assert_not_called()

    @override_settings(SENDGRID_API_KEY="   ", EMAIL_BACKEND=BACKEND)
    def test_a_blank_key_is_the_same_as_none(self):
        with self.assertRaises(ImproperlyConfigured):
            send_mail("Hi", "Body", None, [TEST_EMAIL])

    @override_settings(SENDGRID_API_KEY="", EMAIL_BACKEND=BACKEND)
    def test_fail_silently_returns_zero_but_still_logs_the_cause(self):
        with self.assertLogs("apps.accounts.sendgrid_backend", level="ERROR") as logs:
            sent = send_mail("Hi", "Body", None, [TEST_EMAIL], fail_silently=True)

        self.assertEqual(sent, 0)
        self.assertIn("SENDGRID_API_KEY", "\n".join(logs.output))
        self.client_class.assert_not_called()

    @override_settings(SENDGRID_API_KEY="", EMAIL_BACKEND=BACKEND)
    def test_an_explicit_key_wins_over_settings(self):
        backend = SendGridEmailBackend(api_key=FAKE_KEY)
        backend.send_messages([EmailMessage("Hi", "Body", "a@gostyle.uk", [TEST_EMAIL])])

        self.client_class.assert_called_once_with(api_key=FAKE_KEY)

    @override_settings(SENDGRID_API_KEY="", EMAIL_BACKEND=BACKEND)
    def test_there_is_no_smtp_to_fall_back_to(self):
        with mock.patch("smtplib.SMTP") as smtp, mock.patch("smtplib.SMTP_SSL") as smtp_ssl:
            with self.assertRaises(ImproperlyConfigured):
                send_mail("Hi", "Body", None, [TEST_EMAIL])

        smtp.assert_not_called()
        smtp_ssl.assert_not_called()


class ApiErrorTests(BackendTestCase):
    def test_a_refused_sender_raises_with_sendgrids_own_reason(self):
        self.sdk.send.side_effect = sdk_error(
            403,
            {
                "errors": [
                    {
                        "message": "The from address does not match a verified Sender Identity.",
                        "field": "from",
                    }
                ]
            },
            "Forbidden",
        )

        with self.assertRaises(SendGridError) as ctx:
            send_mail("Hi", "Body", None, [TEST_EMAIL])

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertIn("verified Sender Identity", str(ctx.exception))
        self.assertIn("(field: from)", str(ctx.exception))

    def test_a_bad_key_is_a_401(self):
        self.sdk.send.side_effect = sdk_error(
            401, {"errors": [{"message": "The provided authorization grant is invalid", "field": None}]}
        )

        with self.assertRaises(SendGridError) as ctx:
            send_mail("Hi", "Body", None, [TEST_EMAIL])

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("authorization grant is invalid", str(ctx.exception))

    def test_a_server_error_with_no_body_falls_back_to_the_reason(self):
        self.sdk.send.side_effect = sdk_error(503, None, "Service Unavailable")

        with self.assertRaises(SendGridError) as ctx:
            send_mail("Hi", "Body", None, [TEST_EMAIL])

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("Service Unavailable", str(ctx.exception))

    def test_a_body_that_is_not_json_does_not_hide_the_failure(self):
        from python_http_client.exceptions import HTTPError

        self.sdk.send.side_effect = HTTPError(502, "Bad Gateway", b"<html>oops</html>", {})

        with self.assertRaisesRegex(SendGridError, "HTTP 502: Bad Gateway"):
            send_mail("Hi", "Body", None, [TEST_EMAIL])

    def test_a_connection_failure_raises_with_no_status(self):
        self.sdk.send.side_effect = urllib.error.URLError("Name or service not known")

        with self.assertLogs("apps.accounts.sendgrid_backend", level="ERROR"):
            with self.assertRaises(SendGridError) as ctx:
                send_mail("Hi", "Body", None, [TEST_EMAIL])

        self.assertIsNone(ctx.exception.status_code)
        self.assertIn("unreachable", str(ctx.exception))

    def test_a_timeout_raises(self):
        self.sdk.send.side_effect = TimeoutError("timed out")

        with self.assertLogs("apps.accounts.sendgrid_backend", level="ERROR"):
            with self.assertRaisesRegex(SendGridError, "unreachable: TimeoutError"):
                send_mail("Hi", "Body", None, [TEST_EMAIL])

    def test_fail_silently_swallows_the_error_and_counts_nothing_sent(self):
        self.sdk.send.side_effect = sdk_error(403, {"errors": [{"message": "no"}]})

        with self.assertLogs("apps.accounts.sendgrid_backend", level="ERROR"):
            sent = send_mail("Hi", "Body", None, [TEST_EMAIL], fail_silently=True)

        self.assertEqual(sent, 0)

    def test_the_error_log_has_the_reason_but_no_secret_or_customer_data(self):
        self.sdk.send.side_effect = sdk_error(
            400, {"errors": [{"message": "Does not contain a valid address.", "field": "to"}]}
        )

        with self.assertLogs("apps.accounts.sendgrid_backend", level="ERROR") as logs:
            with self.assertRaises(SendGridError):
                send_mail("Secret subject", "Code 123456", None, [TEST_EMAIL])

        text = "\n".join(logs.output)
        self.assertIn("Does not contain a valid address.", text)
        for private in (TEST_EMAIL, "Secret subject", "123456", FAKE_KEY):
            self.assertNotIn(private, text)

    def test_the_raised_error_never_carries_the_key_or_the_recipient(self):
        self.sdk.send.side_effect = sdk_error(500, {"errors": [{"message": "boom"}]})

        with self.assertRaises(SendGridError) as ctx:
            send_mail("Hi", "Body", None, [TEST_EMAIL])

        self.assertNotIn(FAKE_KEY, str(ctx.exception))
        self.assertNotIn(TEST_EMAIL, str(ctx.exception))
        # Not chained either: nothing of the original is shown with it.
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_a_failure_on_one_message_stops_a_strict_batch(self):
        self.sdk.send.side_effect = [fake_response(), sdk_error(500, None), fake_response()]
        messages = [EmailMessage(s, "b", None, [TEST_EMAIL]) for s in ("1", "2", "3")]

        with self.assertLogs("apps.accounts.sendgrid_backend", level="ERROR"):
            with self.assertRaises(SendGridError):
                get_connection().send_messages(messages)

        self.assertEqual(self.sdk.send.call_count, 2)

    def test_a_failure_on_one_message_in_a_silent_batch_counts_the_rest(self):
        self.sdk.send.side_effect = [fake_response(), sdk_error(500, None), fake_response()]
        messages = [EmailMessage(s, "b", None, [TEST_EMAIL]) for s in ("1", "2", "3")]

        with self.assertLogs("apps.accounts.sendgrid_backend", level="ERROR"):
            self.assertEqual(get_connection(fail_silently=True).send_messages(messages), 2)


class RealSdkTests(SimpleTestCase):
    """The real SDK and the real HTTP client, up to the point of the socket.

    This is what proves the payload is something the SDK accepts and that the
    key, URL and timeout are what SendGrid will be given.
    """

    class FakeOpener:
        def __init__(self, outcome):
            self.outcome = outcome
            self.requests = []
            self.timeouts = []

        def open(self, request, timeout=None):
            self.requests.append(request)
            self.timeouts.append(timeout)
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    class FakeHttpResponse:
        def __init__(self, status=202, headers=None):
            self.status = status
            self.headers = email.message.Message()
            for k, v in (headers or {}).items():
                self.headers[k] = v

        def getcode(self):
            return self.status

        def read(self):
            return b""

        def info(self):
            return self.headers

    def run_send(self, outcome, **kwargs):
        opener = self.FakeOpener(outcome)
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with override_settings(SENDGRID_API_KEY=FAKE_KEY, DEFAULT_FROM_EMAIL=SENDER):
                backend = SendGridEmailBackend(**kwargs)
                msg = EmailMultiAlternatives("Verify your email address", "Plain 654321", None, [TEST_EMAIL])
                msg.attach_alternative("<p>Rich 654321</p>", "text/html")
                try:
                    return opener, backend.send_messages([msg])
                except Exception as exc:
                    return opener, exc

    def test_the_request_is_a_signed_post_to_the_mail_send_endpoint(self):
        opener, sent = self.run_send(self.FakeHttpResponse(headers={"X-Message-Id": "abc"}))

        self.assertEqual(sent, 1)
        (request,) = opener.requests
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://api.sendgrid.com/v3/mail/send")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {FAKE_KEY}")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(opener.timeouts, [SEND_TIMEOUT_SECONDS])

        body = json.loads(request.data)
        self.assertEqual(body["from"], {"email": "noreply@gostyle.uk", "name": "GoStyle"})
        self.assertEqual(body["personalizations"], [{"to": [{"email": TEST_EMAIL}]}])
        self.assertEqual(body["subject"], "Verify your email address")
        self.assertEqual([c["type"] for c in body["content"]], ["text/plain", "text/html"])

    def test_a_403_from_sendgrid_becomes_a_send_grid_error(self):
        denied = urllib.error.HTTPError(
            "https://api.sendgrid.com/v3/mail/send",
            403,
            "Forbidden",
            email.message.Message(),
            io.BytesIO(
                json.dumps(
                    {"errors": [{"message": "The from address does not match a verified Sender Identity.", "field": "from"}]}
                ).encode()
            ),
        )

        _, outcome = self.run_send(denied)

        self.assertIsInstance(outcome, SendGridError)
        self.assertEqual(outcome.status_code, 403)
        self.assertIn("verified Sender Identity", str(outcome))
        self.assertNotIn(FAKE_KEY, str(outcome))

    def test_an_unreachable_host_becomes_a_send_grid_error(self):
        with self.assertLogs("apps.accounts.sendgrid_backend", level="ERROR"):
            _, outcome = self.run_send(urllib.error.URLError("[Errno -2] Name or service not known"))

        self.assertIsInstance(outcome, SendGridError)
        self.assertIsNone(outcome.status_code)


class WiringTests(SimpleTestCase):
    def test_the_dotted_path_loads_the_backend(self):
        self.assertIsInstance(get_connection(BACKEND), SendGridEmailBackend)

    @override_settings(EMAIL_BACKEND=BACKEND, SENDGRID_API_KEY=FAKE_KEY)
    def test_the_default_connection_is_sendgrid_when_so_configured(self):
        self.assertIsInstance(get_connection(), SendGridEmailBackend)

    def test_the_settings_module_defaults_to_sendgrid_with_an_aligned_sender(self):
        from config.settings import base

        self.assertEqual(
            base.DEFAULT_FROM_EMAIL, f"{base.SENDGRID_FROM_NAME} <{base.SENDGRID_FROM_EMAIL}>"
        )
        self.assertFalse(hasattr(base, "EMAIL_HOST"), "SMTP settings should be gone")

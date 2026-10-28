"""Django email backend over the SendGrid Mail Send HTTPS API.

`send_mail`, `EmailMessage` and `EmailMultiAlternatives` keep working unchanged:
this is a drop-in `EMAIL_BACKEND` (config/settings/base.py). There is no SMTP
path and nothing falls back to one. A send SendGrid refuses, or cannot be
reached for, raises `SendGridError` -- or, with `fail_silently=True`, is logged
and counted as not sent, which is Django's own contract.

A returned count means SendGrid ACCEPTED the message (HTTP 202) for processing,
not that it reached an inbox. A bounce or a drop only shows in SendGrid's
Activity feed, under the message id this module logs.

Nothing the customer wrote is logged: not the address, the subject or the body
(an OTP email's body IS the code). An error carries SendGrid's own reason and
the HTTP status, built from the response body only, never from the request,
whose headers hold the API key.
"""

import base64
import email.message
import json
import logging
from email.utils import getaddresses, parseaddr
from http.client import HTTPException

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import BadHeaderError
from django.core.mail.backends.base import BaseEmailBackend
from python_http_client.exceptions import HTTPError
from sendgrid import SendGridAPIClient

logger = logging.getLogger(__name__)

# Upper bound on one Mail Send call. A send runs inside a request (an OTP is
# sent from a transaction.on_commit hook), so a hung connection must not hang
# the request with it.
SEND_TIMEOUT_SECONDS = 10

# Headers SendGrid builds itself or refuses in the `headers` object; sending
# one makes the whole request a 400. The address headers are carried by the
# payload's own fields instead.
_RESERVED_HEADERS = frozenset(
    {
        "from",
        "to",
        "cc",
        "bcc",
        "reply-to",
        "subject",
        "content-type",
        "content-transfer-encoding",
        "mime-version",
        "received",
        "dkim-signature",
        "x-sg-id",
        "x-sg-eid",
    }
)


class SendGridError(Exception):
    """SendGrid refused the message, or could not be reached.

    `str()` is safe to log: it holds the HTTP status and SendGrid's reason,
    never an address, a subject, a body or the API key. `status_code` is None
    when SendGrid never answered (DNS, a reset connection, the timeout).
    """

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _response_reasons(body):
    """SendGrid's own words for what was wrong, e.g. "The from address does
    not match a verified Sender Identity", from the response BODY."""
    try:
        errors = json.loads(body.decode("utf-8", errors="replace")).get("errors")
    except (AttributeError, ValueError):
        return []
    if not isinstance(errors, list):
        return []
    reasons = []
    for err in errors:
        if not isinstance(err, dict) or not isinstance(err.get("message"), str):
            continue
        field = err.get("field")
        reasons.append(
            f"{err['message']} (field: {field})" if isinstance(field, str) and field
            else err["message"]
        )
    return reasons


def _error_from_response(exc):
    """A SendGridError for an HTTP error answer. Built from the status and the
    response body only; the original exception is never chained."""
    reasons = "; ".join(_response_reasons(exc.body)) or str(exc.reason or "no reason given")
    return SendGridError(f"SendGrid HTTP {exc.status_code}: {reasons[:300]}", exc.status_code)


def _pair(name, address):
    # parseaddr is lenient (a bare word comes back as an address); this only
    # catches what could never be one. SendGrid validates the rest and says so.
    if "@" not in address:
        raise SendGridError("A message has an address that is not an email address.")
    return {"email": address, "name": name} if name else {"email": address}


def _address(value):
    return _pair(*parseaddr(str(value)))


def _recipients(message):
    """to / cc / bcc as SendGrid wants them. SendGrid refuses an address that
    appears twice across the three, which Django does not, so the first
    mention wins (to, then cc, then bcc), compared case-insensitively."""
    seen = set()
    lists = {}
    for key, values in (("to", message.to), ("cc", message.cc), ("bcc", message.bcc)):
        kept = []
        for value in values:
            if not value:
                continue
            address = _address(value)
            fold = address["email"].lower()
            if fold not in seen:
                seen.add(fold)
                kept.append(address)
        if kept:
            lists[key] = kept
    return lists


def _reply_to(message):
    # Django lets a Reply-To header override the attribute, so does this.
    for name, value in message.extra_headers.items():
        if name.lower() == "reply-to":
            return [_pair(n, a) for n, a in getaddresses([str(value)])]
    return [_address(value) for value in message.reply_to if value]


def _text(content, encoding):
    return content.decode(encoding) if isinstance(content, bytes) else str(content)


def _contents(message, encoding):
    """The message's parts in the order SendGrid demands: text/plain, then
    text/html, then anything else. Empty parts are dropped; SendGrid refuses
    them."""
    parts = []
    if message.body and message.body.strip():
        parts.append((f"text/{message.content_subtype}", message.body))
    for content, mimetype in getattr(message, "alternatives", []):
        if not mimetype.startswith("text/"):
            raise SendGridError(f"An alternative of type {mimetype} cannot be sent through SendGrid.")
        text = _text(content, encoding)
        if text.strip():
            parts.append((mimetype, text))

    def rank(part):
        return {"text/plain": 0, "text/html": 1}.get(part[0], 2)

    return [{"type": mimetype, "value": value} for mimetype, value in sorted(parts, key=rank)]


def _attachment(attachment, encoding):
    disposition, content_id = "attachment", None
    if isinstance(attachment, email.message.Message):
        # A MIMEPart (or the deprecated MIMEBase): read it back out.
        filename = attachment.get_filename()
        mimetype = attachment.get_content_type()
        content = attachment.get_payload(decode=True)
        if content is None:
            raise SendGridError("A nested multipart attachment cannot be sent through SendGrid.")
        if attachment.get_content_disposition() == "inline":
            disposition = "inline"
            content_id = (attachment["Content-ID"] or "").strip().strip("<>") or None
    else:
        filename, content, mimetype = attachment
        if isinstance(content, str):
            content = content.encode(encoding)
        elif not isinstance(content, (bytes, bytearray, memoryview)):
            raise SendGridError(f"An attachment of type {mimetype} has content SendGrid cannot take.")

    payload = {
        "content": base64.b64encode(bytes(content)).decode("ascii"),
        "type": mimetype,
        # SendGrid requires a filename; Django does not.
        "filename": filename or "attachment",
        "disposition": disposition,
    }
    if content_id:
        payload["content_id"] = content_id
    return payload


def build_payload(message):
    """The Mail Send request body for one Django EmailMessage.

    Raises SendGridError for something SendGrid cannot carry and BadHeaderError
    for a line break in the subject, as Django's own backends do.
    """
    subject = str(message.subject)
    if "\n" in subject or "\r" in subject:
        raise BadHeaderError("Header values can't contain newlines in the subject.")

    encoding = message.encoding or settings.DEFAULT_CHARSET

    payload = {
        "from": _address(message.extra_headers.get("From", message.from_email)),
        "subject": subject,
        "personalizations": [_recipients(message)],
        "content": _contents(message, encoding),
        # Links go out exactly as written. With click tracking on, SendGrid
        # rewrites every URL through its own redirect domain.
        "tracking_settings": {"click_tracking": {"enable": False, "enable_text": False}},
    }

    reply_to = _reply_to(message)
    if len(reply_to) == 1:
        payload["reply_to"] = reply_to[0]
    elif reply_to:
        payload["reply_to_list"] = reply_to

    headers = {
        str(name): str(value)
        for name, value in message.extra_headers.items()
        if str(name).lower() not in _RESERVED_HEADERS
    }
    if headers:
        payload["headers"] = headers

    if message.attachments:
        payload["attachments"] = [_attachment(a, encoding) for a in message.attachments]

    return payload


class SendGridEmailBackend(BaseEmailBackend):
    """Sends Django email messages through the SendGrid Mail Send API."""

    def __init__(self, fail_silently=False, api_key=None, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        key = api_key if api_key is not None else getattr(settings, "SENDGRID_API_KEY", "")
        self.api_key = (key or "").strip()
        self._client = None

    def open(self):
        """Make the API client. True if this call made it, False if it already
        existed or could not be made silently -- the SMTP backend's contract."""
        if self._client is not None:
            return False
        if not self.api_key:
            message = (
                "SENDGRID_API_KEY is not set, so email cannot be sent. Set it in "
                "the environment (see .env.example)."
            )
            if self.fail_silently:
                logger.error(message)
                return False
            raise ImproperlyConfigured(message)
        self._client = self._make_client()
        return True

    def close(self):
        self._client = None

    def _make_client(self):
        """Overridden in tests; nothing else should need to."""
        client = SendGridAPIClient(api_key=self.api_key)
        client.client.timeout = SEND_TIMEOUT_SECONDS
        return client

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        new_connection = self.open()
        if self._client is None:
            return 0  # no key, and fail_silently said not to raise

        sent = 0
        try:
            for message in email_messages:
                if self._send(message):
                    sent += 1
        finally:
            if new_connection:
                self.close()
        return sent

    def _send(self, message):
        if not message.recipients():
            return False
        try:
            response = self._post(message)
        except (SendGridError, BadHeaderError) as exc:
            logger.error("Email not sent: %s", exc)
            if not self.fail_silently:
                raise
            return False

        logger.info(
            "SendGrid accepted an email  status=%s  message_id=%s",
            response.status_code,
            response.headers.get("X-Message-Id", "-"),
        )
        return True

    def _post(self, message):
        payload = build_payload(message)
        try:
            return self._client.send(payload)
        except HTTPError as exc:
            raise _error_from_response(exc) from None
        except (OSError, HTTPException) as exc:
            # DNS, a refused or reset connection, the timeout. The class name
            # and message only; the request (and its Authorization header)
            # never enters the text.
            raise SendGridError(f"SendGrid unreachable: {type(exc).__name__}: {exc}"[:300]) from None

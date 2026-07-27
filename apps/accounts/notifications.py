"""OTP delivery.

A sender implements a single method:

    send(destination, code, destination_type) -> None

The router picks a concrete sender per call: ConsoleSender when OTP_SENDER is
"console" (local dev), otherwise WhatsApp for the phone destination type and
email for the email destination type. Phone OTP goes over WhatsApp only; there
is no SMS fallback.
"""

import json
import urllib.error
import urllib.request

from django.conf import settings
from django.core.mail import send_mail

from .identifiers import EMAIL, PHONE

WHATSAPP_API_VERSION = "v23.0"


class ConsoleSender:
    """Logs the code to stdout. For local development only."""

    def send(self, destination, code, destination_type):
        print(f"\n>>> OTP for {destination} [{destination_type}]: {code}\n")


class EmailSender:
    """Delivers the code over Django's configured email backend."""

    def send(self, destination, code, destination_type):
        send_mail(
            subject="Your Go Style verification code",
            message=(
                f"Your verification code is {code}. "
                "It expires in 5 minutes. If you did not request this, ignore this email."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[destination],
            fail_silently=False,
        )


class WhatsAppSender:
    """Delivers the code over the WhatsApp Cloud API using an authentication
    template. The template renders the code in its body and in a copy-code
    button, so both components carry the same value."""

    def send(self, destination, code, destination_type):
        phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
        access_token = settings.WHATSAPP_ACCESS_TOKEN
        template_name = settings.WHATSAPP_TEMPLATE_NAME
        if not (phone_number_id and access_token and template_name):
            raise RuntimeError("WhatsApp sender is not configured.")

        url = (
            f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/"
            f"{phone_number_id}/messages"
        )
        payload = {
            "messaging_product": "whatsapp",
            # WhatsApp wants the E.164 number without the leading plus.
            "to": destination.lstrip("+"),
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": settings.WHATSAPP_TEMPLATE_LANGUAGE},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": code}],
                    },
                    {
                        "type": "button",
                        "sub_type": "url",
                        "index": "0",
                        "parameters": [{"type": "text", "text": code}],
                    },
                ],
            },
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(f"WhatsApp send failed ({exc.code}): {body}") from exc


def get_sender(destination_type):
    """Return the sender to use for `destination_type` given the current config."""
    if getattr(settings, "OTP_SENDER", "console") == "console":
        return ConsoleSender()
    if destination_type == EMAIL:
        return EmailSender()
    if destination_type == PHONE:
        return WhatsAppSender()
    raise ValueError(f"No sender for destination type {destination_type!r}")

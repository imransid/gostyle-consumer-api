"""OTP delivery.

A sender implements a single method:

    send(destination, code, destination_type, subject=None, minutes=None) -> None

subject and minutes are for an email's subject line and its "expires in"
(2-step verification passes its own). Left out, an email says what it always
has. The console and WhatsApp take them and ignore them: WhatsApp's text is
its approved template's.

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

    def send(self, destination, code, destination_type, subject=None, minutes=None):
        print(f"\n>>> OTP for {destination} [{destination_type}]: {code}\n")


class EmailSender:
    """Delivers the code over Django's configured email backend.

    Sends multipart: plain text for clients that refuse HTML, and the branded
    template for everything else.
    """

    def send(self, destination, code, destination_type, subject=None, minutes=None):
        from django.core.mail import EmailMultiAlternatives
        from .emails import render_otp_email
        # Imported here: services imports this module.
        from .services import OTP_TTL_SECONDS

        # By default how long request_otp's code really lasts. It said 5
        # while codes lasted 10.
        if minutes is None:
            minutes = OTP_TTL_SECONDS // 60

        text = (
            f"Your verification code is {code}. "
            f"It expires in {minutes} minutes. "
            "If you did not request this, ignore this email."
        )

        msg = EmailMultiAlternatives(
            subject=subject or "Your Go Style verification code",
            body=text,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[destination],
        )
        msg.attach_alternative(render_otp_email(code, minutes), "text/html")
        msg.send(fail_silently=False)

class WhatsAppSender:
    """Delivers the code over the WhatsApp Cloud API using an authentication
    template. The template renders the code in its body and in a copy-code
    button, so both components carry the same value. Its text is fixed when
    Meta approves it, so subject and minutes have nowhere to go."""

    def send(self, destination, code, destination_type, subject=None, minutes=None):
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


def send_contact_change_notice(destination, destination_type, changed_type):
    """Tell a contact that is being replaced that the change happened.

    Not a sender: every sender above delivers a six digit code and nothing
    else. This is a plain message to an address that is on its way out.

    Email only for now. WhatsApp refuses free text outside a 24 hour window,
    so a phone notice needs its own approved template first.
    """
    if getattr(settings, "OTP_SENDER", "console") == "console":
        print(f"\n>>> NOTICE to {destination}: your {changed_type} was changed\n")
        return

    if destination_type != EMAIL:
        return

    send_mail(
        subject="Your Go Style account details changed",
        message=(
            f"The {changed_type} on your Go Style account was just changed. "
            "If this was not you, contact support straight away."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[destination],
        fail_silently=True,
    )


def send_security_notice(destination, subject, message):
    """Tell an account's email that a security setting changed: 2-step
    verification turned on or off, the authenticator app removed
    (apps/accounts/two_factor.notify). An unexpected one is how a customer
    notices someone else in their account.

    Email only, for the reason the notice above is. A failure is silent: the
    change it reports has already been made.
    """
    if getattr(settings, "OTP_SENDER", "console") == "console":
        print(f"\n>>> NOTICE to {destination}: {message}\n")
        return

    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[destination],
        fail_silently=True,
    )

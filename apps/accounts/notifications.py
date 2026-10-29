"""OTP delivery.

A sender implements a single method:

    send(destination, code, destination_type, subject=None, minutes=None,
         purpose=None, first_name=None) -> None

subject and minutes are for an email's subject line and its "expires in"
(2-step verification passes its own). purpose (register, password_reset, ...)
picks the email's template, and first_name is its greeting. Left out, an email
is the generic "Confirm it's you" with the code's real lifetime. The console
and WhatsApp take them all and ignore them: WhatsApp's text is its approved
template's.

The router picks a concrete sender per call: ConsoleSender when OTP_SENDER is
"console" (local dev), otherwise WhatsApp for the phone destination type and
email for the email destination type. Phone OTP goes over WhatsApp only; there
is no SMS fallback. Email goes through Django's configured EMAIL_BACKEND, which
is the SendGrid backend (sendgrid_backend.py): no SMTP.
"""

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from . import push_api
from .identifiers import EMAIL, PHONE

logger = logging.getLogger(__name__)

WHATSAPP_API_VERSION = "v23.0"


class ConsoleSender:
    """Logs the code to stdout. For local development only."""

    def send(self, destination, code, destination_type, subject=None, minutes=None,
             purpose=None, first_name=None):
        print(f"\n>>> OTP for {destination} [{destination_type}]: {code}\n")


class EmailSender:
    """Delivers the code over Django's configured email backend (SendGrid).

    Sends multipart: plain text for clients that refuse HTML, and the branded
    template for everything else. Which template depends on the purpose, as in
    the platform: registration and password reset have their own words, every
    other purpose is the generic "Confirm it's you".

    A refused send raises (fail_silently=False) and is not retried here; the
    caller's convention for an OTP that fails to go out is unchanged.
    """

    def send(self, destination, code, destination_type, subject=None, minutes=None,
             purpose=None, first_name=None):
        from .emails import build_otp_email, minutes_from_seconds
        # Imported here: services imports this module.
        from .services import OTP_TTL_SECONDS

        # By default how long request_otp's code really lasts. It said 5
        # while codes lasted 10.
        if minutes is None:
            minutes = minutes_from_seconds(OTP_TTL_SECONDS)

        mail = build_otp_email(purpose, code, minutes, first_name, subject=subject)
        msg = EmailMultiAlternatives(
            subject=mail.subject,
            body=mail.text,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[destination],
        )
        msg.attach_alternative(mail.html, "text/html")
        msg.send(fail_silently=False)


class WhatsAppSender:
    """Delivers the code over the WhatsApp Cloud API using an authentication
    template. The template renders the code in its body and in a copy-code
    button, so both components carry the same value. Its text is fixed when
    Meta approves it, so subject and minutes have nowhere to go."""

    def send(self, destination, code, destination_type, subject=None, minutes=None,
             purpose=None, first_name=None):
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


def send_notice(destination, subject, headline, message, preheader=None):
    """Email `destination` that something about its account just changed:
    the branded notice (emails.build_notice_email) with `message` as its
    plain text. Every notice below comes through here, and the welcome.

    Not a sender: every sender above delivers a six digit code and nothing
    else. A failure is logged and swallowed (fail_silently): the change a
    notice reports has already been made, and an email that did not go out is
    no reason to undo it or fail the request that made it.
    """
    if getattr(settings, "OTP_SENDER", "console") == "console":
        print(f"\n>>> NOTICE to {destination}: {message}\n")
        return

    from .emails import build_notice_email

    mail = build_notice_email(subject, headline, message, preheader=preheader)
    msg = EmailMultiAlternatives(
        subject=mail.subject,
        body=mail.text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[destination],
    )
    msg.attach_alternative(mail.html, "text/html")
    msg.send(fail_silently=True)


def send_contact_change_notice(destination, destination_type, changed_type):
    """Tell a contact that is being replaced that the change happened.

    This is a plain message to an address that is on its way out.

    Email only for now. WhatsApp refuses free text outside a 24 hour window,
    so a phone notice needs its own approved template first.
    """
    if getattr(settings, "OTP_SENDER", "console") == "console":
        print(f"\n>>> NOTICE to {destination}: your {changed_type} was changed\n")
        return

    if destination_type != EMAIL:
        return

    send_notice(
        destination,
        subject="Your Go Style account details changed",
        headline="Your account details changed",
        message=(
            f"The {changed_type} on your Go Style account was just changed. "
            "If this was not you, contact support straight away."
        ),
    )


def send_security_notice(destination, subject, message, headline=None):
    """Tell an account's email that a security setting changed: 2-step
    verification turned on or off, the authenticator app removed
    (apps/accounts/two_factor.notify), the password changed or reset, a
    deletion scheduled or cancelled, a sign-in from a new device. An
    unexpected one is how a customer notices someone else in their account.

    Email only, for the reason the notice above is. A failure is silent: the
    change it reports has already been made.
    """
    send_notice(destination, subject, headline or subject, message)


def verified_email(account):
    """The account's email if it has been verified, else None. A notice goes
    only to an address the customer has proved is theirs: an unverified one
    may be a typo, and somebody else's inbox."""
    if account.email and account.email_verified_at:
        return account.email
    return None


def send_password_changed_notice(account):
    """Tell the account's verified email that its password was just changed
    (services.change_password) or reset (services.reset_password). A reset
    the owner did not ask for is exactly the moment they need to hear it."""
    destination = verified_email(account)
    if destination is None:
        return
    send_security_notice(
        destination,
        subject="Go Style: your password was changed",
        headline="Your password was changed",
        message=(
            "The password for your Go Style account was just changed.\n\n"
            "If this was not you, reset your password from the app's sign-in "
            "screen and contact support straight away."
        ),
    )


def send_deletion_scheduled_notice(account):
    """Tell the account's verified email that it is now scheduled for
    deletion (ConsumerAccount.request_deletion), and until when it can be
    cancelled. Somebody else with the password can do this too."""
    destination = verified_email(account)
    if destination is None or account.purge_at is None:
        return
    purge_at = timezone.localtime(account.purge_at)
    when = f"{purge_at.day} {purge_at:%B %Y}"
    send_security_notice(
        destination,
        subject="Go Style: your account is scheduled for deletion",
        headline="Your account is scheduled for deletion",
        message=(
            f"Your Go Style account will be deleted on {when}. Until then you "
            "can sign in to the app and cancel the deletion from your account "
            "settings.\n\n"
            "If you did not ask for this, sign in, cancel the deletion, change "
            "your password and contact support straight away."
        ),
    )


def _first_name(account):
    parts = (account.full_name or "").split()
    return parts[0] if parts else None


def send_welcome_email(account):
    """Welcome the account once it is verified, the first time, by its email
    (services.verify_otp_for_user). An account verified by its phone has no
    verified email yet, so it gets none."""
    destination = verified_email(account)
    if destination is None:
        return
    name = _first_name(account)
    send_notice(
        destination,
        subject="Welcome to GoStyle",
        headline=f"Welcome, {name}" if name else "Welcome to GoStyle",
        message=(
            "Your GoStyle account is verified and ready to use.\n\n"
            "Find salons and stylists near you, book a visit in a few taps, and "
            "keep the places you love in your favourites.\n\n"
            "If you did not create this account, contact support."
        ),
        preheader="Your GoStyle account is verified and ready to use.",
    )


def send_new_sign_in_notice(account, session):
    """Tell the account's verified email that it was just signed in to from a
    device it has not used before (sessions.start), and from where. The
    sign-in has happened; this is how the owner hears of one that was not
    them."""
    destination = verified_email(account)
    if destination is None:
        return
    device = session.device_name or "an unknown device"
    place = f" in {session.location}" if session.location else ""
    send_security_notice(
        destination,
        subject="Go Style: new sign-in to your account",
        headline="New sign-in to your account",
        message=(
            f"Your Go Style account was just signed in to on {device}{place}.\n\n"
            "If this was you, there is nothing to do. If not, change your "
            "password, sign that device out from Where you're signed in in the "
            "app's settings, and contact support straight away."
        ),
    )


def send_new_sign_in_push(account, session):
    """The same news as send_new_sign_in_notice, as a push to the account's
    phones (push service, POST /notifications/user). The phone that just
    signed in has not registered for push yet as a rule, so it is the
    others that hear of it.

    Not when the account has push off (/notifications/preferences), nor
    while PUSH_API_KEY is unset. A push that fails is logged and swallowed:
    the sign-in has happened, and must not fail for it.
    """
    if not settings.PUSH_API_KEY:
        return
    # Imported here: models imports this module.
    from .models import NotificationPreference

    prefs = NotificationPreference.objects.filter(account=account).first()
    if prefs is not None and not prefs.push:
        return
    device = session.device_name or "an unknown device"
    place = f" in {session.location}" if session.location else ""
    try:
        status_code, body = push_api.send_to_user(
            user_id=account.pk,
            event_id=f"new-sign-in:{session.pk}",
            title="New sign-in to your account",
            body=(
                f"Your Go Style account was just signed in to on {device}{place}. "
                "If this was not you, change your password."
            ),
            data={"type": "new_sign_in", "sessionId": str(session.pk)},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("new sign-in push not sent: %s", e)
        return
    if status_code >= 400:
        logger.warning("push service refused the new sign-in push (%s): %s", status_code, body)


def send_phone_changed_notice(account):
    """Tell the account's verified email that its phone number was just
    changed (services.confirm_contact_change). The old number cannot be told
    (send_contact_change_notice: WhatsApp needs a template), so this is the
    only word the owner gets of it."""
    destination = verified_email(account)
    if destination is None:
        return
    send_contact_change_notice(destination, EMAIL, "phone number")


def send_deletion_cancelled_notice(account):
    """Tell the account's verified email that its scheduled deletion was
    cancelled (CancelDeletionView), so the account stays."""
    destination = verified_email(account)
    if destination is None:
        return
    send_security_notice(
        destination,
        subject="Go Style: your account will not be deleted",
        headline="Your account will not be deleted",
        message=(
            "The deletion of your Go Style account was cancelled. Your account "
            "stays as it is, and you can keep using the app as before.\n\n"
            "If you did not cancel it, change your password and contact support "
            "straight away."
        ),
    )


def send_account_deleted_notice(destination):
    """Tell the address an account had, once it is purged
    (ConsumerAccount.purge), that the deletion is done. Takes the address,
    not the account: by the time this runs the account no longer has one,
    and the address is not kept anywhere after this email."""
    if not destination:
        return
    send_security_notice(
        destination,
        subject="Go Style: your account has been deleted",
        headline="Your account has been deleted",
        message=(
            "Your Go Style account and the personal details on it have been "
            "deleted, as you asked. This cannot be undone. You are welcome to "
            "create a new account at any time.\n\n"
            "If you did not ask for this, contact support."
        ),
    )

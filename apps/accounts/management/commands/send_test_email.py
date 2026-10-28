from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email

from apps.accounts.emails import build_notice_email
from apps.accounts.models import Purpose
from apps.accounts.notifications import EmailSender
from apps.accounts.sendgrid_backend import SendGridError
from apps.accounts.two_factor import SECURITY_SUBJECT, SIGN_IN_SUBJECT

# Obviously not a real code, and never one that was issued.
PLACEHOLDER_CODE = "000000"

# The code emails: the OTP purposes, and 2-step verification's two subjects
# over the generic words.
CODE_TEMPLATES = {
    **{p.value: {"purpose": p.value} for p in Purpose},
    "two_factor_sign_in": {"subject": SIGN_IN_SUBJECT},
    "two_factor_security": {"subject": SECURITY_SUBJECT},
}

# The notices, with example words. The real ones are written where the change
# is made (notifications.py, two_factor.notify).
NOTICE_TEMPLATES = {
    "notice_password_changed": (
        "Go Style: your password was changed",
        "Your password was changed",
        "The password for your Go Style account was just changed.\n\n"
        "If this was not you, reset your password from the app's sign-in "
        "screen and contact support straight away.",
    ),
    "notice_two_step_off": (
        "Go Style: 2-step verification turned off",
        "2-step verification turned off",
        "2-step verification was turned off for your Go Style account on "
        "iPhone 15 in Dubai.\n\n"
        "If this was not you, reset your password and contact support straight away.",
    ),
    "notice_deletion_scheduled": (
        "Go Style: your account is scheduled for deletion",
        "Your account is scheduled for deletion",
        "Your Go Style account will be deleted on 1 January 2030. Until then you "
        "can sign in to the app and cancel the deletion from your account "
        "settings.\n\n"
        "If you did not ask for this, sign in, cancel the deletion, change "
        "your password and contact support straight away.",
    ),
    "notice_deletion_cancelled": (
        "Go Style: your account will not be deleted",
        "Your account will not be deleted",
        "The deletion of your Go Style account was cancelled. Your account "
        "stays as it is, and you can keep using the app as before.\n\n"
        "If you did not cancel it, change your password and contact support "
        "straight away.",
    ),
    "notice_account_deleted": (
        "Go Style: your account has been deleted",
        "Your account has been deleted",
        "Your Go Style account and the personal details on it have been "
        "deleted, as you asked. This cannot be undone. You are welcome to "
        "create a new account at any time.\n\n"
        "If you did not ask for this, contact support.",
    ),
    "notice_new_sign_in": (
        "Go Style: new sign-in to your account",
        "New sign-in to your account",
        "Your Go Style account was just signed in to on Galaxy S23 Ultra in Dubai.\n\n"
        "If this was you, there is nothing to do. If not, change your "
        "password, sign that device out from Where you're signed in in the "
        "app's settings, and contact support straight away.",
    ),
    "notice_phone_changed": (
        "Your Go Style account details changed",
        "Your account details changed",
        "The phone number on your Go Style account was just changed. "
        "If this was not you, contact support straight away.",
    ),
    "welcome": (
        "Welcome to GoStyle",
        "Welcome, Sara",
        "Your GoStyle account is verified and ready to use.\n\n"
        "Find salons and stylists near you, book a visit in a few taps, and "
        "keep the places you love in your favourites.\n\n"
        "If you did not create this account, contact support.",
        "Your GoStyle account is verified and ready to use.",
    ),
}


class Command(BaseCommand):
    help = (
        "Send one real email to an address, through the configured email "
        "backend, to see a template and prove the SendGrid setup. A code in it "
        "is a placeholder that was never issued. Run from a shell on the "
        "server; there is deliberately no endpoint for this."
    )

    def add_arguments(self, parser):
        parser.add_argument("--to", required=True, help="Address to send to.")
        parser.add_argument(
            "--template",
            choices=[*CODE_TEMPLATES, *NOTICE_TEMPLATES],
            default=Purpose.REGISTER.value,
            help="Which email to send (default: register).",
        )
        parser.add_argument(
            "--first-name", default=None, help="Greeting name, for the code emails that use one."
        )

    def handle(self, *args, **options):
        to = options["to"].strip()
        try:
            validate_email(to)
        except ValidationError:
            raise CommandError("--to is not a valid email address.")

        template = options["template"]
        try:
            if template in CODE_TEMPLATES:
                EmailSender().send(
                    to,
                    PLACEHOLDER_CODE,
                    "email",
                    first_name=options["first_name"],
                    **CODE_TEMPLATES[template],
                )
            else:
                # Not send_notice: that one is silent on a failure, and
                # this command is here to show it.
                mail = build_notice_email(*NOTICE_TEMPLATES[template])
                msg = EmailMultiAlternatives(
                    mail.subject, mail.text, settings.DEFAULT_FROM_EMAIL, [to]
                )
                msg.attach_alternative(mail.html, "text/html")
                msg.send(fail_silently=False)
        except (ImproperlyConfigured, SendGridError) as exc:
            raise CommandError(str(exc))

        backend = settings.EMAIL_BACKEND.rsplit(".", 1)[-1]
        self.stdout.write(
            self.style.SUCCESS(
                f"Handed a {template} email to {to} via {backend}. "
                "For SendGrid that means accepted for processing, not delivered: "
                "check the inbox, then SendGrid's Activity feed if it is missing."
            )
        )

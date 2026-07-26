from django.conf import settings
from django.core.mail import send_mail


class ConsoleOtpSender:
    """Prints the code to the server log. Used for SMS/WhatsApp in DEBUG until a
    real phone provider (Twilio / Meta WhatsApp Cloud API) is wired in."""

    def send(self, destination, code):
        print(f"\n>>> OTP for {destination}: {code}\n")


class WhatsAppOtpSender:
    def send(self, destination, code):
        raise NotImplementedError("Wire the WhatsApp provider here")


class EmailOtpSender:
    """Sends the code over email via Django's configured EMAIL_BACKEND (SMTP in
    production, console backend in local dev)."""

    def send(self, destination, code):
        send_mail(
            subject="Your Go Style verification code",
            message=f"Your verification code is {code}. It expires in 5 minutes.",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[destination],
            fail_silently=False,
        )


def get_sender(channel):
    """Return an OTP sender for the given channel.

    Phone channels (sms/whatsapp) log to the console in DEBUG and raise until a
    real provider is configured in production. Email always goes through the
    Django email backend.
    """
    if channel == "email":
        return EmailOtpSender()
    return ConsoleOtpSender() if settings.DEBUG else WhatsAppOtpSender()

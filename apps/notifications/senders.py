"""
The channels a notification goes out on: push and email. SMS has no
provider yet.

A sender takes one claimed NotificationDelivery and answers a Result; it
never raises for anything the channel says. The worker turns the Result into
the row's new status.

    sent     the channel took it
    skipped  nothing to send to, or the customer switched it off since
    failed   the channel refused it; trying again would not change that
    retry    no answer; worth another go later
"""

from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMultiAlternatives

from apps.accounts import push_api
from apps.accounts.emails import build_notice_email
from apps.accounts.models import NotificationPreference
from apps.accounts.notifications import verified_email
from apps.accounts.sendgrid_backend import SendGridError

from . import registry


@dataclass(frozen=True)
class Result:
    kind: str
    detail: str = ""


def sent(detail=""):
    return Result("sent", detail)


def skipped(reason):
    return Result("skipped", reason)


def failed(error):
    return Result("failed", error)


def retry(error):
    return Result("retry", error)


def push_data(notification):
    """The push's data: what the app needs to open the right screen, all
    strings. `type` stays what an older app build knows for the kinds that
    pushed before the Notification Center (registry.Kind.push_type)."""
    kind = registry.KINDS.get(notification.type)
    data = {
        **notification.data,
        "notificationId": str(notification.id),
        "type": (kind.push_type if kind else None) or notification.type,
        "category": notification.category,
    }
    if notification.target_type:
        data["targetType"] = notification.target_type
        data["targetId"] = notification.target_id
    return data


def send_push(delivery):
    """POST /notifications/user on the push service, to every phone of the
    account. eventId is the notification's id, so the push service drops a
    second send even when this worker died after the first one went out."""
    notification = delivery.notification
    account = notification.account
    if not settings.PUSH_API_KEY:
        return skipped("push_not_configured")
    if not account.is_active or account.deleted_at is not None:
        return skipped("account_gone")
    # Asked again at send time: push turned off since it was queued wins.
    prefs = NotificationPreference.objects.filter(account=account).first()
    if prefs is not None and not prefs.push:
        return skipped("opted_out")

    try:
        status_code, body = push_api.send_to_user(
            user_id=account.pk,
            event_id=f"ntf:{notification.id}",
            title=notification.title,
            body=notification.body,
            data=push_data(notification),
        )
    except push_api.PushApiUnavailable as e:
        return retry(f"push service unavailable: {e}")
    if status_code >= 500:
        return retry(f"push service answered {status_code}")
    if status_code >= 400:
        return failed(f"push service refused {status_code}: {_short(body)}")
    devices = body.get("devices") if isinstance(body, dict) else None
    if devices == 0:
        return skipped("no_devices")
    return sent(f"devices={devices}")


def _short(body):
    # The push service's refusal names a field, never a person.
    return str(body)[:300]


def send_email(delivery):
    """The notification as the branded notice email (emails.build_notice_email),
    through Django's backend (SendGrid). Only to a verified address, read
    again at send time: a customer who changed or lost it since is not
    written to at the old one."""
    notification = delivery.notification
    account = notification.account
    if not account.is_active or account.deleted_at is not None:
        return skipped("account_gone")
    prefs = NotificationPreference.objects.filter(account=account).first()
    if prefs is not None and not prefs.email:
        return skipped("opted_out")
    to = verified_email(account)
    if to is None:
        return skipped("email_unverified")

    mail = build_notice_email(
        notification.title,
        notification.title,
        notification.body,
        preheader=notification.body[:120],
    )
    msg = EmailMultiAlternatives(
        subject=mail.subject,
        body=mail.text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to],
    )
    msg.attach_alternative(mail.html, "text/html")
    try:
        msg.send(fail_silently=False)
    except ImproperlyConfigured:
        # No SENDGRID_API_KEY on this server.
        return skipped("email_not_configured")
    except SendGridError as e:
        # Its message names a status and a reason, never an address.
        if e.status_code is None or e.status_code == 429 or e.status_code >= 500:
            return retry(str(e))
        return failed(str(e))
    return sent("sendgrid")


SENDERS = {
    registry.PUSH: send_push,
    registry.EMAIL: send_email,
}

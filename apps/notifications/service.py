"""
notify(): the one way a notification comes into being.

In one transaction it keeps the item for the Notification Center and queues
one delivery per channel the customer's switches allow. The worker
(worker.py) sends them; nothing here touches the network, so a producer
calling this from inside its own transaction holds nothing open.
"""

from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import NotificationPreference

from . import registry
from .models import Notification, NotificationDelivery


@dataclass(frozen=True)
class Outcome:
    # None when nothing was kept (a promotion whose switch is off).
    notification: Notification | None
    # False for a dedupe_key this account already had: nothing new was made.
    created: bool


def preference(prefs, name):
    """A NotificationPreference switch, at the model's default when the
    customer never opened their settings. Reads only: no row is written."""
    if prefs is not None:
        return getattr(prefs, name)
    return NotificationPreference._meta.get_field(name).default


def channels_for(kind, prefs):
    """The channels this kind goes out on for this customer, by the rules in
    registry.Kind: its own event switch first, then each channel's switch,
    then whether the channel can send at all."""
    if kind.preference is not None and not preference(prefs, kind.preference):
        return []
    return [channel for channel in kind.channels if _channel_on(channel, prefs)]


def _channel_on(channel, prefs):
    if channel == NotificationDelivery.Channel.PUSH:
        return bool(settings.PUSH_API_KEY) and preference(prefs, "push")
    # Email and SMS have no sender here yet
    # (docs/NOTIFICATIONS_SETTINGS_AUDIT.md, phase 3 and decision D8).
    return False


def notify(
    account,
    type_,
    *,
    dedupe_key,
    title,
    body,
    data=None,
    target_type=None,
    target_id=None,
    occurred_at=None,
    deliver_by=None,
    source="customer-api",
):
    """Keep one notification for `account` and queue its alerts.

    `dedupe_key` is the producer's idempotency key: the same key again for
    the same account answers the first row, created=False, and queues
    nothing. `data` values are sent as strings (a push's data is strings
    only). `deliver_by` caps how late an alert may go out; left out, the
    kind's own deliver_within from now.

    Raises registry.UnknownKind for a type not in registry.KINDS.
    """
    kind = registry.kind_of(type_)
    # filter().first(), never for_account(): deciding must not write a row.
    prefs = NotificationPreference.objects.filter(account=account).first()
    if kind.promotional and not preference(prefs, kind.preference):
        return Outcome(None, False)

    now = timezone.now()
    # Both or neither: half a target is nowhere to go.
    has_target = target_type is not None and target_id is not None
    with transaction.atomic():
        # get_or_create survives two producers at once: it catches the
        # unique-constraint error and re-reads the winner's row.
        row, created = Notification.objects.get_or_create(
            account=account,
            dedupe_key=dedupe_key,
            defaults={
                "category": kind.category,
                "type": type_,
                "title": title[:140],
                "body": body[:1000],
                "data": {
                    str(key): str(value)
                    for key, value in (data or {}).items()
                    if value is not None
                },
                "target_type": target_type if has_target else None,
                "target_id": str(target_id) if has_target else None,
                "source": source,
                "occurred_at": occurred_at or now,
                "created_at": now,
            },
        )
        if created:
            NotificationDelivery.objects.bulk_create(
                NotificationDelivery(
                    notification=row,
                    channel=channel,
                    next_attempt_at=now,
                    expires_at=deliver_by or now + kind.deliver_within,
                    created_at=now,
                )
                for channel in channels_for(kind, prefs)
            )
    return Outcome(row, created)

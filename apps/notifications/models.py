"""
The Notification Center (docs/NOTIFICATION_CENTER_API.md).

Notification is the customer's inbox: one row per thing that happened to
them, kept as it was shown. NotificationDelivery is one row per channel that
thing goes out on (push today), worked off by the notification_worker
command. Both are written together, in one transaction, by service.notify().
"""

import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import ConsumerAccount


class Category(models.TextChoices):
    """The Notification Center's tabs. "All" is no filter, not a category."""

    ORDERS = "orders", "orders"
    UPDATES = "updates", "updates"
    PAYMENTS = "payments", "payments"
    PROMOTIONS = "promotions", "promotions"


class Notification(models.Model):
    """
    One item in a customer's Notification Center.

    `type` is a key of registry.KINDS, not a database enum, so a new kind of
    notification needs no migration. `title` and `body` are stored as the
    customer was told them: history does not change when the wording does.

    `dedupe_key` is the producer's own idempotency key (a booking-api outbox
    event, a sign-in session). The same key twice for one account is one row:
    a retried producer can never make a second item, or a second push.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        ConsumerAccount,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    category = models.CharField(max_length=20, choices=Category.choices)
    type = models.CharField(max_length=64)
    title = models.CharField(max_length=140)
    body = models.CharField(max_length=1000)
    # Strings only (they ride in the push's data, which FCM keeps to strings).
    data = models.JSONField(default=dict, blank=True)
    # Where tapping it goes: a booking, a session, a salon. Both or neither.
    target_type = models.CharField(max_length=30, null=True, blank=True)
    target_id = models.CharField(max_length=64, null=True, blank=True)
    source = models.CharField(max_length=30)
    dedupe_key = models.CharField(max_length=200)
    # When the thing happened; created_at is when we heard of it.
    occurred_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True, blank=True)
    # Offers only: hidden from the list and the counts after this.
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "notification"
        constraints = [
            models.UniqueConstraint(
                fields=["account", "dedupe_key"],
                name="notification_account_dedupe_uq",
            ),
            models.CheckConstraint(
                condition=Q(category__in=Category.values),
                name="notification_category_valid",
            ),
        ]
        indexes = [
            # The list, newest first, and one tab of it.
            models.Index(
                fields=["account", "-created_at", "-id"],
                name="notification_list_idx",
            ),
            models.Index(
                fields=["account", "category", "-created_at", "-id"],
                name="notification_tab_idx",
            ),
            # The unread counts read only unread rows.
            models.Index(
                fields=["account", "category"],
                condition=Q(read_at__isnull=True),
                name="notification_unread_idx",
            ),
            # The retention delete.
            models.Index(fields=["created_at"], name="notification_created_idx"),
        ]

    def __str__(self):
        return f"{self.type} for {self.account_id}"


class NotificationDelivery(models.Model):
    """
    One channel of one notification, and how its sending went.

    A row is claimed by the worker with SKIP LOCKED, which counts the attempt
    and pushes next_attempt_at out by a lease: a worker killed mid-send leaves
    a row that comes due again by itself. The write-back is fenced on the
    attempt number, so a slow worker cannot overwrite a newer one's result.

    db_table is not "notification_delivery": that name is booking-api's
    reminder table, which apps/booking_data mirrors read-only.
    """

    class Channel(models.TextChoices):
        PUSH = "push", "push"
        EMAIL = "email", "email"
        SMS = "sms", "sms"

    class Status(models.TextChoices):
        PENDING = "pending", "pending"
        SENT = "sent", "sent"
        FAILED = "failed", "failed"
        SKIPPED = "skipped", "skipped"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notification = models.ForeignKey(
        Notification,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    channel = models.CharField(max_length=10, choices=Channel.choices)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    # Never sent after this: a reminder after the visit is noise.
    expires_at = models.DateTimeField(null=True, blank=True)
    # Trimmed, and never holds personal data.
    last_error = models.TextField(blank=True, default="")
    skip_reason = models.CharField(max_length=40, blank=True, default="")
    # What the channel said back: the push service's device count.
    provider_ref = models.CharField(max_length=200, blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customer_notification_delivery"
        constraints = [
            models.UniqueConstraint(
                fields=["notification", "channel"],
                name="notification_delivery_channel_uq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["next_attempt_at"],
                condition=Q(status="pending"),
                name="notification_delivery_due_idx",
            ),
        ]

    def __str__(self):
        return f"{self.channel} {self.status} for {self.notification_id}"

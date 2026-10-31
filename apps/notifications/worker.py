"""
Works off the queued deliveries (management command notification_worker).

The pattern booking-api, review-service and the platform already use: the
queue is the table itself, claimed with SELECT ... FOR UPDATE SKIP LOCKED, so
two workers never take the same row and Postgres stays the only source of
truth. No broker, no extra library.

    claim     count the attempt and push next_attempt_at out by LEASE, in one
              short transaction. A worker killed mid-send leaves a row that
              comes due again by itself.
    deliver   send outside any transaction, then write the outcome back
              fenced on the attempt number: a slow worker whose lease ran out
              cannot overwrite the result of the one that took over.
"""

import logging
from collections import Counter
from datetime import timedelta

from django.db import transaction
from django.db.models import Min
from django.utils import timezone

from .models import Notification, NotificationDelivery
from .senders import SENDERS, Result, skipped

logger = logging.getLogger(__name__)

BATCH = 50
LEASE = timedelta(minutes=2)
MAX_ATTEMPTS = 5
# The wait after attempt 1, 2, 3 and 4. The fifth failure is final.
BACKOFF = (
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
    timedelta(minutes=30),
)
# How long the Notification Center keeps an item (decision D10).
RETENTION = timedelta(days=180)
PURGE_BATCH = 5000

PENDING = NotificationDelivery.Status.PENDING


def claim(now, batch=BATCH):
    """Take up to `batch` due rows for this worker, each with its attempt
    counted. Returns them with their notification and account loaded."""
    with transaction.atomic():
        rows = list(
            NotificationDelivery.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("notification__account")
            .filter(status=PENDING, next_attempt_at__lte=now)
            .order_by("next_attempt_at")[:batch]
        )
        for row in rows:
            row.attempts += 1
            row.next_attempt_at = now + LEASE
            row.updated_at = now
        NotificationDelivery.objects.bulk_update(
            rows, ["attempts", "next_attempt_at", "updated_at"]
        )
    return rows


def deliver(row, now):
    """Send one claimed row and write down what happened. Returns the
    Result's kind."""
    if row.expires_at is not None and row.expires_at <= now:
        result = skipped("expired")
    else:
        sender = SENDERS.get(row.channel)
        if sender is None:
            result = skipped("channel_unavailable")
        else:
            try:
                result = sender(row)
            except Exception as e:  # noqa: BLE001
                # A bug, not the channel: counted like no answer, so the
                # attempts cap still ends it.
                logger.exception("notification %s %s: sender raised", row.notification_id, row.channel)
                result = Result("retry", f"{type(e).__name__}: {e}")
    finish(row, result, now)
    return result.kind


def finish(row, result, now):
    """The row's new state, written only if it is still this claim's.
    Returns False when a newer claim had taken the row over."""
    fields = {"updated_at": now}
    if result.kind == "sent":
        fields.update(
            status=NotificationDelivery.Status.SENT,
            sent_at=now,
            provider_ref=result.detail[:200],
            last_error="",
        )
    elif result.kind == "skipped":
        fields.update(status=NotificationDelivery.Status.SKIPPED, skip_reason=result.detail[:40])
    elif result.kind == "retry" and row.attempts < MAX_ATTEMPTS:
        fields.update(
            next_attempt_at=now + BACKOFF[row.attempts - 1],
            last_error=result.detail[:500],
        )
    else:
        fields.update(status=NotificationDelivery.Status.FAILED, last_error=result.detail[:500])
    return bool(
        NotificationDelivery.objects.filter(
            pk=row.pk, status=PENDING, attempts=row.attempts
        ).update(**fields)
    )


def run_once(batch=BATCH):
    """One claim and its sends. Returns a Counter of Result kinds."""
    counts = Counter()
    for row in claim(timezone.now(), batch):
        counts[deliver(row, timezone.now())] += 1
    return counts


def purge_old(now, batch=PURGE_BATCH):
    """Delete Notification Center items past RETENTION, `batch` at a time
    (their deliveries go with them). Returns how many went."""
    ids = list(
        Notification.objects.filter(created_at__lt=now - RETENTION)
        .order_by("created_at")
        .values_list("id", flat=True)[:batch]
    )
    if ids:
        Notification.objects.filter(id__in=ids).delete()
    return len(ids)


def queue_stats(now):
    """What the queue looks like, for the log line the worker writes."""
    pending = NotificationDelivery.objects.filter(status=PENDING)
    oldest = pending.filter(next_attempt_at__lte=now).aggregate(at=Min("next_attempt_at"))["at"]
    return {
        "pending": pending.count(),
        "oldest_due_seconds": int((now - oldest).total_seconds()) if oldest else 0,
    }

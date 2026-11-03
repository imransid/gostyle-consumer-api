"""
The purge of the accounts whose 30 days are over (docs/ACCOUNT_DELETION_FE.md).

One loop for the two ways it runs: the purge_due_accounts command, by hand,
and the notification worker, every hour while ACCOUNT_PURGE_V1 is on.

For each account: its bookings still to come are cancelled first
(apps/salons/account_deletion.py), then ConsumerAccount.purge() clears it.
When booking-api cannot be asked, that account waits for a later run, so its
bookings are not left behind by a short outage. Not for ever: once its date
is BOOKINGS_WAIT past, it is purged anyway and the bookings are logged as
left. A deletion is a promise to the customer.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import ConsumerAccount

logger = logging.getLogger(__name__)

# How long past its date an account waits for booking-api before it is
# purged without its bookings cancelled.
BOOKINGS_WAIT = timedelta(days=3)


@dataclass
class PurgeRun:
    purged: int = 0
    # Waiting for booking-api: tried again on the next run.
    waiting: int = 0
    failed: int = 0
    bookings_cancelled: int = 0
    bookings_left: int = 0


def due_accounts(now):
    """deleted_at IS NULL, not NOT NULL: the purge is what sets deleted_at,
    so a row that has it is already done. That is also why a second run is
    safe: the first one takes every due row out of this filter.
    all_objects for clarity only; objects would find the same rows."""
    return ConsumerAccount.all_objects.filter(deleted_at__isnull=True, purge_at__lte=now)


def purge_due(now=None):
    """Cancel the bookings of, then purge, every account due at `now`.
    One account's failure never stops the rest: it stays due."""
    # Imported here: apps.salons imports this app.
    from apps.salons.account_deletion import Unreachable, cancel_bookings_to_come

    now = now or timezone.now()
    due = due_accounts(now)
    run = PurgeRun()
    for pk in list(due.values_list("pk", flat=True)):
        try:
            account = due.filter(pk=pk).first()
            if account is None:  # cancelled since the list was taken
                continue
            try:
                report = cancel_bookings_to_come(account)
                run.bookings_cancelled += report.cancelled
                run.bookings_left += len(report.left)
            except Unreachable as exc:
                if now - account.purge_at < BOOKINGS_WAIT:
                    run.waiting += 1
                    logger.warning(
                        "account deletion: booking-api could not be asked for %s, "
                        "the purge waits for the next run: %s", pk, exc,
                    )
                    continue
                logger.error(
                    "account deletion: booking-api could not be asked for %s for %s, "
                    "purged without its bookings cancelled: %s", pk, BOOKINGS_WAIT, exc,
                )

            with transaction.atomic():
                # Read again under a lock: the member may have cancelled
                # since the list was taken, or another run got there first.
                account = due.select_for_update().filter(pk=pk).first()
                if account is None:
                    continue
                account.purge()
            run.purged += 1
        except Exception:
            # One bad row must not stop the rest. It stays due, so the next
            # run tries it again.
            run.failed += 1
            logger.exception("purge_due_accounts: purge failed for %s", pk)
    return run

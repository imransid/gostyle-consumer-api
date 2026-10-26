import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import ConsumerAccount

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Purge the accounts whose deletion date has passed: purge_at in the "
        "past and not purged yet."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count the accounts that are due and purge nothing.",
        )

    def handle(self, *args, **options):
        # deleted_at IS NULL, not NOT NULL: the purge is what sets deleted_at,
        # so a row that has it is already done. That is also why a second
        # run is safe: the first one takes every due row out of this filter.
        # all_objects for clarity only; objects would find the same rows.
        due = ConsumerAccount.all_objects.filter(
            deleted_at__isnull=True,
            purge_at__lte=timezone.now(),
        )

        if options["dry_run"]:
            self.stdout.write(f"Dry run: {due.count()} account(s) due, none purged.")
            return

        purged = failed = 0
        for pk in list(due.values_list("pk", flat=True)):
            try:
                with transaction.atomic():
                    # Read again under a lock: the member may have cancelled
                    # since the list was taken, or another run got there first.
                    account = due.select_for_update().filter(pk=pk).first()
                    if account is None:
                        continue
                    account.purge()
                purged += 1
            except Exception:
                # One bad row must not stop the rest. It stays due, so the
                # next run tries it again.
                failed += 1
                logger.exception("purge_due_accounts: purge failed for %s", pk)

        self.stdout.write(self.style.SUCCESS(f"Purged {purged} account(s)."))
        if failed:
            raise CommandError(f"{failed} account(s) failed to purge, see the log.")

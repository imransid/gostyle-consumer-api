from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.deletion import due_accounts, purge_due


class Command(BaseCommand):
    help = (
        "Purge the accounts whose deletion date has passed: purge_at in the "
        "past and not purged yet. Their bookings still to come are cancelled "
        "first, as the customer's own cancel. The notification worker runs "
        "the same every hour while ACCOUNT_PURGE_V1 is on."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count the accounts that are due and purge nothing.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            count = due_accounts(timezone.now()).count()
            self.stdout.write(f"Dry run: {count} account(s) due, none purged.")
            return

        run = purge_due()
        self.stdout.write(self.style.SUCCESS(f"Purged {run.purged} account(s)."))
        self.stdout.write(
            f"Bookings cancelled: {run.bookings_cancelled}, left as they were: "
            f"{run.bookings_left}."
        )
        if run.waiting:
            self.stdout.write(
                f"{run.waiting} account(s) wait for booking-api, tried again on the next run."
            )
        if run.failed:
            raise CommandError(f"{run.failed} account(s) failed to purge, see the log.")

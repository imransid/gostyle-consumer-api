import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from apps.notifications import worker

logger = logging.getLogger("apps.notifications.worker")

# How often the housekeeping runs, in seconds.
STATS_EVERY = 60
PURGE_EVERY = 3600


class Command(BaseCommand):
    help = (
        "Send the queued notifications (push), until stopped. Safe to run "
        "more than once at a time: rows are claimed with SKIP LOCKED."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="One claim and its sends, then exit. For a test by hand.",
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=1.0,
            help="Seconds to wait when the queue is empty (default 1).",
        )

    def handle(self, *args, **options):
        if options["once"]:
            self.stdout.write(str(dict(worker.run_once())))
            return

        self.stopping = False
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        logger.info("notification worker started")

        totals = {}
        last_stats = last_purge = 0.0
        while not self.stopping:
            # A long-lived process fires no request signals, so it reaps its
            # own connections, as grpcserver's calls do.
            close_old_connections()
            try:
                counts = worker.run_once()
                for kind, n in counts.items():
                    totals[kind] = totals.get(kind, 0) + n

                clock = time.monotonic()
                if clock - last_purge >= PURGE_EVERY:
                    last_purge = clock
                    gone = worker.purge_old(timezone.now())
                    if gone:
                        logger.info("notification center: %d old items deleted", gone)
                if clock - last_stats >= STATS_EVERY:
                    last_stats = clock
                    stats = worker.queue_stats(timezone.now())
                    logger.info(
                        "notification queue: pending=%d oldest_due_seconds=%d sent=%d "
                        "skipped=%d failed=%d retry=%d",
                        stats["pending"],
                        stats["oldest_due_seconds"],
                        totals.get("sent", 0),
                        totals.get("skipped", 0),
                        totals.get("failed", 0),
                        totals.get("retry", 0),
                    )
                    totals = {}
            except Exception:  # noqa: BLE001
                # The database went away, most likely. Wait and go again;
                # a claimed row's lease brings it back.
                logger.exception("notification worker loop failed")
                counts = None

            if not counts:
                self._sleep(options["interval"])
        logger.info("notification worker stopped")

    def _stop(self, *_):
        self.stopping = True

    def _sleep(self, seconds):
        # In small steps, so a SIGTERM during a quiet spell exits at once.
        end = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < end:
            time.sleep(0.1)

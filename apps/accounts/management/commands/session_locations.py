"""
Why Where you're signed in shows no place, and the fix for live sessions.

    python manage.py session_locations           what is set, and what is wrong
    python manage.py session_locations --apply   also write each live session's
                                                 place, looked up again now

A session gets its place (location) from its IP and the GeoIP file
(apps/accounts/geoip.py). Null comes from one of two things on the server
(docs/DEVICE_SESSIONS_SERVER.md, steps 2 and 3):

  * no GeoIP file: GEOIP_CITY_DB empty, or the file not mounted;
  * no real client IP: without the nginx X-Proxy-Secret matching
    PROXY_SECRET, every session's IP is the container network's own
    (10.x), a private address with no place.

Once both are right, live sessions get their place by themselves at their
next refresh or last_seen_at update. --apply does it now, for every live
session, in today's words ("Dubai, UAE").
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from rest_framework.settings import api_settings as drf_settings

from apps.accounts import geoip
from apps.accounts.models import UserSession

# MaxMind publishes weekly; older than this, the file is not being refreshed.
STALE = timedelta(days=45)


class Command(BaseCommand):
    help = (
        "Say why sessions have no location (GeoIP file, proxy secret), and "
        "with --apply look up every live session's place again."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the place found for each live session. Without it, nothing changes.",
        )

    def handle(self, *args, **options):
        has_database = self.report_settings()

        public = private = no_ip = found = changed = 0
        for session in UserSession.objects.filter(ended_at__isnull=True).iterator():
            if not session.ip_address:
                no_ip += 1
                continue
            if geoip.public_address(session.ip_address) is None:
                private += 1
                continue
            public += 1
            place = geoip.location(session.ip_address) if has_database else geoip.KEEP
            if place is geoip.KEEP:
                continue
            found += place is not None
            if place != session.location:
                changed += 1
                if options["apply"]:
                    UserSession.objects.filter(pk=session.pk).update(location=place)

        self.stdout.write(
            f"Live sessions: {public + private + no_ip}: {public} with a public IP "
            f"({found} with a place), {private} with a private IP, {no_ip} with none."
        )
        if private:
            self.warn(
                f"{private} session(s) have a private IP: the app is not getting the "
                "client's real IP. Check that host nginx includes "
                "/etc/nginx/snippets/gostyle-proxy-secret.conf and that PROXY_SECRET "
                "is the same value (step 2). They fix themselves at their next request."
            )
        if has_database:
            verb = "Changed" if options["apply"] else "Would change (run with --apply)"
            self.stdout.write(f"{verb}: {changed} location(s).")

    def report_settings(self):
        """Print what the lookup depends on. Returns whether the GeoIP file
        can be read."""
        num_proxies = drf_settings.NUM_PROXIES
        self.stdout.write(f"NUM_PROXIES: {num_proxies}")
        if not num_proxies:
            self.warn(
                "NUM_PROXIES is not set: X-Forwarded-For is never read (DJANGO_NUM_PROXIES=1)."
            )
        if settings.PROXY_SECRET:
            self.stdout.write("PROXY_SECRET: set")
        else:
            self.warn(
                "PROXY_SECRET: not set. Every session's IP is the container's own, "
                "which has no place (docs/DEVICE_SESSIONS_SERVER.md step 2)."
            )

        path = settings.GEOIP_CITY_DB
        meta = geoip.database()
        if meta is None:
            why = "not set" if not path else f"{path} cannot be read (is /var/lib/GeoIP mounted?)"
            self.warn(f"GEOIP_CITY_DB: {why}. No session gets a place (step 3).")
            return False
        built = datetime.fromtimestamp(meta.build_epoch, tz=dt_timezone.utc)
        self.stdout.write(f"GEOIP_CITY_DB: {path} ({meta.database_type}, built {built:%Y-%m-%d})")
        if timezone.now() - built > STALE:
            self.warn("The GeoIP file is old: is the monthly geoipupdate cron running?")
        return True

    def warn(self, message):
        self.stdout.write(self.style.WARNING(message))

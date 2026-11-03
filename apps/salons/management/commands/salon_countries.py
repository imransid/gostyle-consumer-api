"""
Every live salon's country, worked out from its map pin (MAP_COUNTRY_V1,
docs/MAP_COUNTRY_SERVER.md).

    python manage.py salon_countries           # what would change, writes nothing
    python manage.py salon_countries --apply   # writes it

A salon needs a row when it has none (new, or before the first run) or its
pin has moved since its row was written. The map redoes up to 500 of these a
minute by itself (map_country.keep_salons_fresh); this command does all of
them at once, which is the backfill to run on deploy, and prints how many
live salons each country has.

"Address says" is platform's branch.country_code, from the salon's address
form. The map never uses it: it is shown so a pin in the wrong country (or a
typo in the address) is easy to spot.
"""

from django.core.cache import cache
from django.core.management.base import BaseCommand

from apps.platform_data.models import Branch
from apps.salons import map_country

BLANK = "-"


class Command(BaseCommand):
    help = "Work out each live salon's country from its map pin. Writes nothing without --apply."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Write the rows. Without it, nothing is written."
        )

    def handle(self, *args, apply=False, **options):
        rows = map_country.work_out(map_country.stale_branches())
        branches = {
            branch["id"]: branch
            for branch in Branch.objects.filter(id__in=[row.branch_id for row in rows]).values(
                "id", "name", "city", "country_code"
            )
        }

        self.stdout.write(f"Salons with no country yet, or whose pin moved: {len(rows)}")
        for row in rows:
            branch = branches.get(row.branch_id, {})
            typed = (branch.get("country_code") or "").strip().upper()
            note = "  (address says another country)" if len(typed) == 2 and typed != row.country_code else ""
            self.stdout.write(
                f"  {row.branch_id}  {branch.get('name') or BLANK} ({branch.get('city') or BLANK})"
                f"  pin {row.lat:.5f},{row.lng:.5f}  country {row.country_code or BLANK}"
                f"  address says {typed or BLANK}{note}"
            )

        if apply:
            written = map_country.save(rows)
            self.stdout.write(self.style.SUCCESS(f"Written: {written}"))
        elif rows:
            self.stdout.write(self.style.WARNING("Nothing written. Run again with --apply to write these."))

        cache.delete(map_country.COUNTS_KEY)
        counts = map_country.live_salon_counts()
        when = "now" if apply or not rows else "before --apply"
        self.stdout.write(f"Live salons per country ({when}):")
        for code, salons in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            self.stdout.write(f"  {code}  {salons}")
        if not counts:
            self.stdout.write(f"  {BLANK}")

"""
Report which branches have no timezone set. READ ONLY.

`branch.timezone` is nullable, and apps/salons/timezones.py falls back to
Asia/Dubai when it is empty. That fallback is right for the salons GoStyle
launched with and wrong for every branch outside the Gulf, so the useful
question is not "is there a fallback" but "how many rows are riding on it, and
where are they".

This command answers that and NOTHING else. It runs SELECTs, writes nothing,
and does not guess what any branch's timezone ought to be — the correct value
lives with whoever onboarded the branch, and inferring one from a city name is
how a wrong timezone gets a veneer of authority. The output is a worklist for
the platform side, not a migration.
"""

from collections import Counter

from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef, Q

from apps.platform_data.models import Branch, Storefront
from apps.salons.timezones import DEFAULT_TIMEZONE


# "Unset" has two spellings in a nullable TEXT column, and both behave
# identically at the call site: `"" or DEFAULT` falls back exactly as
# `None or DEFAULT` does. A report counting only NULLs would understate the
# problem by however many rows were written as empty strings.
UNSET = Q(timezone__isnull=True) | Q(timezone="")

BLANK = "—"


class Command(BaseCommand):
    help = "Report branches with no timezone set. Read only; writes nothing."

    def handle(self, *args, **options):
        # Only PUBLIC storefronts matter for exposure. A branch with no public
        # storefront also has no timezone bug a customer can see: the fallback
        # fires for nobody, because nobody can reach the salon.
        public_storefront = Storefront.objects.filter(
            branch_id=OuterRef("pk"),
            visibility="PUBLIC",
            deleted_at__isnull=True,
        )

        live = Branch.objects.filter(deleted_at__isnull=True)
        total = live.count()

        rows = list(
            live.filter(UNSET)
            .annotate(is_public=Exists(public_storefront))
            .values("id", "name", "city", "country_code", "is_public")
            .order_by("country_code", "city", "name")
        )
        exposed = [r for r in rows if r["is_public"]]

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "Branch timezone report — read only, nothing written."
        ))
        self.stdout.write("")
        self.stdout.write(f"  Branches (not deleted)          {total:>6}")
        self.stdout.write(
            f"  With no timezone set            {len(rows):>6}"
            f"{self._percent(len(rows), total)}"
        )

        exposure = f"  ...of those, publicly visible   {len(exposed):>6}"
        self.stdout.write(
            self.style.ERROR(exposure) if exposed else self.style.SUCCESS(exposure)
        )
        self.stdout.write("")
        self.stdout.write(
            f"  Assumed when unset: {DEFAULT_TIMEZONE} "
            f"(apps/salons/timezones.DEFAULT_TIMEZONE)"
        )

        if not rows:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS(
                "  Every live branch has a timezone. The fallback never fires."
            ))
            return

        self._table(rows)
        self._by_country(rows)

        self.stdout.write("")
        if exposed:
            self.stdout.write(self.style.ERROR(
                f"  {len(exposed)} branch(es) marked 'yes' are serving customers "
                f"hours computed in {DEFAULT_TIMEZONE}."
            ))
            self.stdout.write(
                "  Fix those first, on the platform side. Every one whose real "
                "timezone is\n  not " + DEFAULT_TIMEZONE + " is showing the wrong "
                "open/closed badge right now."
            )
        else:
            self.stdout.write(
                "  None of these back a public storefront, so no customer is "
                "seeing a wrong\n  badge today. They become a bug the moment one "
                "of them is published."
            )
        self.stdout.write("")

    # ── output helpers ───────────────────────────────────────────────────
    @staticmethod
    def _percent(part, whole):
        return f"  ({round(100 * part / whole)}%)" if whole else ""

    def _table(self, rows):
        def cell(row, key):
            return row[key] or BLANK

        widths = {
            "country_code": max(7, *(len(cell(r, "country_code")) for r in rows)),
            "city": max(4, *(len(cell(r, "city")) for r in rows)),
            "name": max(6, *(len(cell(r, "name")) for r in rows)),
        }

        self.stdout.write("")
        self.stdout.write("  Branches with no timezone:")
        self.stdout.write("")
        self.stdout.write(
            f"    {'COUNTRY':<{widths['country_code']}}  "
            f"{'CITY':<{widths['city']}}  "
            f"{'BRANCH':<{widths['name']}}  PUBLIC"
        )
        for row in rows:
            line = (
                f"    {cell(row, 'country_code'):<{widths['country_code']}}  "
                f"{cell(row, 'city'):<{widths['city']}}  "
                f"{cell(row, 'name'):<{widths['name']}}  "
                f"{'yes' if row['is_public'] else 'no'}"
            )
            self.stdout.write(self.style.WARNING(line) if row["is_public"] else line)

    def _by_country(self, rows):
        # The question this command was written to answer. A country_code of
        # its own being null is the worse finding of the two: a branch with
        # neither a timezone nor a country is one nobody can even look up.
        tally = Counter(r["country_code"] or "(not set)" for r in rows)

        self.stdout.write("")
        self.stdout.write("  By country:")
        for code, count in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])):
            self.stdout.write(f"    {code:<12} {count:>4}")

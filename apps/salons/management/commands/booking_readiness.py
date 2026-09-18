"""
Why can't this salon be booked?

Read-only. Answers, per salon, the question the customer app asks twice — who
is qualified, and when are they free — and names the first thing missing when
the answer is "nobody" or "never".

    python manage.py booking_readiness
    python manage.py booking_readiness --salon 4aa5a05b-… --days 14

The booking flow needs four things to line up, and each one is somewhere
different in the platform:

    services  → stages → catalog_skill      Service Builder
    stylists  → skill  → staff_skill_…      Staff → Skills
    catalog_skill.code == skill.code        the bridge between those two
    stylists  → shift  → shift_roster       Staff Operations → Shifts

Three of the four can be perfectly good while the fourth is empty, and the API
answers `200` with an empty list either way — correctly, because "nobody is
free" is a real answer and not an error. This command is what tells those
cases apart.
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand

from apps.salons import timezones
from apps.salons.hours import weekly_row
from apps.salons.snapshot import read_snapshot
from apps.salons.selectors import (
    discoverable_salons,
    manual_state_on,
    with_published_card_fields,
    salon_services,
    salon_stylists,
    service_stage_rows,
    shift_rows,
    skill_bridge,
    staff_skill_rows,
)
from apps.salons.skills import held_levels, requirements


class Command(BaseCommand):
    help = "Report what each salon is still missing before it can be booked."

    def add_arguments(self, parser):
        parser.add_argument("--salon", help="One storefront UUID. Default: all.")
        parser.add_argument(
            "--days", type=int, default=14,
            help="How far ahead to look for rostered shifts (default 14).",
        )

    def handle(self, *args, **options):
        salons = with_published_card_fields(discoverable_salons())
        if options["salon"]:
            salons = salons.filter(id=options["salon"])

        salons = list(salons)
        if not salons:
            self.stdout.write(self.style.WARNING("No discoverable salons."))
            return

        blocked = 0
        for salon in salons:
            if not self.report(salon, options["days"]):
                blocked += 1

        self.stdout.write("")
        total = f"{len(salons)} salon" + ("" if len(salons) == 1 else "s")
        if blocked:
            self.stdout.write(self.style.WARNING(
                f"{blocked} of {total} cannot be booked yet. "
                "Every line marked MISSING is data entered on the platform, "
                "not a change to this service."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"{total} ready: the booking flow has what it needs."
            ))

    def report(self, salon, days):
        name = getattr(salon, "published_name", None) or str(salon.id)
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"{name}  ({salon.id})"))

        stylists = list(salon_stylists(salon))
        services = list(salon_services(salon))
        self.line("stylists (active)", len(stylists), len(stylists) > 0,
                  "No active staff. Staff → invite and activate.")
        self.line("bookable services", len(services), len(services) > 0,
                  "No PUBLISHED, online-bookable service.")
        if not stylists or not services:
            return False

        # ── Do services say what skill they need? ──
        service_ids = [s.id for s in services]
        stages = service_stage_rows(service_ids)
        staged = {row["service_id"] for row in stages}
        self.line(
            "services with a skill", f"{len(staged)}/{len(services)}",
            self.how_many(len(staged), len(services)),
            "A service with no stages requires no skill, and the Expert step "
            "answers 422 service_without_skill. Service Builder → Stages.",
        )

        # ── Does the tenant's own skill catalogue match those skills? ──
        bridge = skill_bridge(salon.tenant_id, {row["skill_id"] for row in stages})
        bridged = sum(1 for target in bridge.values() if target is not None)
        self.line(
            "skills matched by code", f"{bridged}/{len(bridge)}",
            self.how_many(bridged, len(bridge)),
            "A required catalog_skill has no tenant skill with the same "
            "`code`. Staff → Skills: add it, and make the code match.",
        )

        # ── Does anyone hold them? ──
        staff_ids = [s.id for s in stylists]
        assignments = staff_skill_rows(salon.tenant_id, staff_ids)
        self.line(
            "staff skill assignments", len(assignments), len(assignments) > 0,
            "Nobody holds any skill, so nobody is qualified for anything. "
            "Staff → Skills → assign.",
        )

        required = requirements(stages, bridge)
        held = held_levels(assignments)
        bookable = [
            service for service in services
            if service.id in required and any(
                self.can(required[service.id], held.get(staff_id, {}))
                for staff_id in staff_ids
            )
        ]
        self.line(
            "services someone can do", f"{len(bookable)}/{len(services)}",
            self.how_many(len(bookable), len(services)),
            "A service here needs a skill nobody holds at the level it asks "
            "for. Check the level too: a stage wanting 4 needs a MASTER.",
        )

        # ── Is the clock right? ──
        # Not a blocker — a null timezone falls back to Asia/Dubai and every
        # time still comes back — which is exactly why it is worth printing:
        # a salon in Dhaka answers two hours out and nothing else complains.
        self.line(
            "branch timezone", salon.branch_timezone or "not set",
            bool(salon.branch_timezone),
            "Times will be answered in "
            f"{timezones.DEFAULT_TIMEZONE}, which is a guess. Branch settings "
            "→ set the real timezone.",
        )

        # ── Is anyone rostered, on a day the salon is open? ──
        # Counted per DAY, not per stylist: a roster that covers only the days
        # the salon is shut offers nothing, and the two calendars are edited
        # in different screens by different people.
        today = date.today()
        weekly = read_snapshot(salon)["HOURS"].get("weekly")
        rostered, open_days, bookable_days = set(), 0, 0
        for offset in range(days):
            day = today + timedelta(days=offset)
            row = weekly_row(weekly, day.weekday())
            is_open = bool(row) and not row.get("closed") and \
                manual_state_on(salon.id, day) != "CLOSED"
            on_duty = shift_rows(
                salon.tenant_id, salon.branch_id, staff_ids, day
            )
            rostered.update(r["staff_member_id"] for r in on_duty)
            open_days += is_open
            bookable_days += bool(is_open and on_duty)

        self.line(
            f"stylists rostered in {days}d", f"{len(rostered)}/{len(stylists)}",
            self.how_many(len(rostered), len(stylists)),
            "No shifts, so no working hours, so no times to offer. "
            "Staff Operations → Shifts → publish a roster for THIS branch "
            f"(branch_id {salon.branch_id}).",
        )
        self.line(
            "open AND staffed days", f"{bookable_days}/{open_days} open days",
            self.how_many(bookable_days, open_days),
            "The salon is open on days nobody is rostered — a day off, or "
            "a roster that simply runs out before this window does. Both "
            "calendars have to agree before a time can be offered.",
        )

        return bool(bookable) and bool(bookable_days)

    @staticmethod
    def how_many(found, wanted):
        """True for all of them, False for none, None for some."""
        if wanted and found == wanted:
            return True
        return False if not found else None

    @staticmethod
    def can(required, holder):
        from apps.salons.skills import can_perform
        return can_perform(required, holder)

    def line(self, label, value, ok, remedy):
        """
        `ok` is True, False, or None for "some but not all".

        The difference matters: a salon where one service of four needs a
        skill nobody has is a salon that can still take bookings for the
        other three, and printing that in the same red as "no staff at all"
        would send someone hunting for a fault that is really a gap.
        """
        mark = {
            True: self.style.SUCCESS("OK     "),
            None: self.style.WARNING("PARTIAL"),
            False: self.style.ERROR("MISSING"),
        }[ok]
        self.stdout.write(f"  {mark} {label:<28} {value}")
        if ok is not True:
            for part in self.wrap(remedy):
                self.stdout.write(f"         {self.style.WARNING(part)}")

    @staticmethod
    def wrap(text, width=66):
        words, line, out = text.split(), "", []
        for word in words:
            if len(line) + len(word) + 1 > width:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        if line:
            out.append(line)
        return out

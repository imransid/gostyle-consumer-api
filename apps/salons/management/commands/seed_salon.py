
"""
Insert one complete salon for local development.

The profile endpoints read across storefront, branch, tenant, categories,
services, availability, staff, packages, products, the two skill catalogues
the booking flow matches across, and a published version snapshot. Testing them needs all of those to exist and agree with each other,
which no fixture file expresses readably.

WRITES TO PLATFORM-OWNED TABLES. Every table here is managed = False and
belongs to gostyle-platform. That is acceptable on a local database and
nowhere else, so the command refuses to run against a non-local host.
"""

import json
import uuid
import zoneinfo
from datetime import datetime, time, timedelta, timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

# Fixed UUIDs so the seeded salon has a stable URL across re-runs. A random
# id would mean fishing the new one out of the output on every seed.
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
BRANCH_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
STOREFRONT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
VERSION_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

# Two levels: one parent chip with two child groups beneath it. The platform
# schema supports this through category.parent_id and no tenant populates it
# today, so the seed is the only place the tree case can be exercised.
CAT_HAIR_ID = uuid.UUID("55555555-5555-5555-5555-555555555551")
CAT_CUTS_ID = uuid.UUID("55555555-5555-5555-5555-555555555552")
CAT_BEARD_ID = uuid.UUID("55555555-5555-5555-5555-555555555553")

BOOKING_ID = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeee1")
BOOKING_ITEM_ID = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeee2")

PKG_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1")
PKG_AVAIL_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2")

CATEGORIES = [
    (CAT_HAIR_ID, "Haircut and Styling", "قص وتصفيف الشعر", None),
    (CAT_CUTS_ID, "Precision Cuts", "قصات دقيقة", CAT_HAIR_ID),
    (CAT_BEARD_ID, "Beard Care", "العناية باللحية", CAT_HAIR_ID),
]

# name, category_id, price_minor, minutes, code, branch_price_minor
SERVICES = [
    ("The Gentleman's Cut", CAT_CUTS_ID, 19900, 30, "SVC-001", None),
    ("Modern Fade", CAT_CUTS_ID, 21500, 25, "SVC-002", None),
    ("Hot Towel Shave", CAT_BEARD_ID, 26000, 35, "SVC-003", None),
    # No category at all, and a branch price override. Two edges in one row:
    # the "Other" fallback must keep a bookable service visible rather than
    # silently dropping it, and 150.00 must win over the catalogue's 180.00.
    ("Scalp Treatment", None, 18000, 20, "SVC-004", 15000),
]

# first, last, job_title, position, onboarding_state
STAFF = [
    ("Liam", "Johnson", "Barber and Grooming Expert", "Senior Barber", "ACTIVE"),
    ("Darius", "Stone", "Haircut and Styling Expert", "Master Barber", "ACTIVE"),
    # employment_status ACTIVE but onboarding_state INVITED: a real person who
    # was invited and never accepted. They have never worked a shift, so they
    # must NOT appear on a public profile. This row is the only proof that the
    # selector filters on BOTH status columns rather than just employment.
    ("Ghost", "Invitee", "Barber", None, "INVITED"),
]

# Minimum notice, in minutes, by service index. Only one service sets it, and
# that is the point: the Hot Towel Shave cannot be booked for the next two
# hours while every other service can, so the nearest-available window has
# something to drop. NULL everywhere else, which is the real-world default.
SERVICE_LEAD_TIMES = {2: 120}

# WHO CAN DO WHAT. Two skill catalogues that nothing joins: `catalog_skill` is
# platform-wide reference data a service stage points at, `skill` is the
# tenant's own list a staff member is assigned from, and the two are matched on
# `code` (see apps/salons/skills.py). Seeding both is what makes the booking
# flow's Expert step answerable at all — without stages, every service requires
# no skill and the endpoint answers 422 service_without_skill.

# code, name_en. Platform-wide, shared by every tenant.
CATALOG_SKILLS = [
    ("HAIRCUT", "Haircut"),
    ("BEARD", "Beard Care"),
    # No tenant skill matches this one. Scalp Treatment therefore requires a
    # skill nobody at this salon can hold, which is the only proof that an
    # unbridged requirement answers 200 with an empty list rather than
    # qualifying everybody.
    ("SCALP", "Scalp Care"),
]

SKILL_CATEGORY_ID = uuid.UUID("ffffffff-ffff-ffff-ffff-fffffffffff0")

# The tenant's own spelling of those codes. "haircut" against the catalogue's
# "HAIRCUT" is deliberate: the bridge matches on a trimmed, lowercased code,
# and an exact-match implementation would pass every other test but this one.
TENANT_SKILLS = [
    ("haircut", "Haircut"),
    ("BEARD", "Beard Care"),
]

# service index -> [(catalog code, min_level 1..5)]. Levels map onto the staff
# ladder as 1=TRAINEE, 2=JUNIOR, 3=SENIOR, 4=MASTER.
SERVICE_SKILLS = {
    0: [("HAIRCUT", 2)],   # Gentleman's Cut: any qualified barber
    1: [("HAIRCUT", 4)],   # Modern Fade: MASTER only
    2: [("BEARD", 3)],     # Hot Towel Shave: SENIOR beard work
    3: [("SCALP", 1)],     # Scalp Treatment: nobody holds this skill
}

# staff index -> [(tenant skill code, level)]. The result the Expert step shows:
# the Fade is Darius alone, the Shave is Liam alone, the Cut is both, and the
# Scalp Treatment is nobody.
STAFF_SKILLS = {
    0: [("haircut", "SENIOR"), ("BEARD", "MASTER")],   # Liam
    1: [("haircut", "MASTER"), ("BEARD", "JUNIOR")],   # Darius
    # Ghost Invitee holds nothing, and is filtered out before skills anyway.
}

# WHEN THEY WORK. A shift belongs to a weekly roster (Monday-based) for one
# branch; times are plain "HH:MM" strings and `break_time` is the start of a
# fixed one-hour unpaid break. Dates are relative to the day the seed runs,
# because a roster written to fixed dates is a roster that is stale tomorrow
# and an availability endpoint with nothing to offer by next week.
SHIFT_WEEKS = 2

# staff index -> (start, end, break start or None, weekday to skip or None)
SHIFTS = {
    # Opens before the salon does (10:00), which must not become a bookable
    # 09:00 start: the salon's own hours bound every shift.
    0: ("09:00", "18:00", "13:00", 1),   # Liam, Tuesdays off, lunch at 13:00
    # Runs to 23:00 against a 22:00 close on most days — the same clamp at the
    # other end — and takes no break.
    1: ("12:00", "23:00", None, None),   # Darius
    # Ghost Invitee is rostered nowhere, as befits someone who never accepted.
}

# A seeded appointment, so the day has a hole in it and `bookings_today` is not
# 0 everywhere. Darius, 15:00-16:00 salon time, on the first day after today
# the salon is open.
BOOKED_STAFF_INDEX = 1
BOOKED_SERVICE_INDEX = 0
BOOKED_LOCAL_HOUR = 15
BOOKED_MINUTES = 60
SALON_TIMEZONE = "Asia/Dubai"

# uuid5 rather than a fixed hex pattern: a shift id has to be derived from the
# date it falls on, and those dates move with the calendar.
SEED_NAMESPACE = uuid.UUID("99999999-0000-0000-0000-000000000000")

# (service index, quantity). Members are Gentleman's Cut (199.00, 30m) and
# Hot Towel Shave (260.00, 35m): 459.00 and 65 minutes bought separately,
# against a bundle price of 370.00. None of those three derived numbers is
# stored anywhere, so this package is the only proof the Sum subqueries in
# salon_packages() compute price_before, save_amount and duration correctly.
PKG_ITEMS = [(0, 1), (2, 1)]
PKG_PRICE_MINOR = 37000

# name, type, price_minor. The PROFESSIONAL row is salon-use stock, not for
# sale, and it must NOT reach the shop tab. It is the only proof the type
# filter in salon_products() actually runs.
PRODUCTS = [
    ("Iron Beard Oil", "RETAIL", 18000),
    ("Matte Hair Clay", "RETAIL", 22000),
    ("Salon Developer 20 Vol", "PROFESSIONAL", 4500),
]

SNAPSHOT = {
    "IDENTITY": {
        "nameEn": "The Iron Razor Barbershop",
        "nameAr": "حلاق الشفرة الحديدية",
        "tagEn": "Precision Cuts and Classic Shaves Since 2015",
        "aboutEn": (
            "Redefining the modern grooming experience. We blend traditional "
            "barbering with contemporary style, from hot towel shaves to "
            "precision fades and expert beard sculpting."
        ),
    },
    "AUDIENCE": {"mode": "GENTS", "privateRooms": False},
    "BADGES": {"priceTier": "UPSCALE", "manualBadges": []},
    "AMENITIES": {
        # ROOFTOP is deliberately included and is NOT a legal key. It proves
        # translate.amenities drops unknown values instead of passing them on.
        "items": ["WIFI", "REFRESHMENTS", "PARKING", "CARD_PAYMENT", "ROOFTOP"]
    },
    "SOCIALS": {
        "instagram": "theironrazor",
        "tiktok": "@theironrazor",
        "youtube": "ironrazor",
        "whatsapp": "+971 50 123 4567",
        "website": "https://ironrazor.ae",
        # YouTube has a handle AND is hidden: the pair the platform explicitly
        # allows, and the case a naive reader would publish by mistake.
        "hidden": ["YOUTUBE"],
    },
    "HOURS": {
        "weekly": [
            {"day": "mon", "closed": False, "open": "10:00", "close": "22:00"},
            {"day": "tue", "closed": False, "open": "10:00", "close": "22:00"},
            {"day": "wed", "closed": False, "open": "10:00", "close": "22:00"},
            {"day": "thu", "closed": False, "open": "10:00", "close": "23:00"},
            {"day": "fri", "closed": False, "open": "14:00", "close": "23:00"},
            {"day": "sat", "closed": False, "open": "10:00", "close": "22:00"},
            {"day": "sun", "closed": True},
        ]
    },
    "MAP": {
        "lat": 25.2213,
        "lng": 55.2621,
        "address": "Shop 4, Al Wasl Road, Jumeirah 1, Dubai",
        "unitEn": "Ground floor, next to the pharmacy",
    },
    # GALLERY_FEATURED and COVER are intentionally ABSENT. A salon that
    # published before those sections existed is the normal case snapshot.py
    # was written to survive, so the seed must contain one.
}


# Deterministic ids so a re-run inserts nothing new and duplicates nothing.
# uuid4() here would defeat ON CONFLICT, because a fresh random id never
# conflicts and every run would add another copy.
def service_id(index):
    return uuid.UUID(f"66666666-6666-6666-6666-66666666666{index}")


def availability_id(index):
    return uuid.UUID(f"77777777-7777-7777-7777-77777777777{index}")


def user_id(index):
    return uuid.UUID(f"88888888-8888-8888-8888-88888888888{index}")


def staff_id(index):
    return uuid.UUID(f"99999999-9999-9999-9999-99999999999{index}")


def skill_id(index):
    return uuid.UUID(f"ffffffff-ffff-ffff-ffff-fffffffffff{index + 1}")


def stage_id(index):
    return uuid.UUID(f"12121212-1212-1212-1212-12121212121{index}")


def roster_id(week_start):
    return uuid.uuid5(SEED_NAMESPACE, f"roster:{BRANCH_ID}:{week_start}")


def shift_id(staff_index, day):
    return uuid.uuid5(SEED_NAMESPACE, f"shift:{staff_index}:{day}")


def package_item_id(order):
    return uuid.UUID(f"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb{order}")


def product_id(index):
    return uuid.UUID(f"cccccccc-cccc-cccc-cccc-ccccccccccc{index}")


def variant_id(index):
    return uuid.UUID(f"dddddddd-dddd-dddd-dddd-ddddddddddd{index}")


class Command(BaseCommand):
    help = "Seed one salon into the local platform tables for development."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Delete and recreate the seeded rows if they already exist.",
        )

    def handle(self, *args, **options):
        host = connection.settings_dict.get("HOST", "")
        if host not in ("127.0.0.1", "localhost", "db", ""):
            raise CommandError(
                f"Refusing to seed: DB_HOST is {host!r}, which is not local. "
                "These tables belong to gostyle-platform."
            )

        now = datetime.now(timezone.utc)

        with transaction.atomic(), connection.cursor() as cur:
            if options["force"]:
                self._purge(cur)

            cur.execute(
                """
                INSERT INTO public.tenant
                    (id, name, currency_default, locale_default, status,
                     created_at, updated_at, tz)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                [TENANT_ID, "Iron Razor Group", "AED", "en", "ACTIVE",
                 now, now, "Asia/Dubai"],
            )

            cur.execute(
                """
                INSERT INTO public.branch
                    (id, tenant_id, code, name, city, lat, lng, timezone,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                [BRANCH_ID, TENANT_ID, "IR-JUM", "Iron Razor Jumeirah", "Dubai",
                 25.2213, 55.2621, "Asia/Dubai", now, now],
            )

            cur.execute(
                """
                INSERT INTO public.storefront
                    (id, tenant_id, branch_id, slug, visibility, link_enabled,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                [STOREFRONT_ID, TENANT_ID, BRANCH_ID, "iron-razor-jumeirah",
                 "PUBLIC", True, now, now],
            )

            cur.execute(
                """
                INSERT INTO public.storefront_version
                    (id, tenant_id, storefront_id, revision_no, snapshot,
                     changed_sections, published_at, is_live)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                [VERSION_ID, TENANT_ID, STOREFRONT_ID, 1,
                 json.dumps(SNAPSHOT), [], now, True],
            )

            # live_version_id is set AFTER the version exists, which is also
            # the order the platform's publish handler uses.
            cur.execute(
                "UPDATE public.storefront SET live_version_id = %s WHERE id = %s",
                [VERSION_ID, STOREFRONT_ID],
            )

            # Categories before services: a service points at a category, and
            # inserting the child first would fail the foreign key.
            for cat_id, name, name_ar, parent_id in CATEGORIES:
                cur.execute(
                    """
                    INSERT INTO public.category
                        (id, tenant_id, name_en, name_ar, slug, sort_order,
                         parent_id, icon, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    [cat_id, TENANT_ID, name, name_ar,
                     name.lower().replace(" ", "-"), 0, parent_id,
                     "scissors", now, now],
                )

            for index, (name, cat_id, price, minutes, code, branch_price) in enumerate(SERVICES):
                cur.execute(
                    """
                    INSERT INTO public.service
                        (id, tenant_id, name, description, duration_minutes,
                         price_minor, currency, status, code, audience,
                         category_id, online_booking_enabled,
                         lead_time_minutes, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    [service_id(index), TENANT_ID, name,
                     f"{name} at Iron Razor.", minutes, price, "AED",
                     "PUBLISHED", code, "MALE", cat_id, True,
                     SERVICE_LEAD_TIMES.get(index), now, now],
                )

                # A service is bookable only where it is AVAILABLE, so the row
                # is not optional: without it the selector filters the service
                # out entirely.
                cur.execute(
                    """
                    INSERT INTO public.service_branch_availability
                        (id, service_id, branch_id, available, price_minor,
                         currency, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    [availability_id(index), service_id(index), BRANCH_ID,
                     True, branch_price, "AED", now, now],
                )

            # The person is a user_account; the job is a staff_profile. Both
            # rows are needed, and the account comes first because the profile
            # points at it.
            for index, (first, last, job_title, position, onboarding) in enumerate(STAFF):
                cur.execute(
                    """
                    INSERT INTO public.user_account
                        (id, tenant_id, phone_e164, first_name, last_name,
                         locale, status, scopes, failed_login_count,
                         job_title, all_branch_access, must_change_password,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    [user_id(index), TENANT_ID, f"+9715012345{index}0",
                     first, last, "en", "ACTIVE", json.dumps([]), 0,
                     job_title, False, False, now, now],
                )

                cur.execute(
                    """
                    INSERT INTO public.staff_profile
                        (id, tenant_id, user_id, branch_id, position,
                         employment_status, onboarding_state, skills, shifts,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    [staff_id(index), TENANT_ID, user_id(index), BRANCH_ID,
                     position, "ACTIVE", onboarding,
                     json.dumps([]), json.dumps({}), now, now],
                )

            # ── Skills ──────────────────────────────────────────────────
            # catalog_skill is platform-wide reference data with a UNIQUE code,
            # so a real seeded row may already hold "HAIRCUT" under an id of
            # its own. Conflict on the CODE and read the id back, rather than
            # assuming the one invented here won.
            catalog_ids = {}
            for order, (code, name_en) in enumerate(CATALOG_SKILLS):
                cur.execute(
                    """
                    INSERT INTO public.catalog_skill
                        (id, code, name_en, sort_order, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (code) DO NOTHING
                    """,
                    [uuid.uuid4(), code, name_en, order, now, now],
                )
                cur.execute(
                    "SELECT id FROM public.catalog_skill WHERE code = %s", [code]
                )
                catalog_ids[code] = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO public.skill_category
                    (id, tenant_id, name, icon, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                [SKILL_CATEGORY_ID, TENANT_ID, "Hair and Grooming",
                 "scissors", now, now],
            )

            tenant_skill_ids = {}
            for index, (code, name) in enumerate(TENANT_SKILLS):
                cur.execute(
                    """
                    INSERT INTO public.skill
                        (id, tenant_id, code, name, icon, category_id,
                         min_bookable_level, cert_required,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    [skill_id(index), TENANT_ID, code, name, "scissors",
                     SKILL_CATEGORY_ID, "JUNIOR", False, now, now],
                )
                tenant_skill_ids[code] = skill_id(index)

            # One stage per service here, which is enough to require a skill.
            # A service's stages are also where its duration comes from on the
            # platform; the seed keeps duration_minutes on the service row as
            # the customer API reads it, so the two agree by construction.
            for index, stages in SERVICE_SKILLS.items():
                for order, (code, min_level) in enumerate(stages):
                    cur.execute(
                        """
                        INSERT INTO public.service_stage
                            (id, service_id, name_en, duration_minutes,
                             sort_order, min_level, skill_id, resource_type,
                             capacity, buffer_pre, buffer_post,
                             duration_impact, products, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        [stage_id(index), service_id(index),
                         SERVICES[index][0], SERVICES[index][3], order,
                         min_level, catalog_ids[code], "NONE", 1, 0, 0, 0,
                         [], now, now],
                    )

            for index, held in STAFF_SKILLS.items():
                for code, level in held:
                    cur.execute(
                        """
                        INSERT INTO public.staff_skill_assignment
                            (staff_member_id, skill_id, tenant_id, level,
                             assigned_at)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (staff_member_id, skill_id) DO NOTHING
                        """,
                        [staff_id(index), tenant_skill_ids[code], TENANT_ID,
                         level, now],
                    )

            # ── Rosters, shifts and one booking ─────────────────────────
            # Two weeks from the Monday of the current week, so the booking
            # flow has something to offer today and for a fortnight after.
            today = datetime.now(timezone.utc).date()
            monday = today - timedelta(days=today.weekday())

            for week in range(SHIFT_WEEKS):
                week_start = monday + timedelta(weeks=week)
                roster = roster_id(week_start)
                cur.execute(
                    """
                    INSERT INTO public.shift_roster
                        (id, tenant_id, branch_id, week_start_date,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, branch_id, week_start_date)
                        DO NOTHING
                    """,
                    [roster, TENANT_ID, BRANCH_ID, week_start, now, now],
                )

                for index, (start, end, unpaid_break, day_off) in SHIFTS.items():
                    cur.execute(
                        """
                        INSERT INTO public.shift_roster_member
                            (roster_id, staff_member_id, tenant_id, sort_order,
                             created_at)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (roster_id, staff_member_id) DO NOTHING
                        """,
                        [roster, staff_id(index), TENANT_ID, index, now],
                    )

                    for offset in range(7):
                        day = week_start + timedelta(days=offset)
                        if day.weekday() == day_off:
                            continue
                        cur.execute(
                            """
                            INSERT INTO public.shift
                                (id, roster_id, staff_member_id, tenant_id,
                                 shift_date, start_time, end_time, break_time,
                                 shift_type, created_at, updated_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (roster_id, staff_member_id, shift_date)
                                DO NOTHING
                            """,
                            [shift_id(index, day), roster, staff_id(index),
                             TENANT_ID, day, start, end, unpaid_break,
                             "FIXED", now, now],
                        )

            # Sunday is closed (see HOURS above), so an appointment seeded onto
            # one would sit on a day that offers nothing and prove nothing.
            booked_day = today + timedelta(days=1)
            if booked_day.weekday() == 6:
                booked_day += timedelta(days=1)

            # start_at is `timestamp without time zone` holding UTC, so the
            # salon-local hour is converted here rather than written raw — the
            # same trap the reading side documents in selectors.booking_rows.
            local_start = datetime.combine(
                booked_day, time(hour=BOOKED_LOCAL_HOUR),
                tzinfo=zoneinfo.ZoneInfo(SALON_TIMEZONE),
            )
            starts_at = local_start.astimezone(timezone.utc).replace(tzinfo=None)
            ends_at = starts_at + timedelta(minutes=BOOKED_MINUTES)

            cur.execute(
                """
                INSERT INTO public.booking
                    (id, tenant_id, branch_id, staff_id, status,
                     start_at, end_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                [BOOKING_ID, TENANT_ID, BRANCH_ID,
                 staff_id(BOOKED_STAFF_INDEX), "CONFIRMED",
                 starts_at, ends_at, now, now],
            )
            cur.execute(
                """
                INSERT INTO public.booking_item
                    (id, booking_id, tenant_id, service_id, duration_minutes,
                     sort_order)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                [BOOKING_ITEM_ID, BOOKING_ID, TENANT_ID,
                 service_id(BOOKED_SERVICE_INDEX), BOOKED_MINUTES, 0],
            )

            # ONE package, inserted after the services it is built from exist.
            # highlights and color_gallery are text[] in Postgres while the
            # stale model calls them TextField; psycopg3 maps a Python list to
            # an array regardless, which is why passing lists works here.
            cur.execute(
                """
                INSERT INTO public.service_package
                    (id, tenant_id, name, description, price_minor, currency,
                     status, branch_scoped, current_version, highlights,
                     requires_coordinator, giftable, audience, color_theme,
                     color_gallery, scheduling, online_booking_enabled,
                     deposit_type, pricing_mode, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                [PKG_ID, TENANT_ID, "Groom and Go",
                 "Leave sharp, stay sharp.", PKG_PRICE_MINOR, "AED",
                 "PUBLISHED", False, 1,
                 ["Gentleman's Cut", "Hot Towel Shave", "Beard oil to take home"],
                 False, True, "MALE", "gold", [], "SEQUENTIAL", True,
                 "NONE", "FIXED", now, now],
            )

            for order, (svc_index, qty) in enumerate(PKG_ITEMS):
                cur.execute(
                    """
                    INSERT INTO public.service_package_item
                        (id, package_id, service_id, quantity, sort_order)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    [package_item_id(order), PKG_ID, service_id(svc_index),
                     qty, order],
                )

            # Once per package, NOT once per item: a package is available at a
            # branch or it is not, regardless of how many services it holds.
            cur.execute(
                """
                INSERT INTO public.service_package_branch_availability
                    (id, package_id, branch_id, available, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                [PKG_AVAIL_ID, PKG_ID, BRANCH_ID, True, now, now],
            )

            # A product carries no price of its own: the price lives on the
            # variant, so both rows are needed for a product to be sellable.
            for index, (name, ptype, price) in enumerate(PRODUCTS):
                cur.execute(
                    """
                    INSERT INTO public.product
                        (id, tenant_id, name, unit, type, status,
                         track_batch_expiry, image_url, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    [product_id(index), TENANT_ID, name, "bottle", ptype,
                     "ACTIVE", False,
                     f"https://picsum.photos/seed/prod{index}/400/400",
                     now, now],
                )

                cur.execute(
                    """
                    INSERT INTO public.product_variant
                        (id, tenant_id, product_id, name, sku, cost_minor,
                         sale_price_minor, currency, position,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    [variant_id(index), TENANT_ID, product_id(index),
                     "Default", f"SKU-{index:03d}", price // 2, price,
                     "AED", 0, now, now],
                )

        self.stdout.write(self.style.SUCCESS("Seeded salon."))
        self.stdout.write(f"  GET /api/v1/salon/{STOREFRONT_ID}")
        self.stdout.write(f"  GET /api/v1/salon/{STOREFRONT_ID}/services")
        self.stdout.write(f"  GET /api/v1/salon/{STOREFRONT_ID}/stylists")
        self.stdout.write(
            f"  GET /api/v1/salon/{STOREFRONT_ID}/stylists"
            f"?service_ids={service_id(0)},{service_id(1)}"
        )
        self.stdout.write(f"  GET /api/v1/salon/{STOREFRONT_ID}/packages")
        self.stdout.write(f"  GET /api/v1/salon/{STOREFRONT_ID}/products")

    def _purge(self, cur):
        """
        Delete in reverse dependency order.

        Children before parents throughout: variant before product, package
        items and availability before the package, the package before the
        services it references, availability before service, profile before
        account, service before category, and live_version_id cleared before
        the version row it points at.
        """
        for index in range(len(PRODUCTS)):
            cur.execute(
                "DELETE FROM public.product_variant WHERE id = %s", [variant_id(index)]
            )
            cur.execute(
                "DELETE FROM public.product WHERE id = %s", [product_id(index)]
            )

        cur.execute(
            "DELETE FROM public.service_package_branch_availability WHERE id = %s",
            [PKG_AVAIL_ID],
        )
        for order in range(len(PKG_ITEMS)):
            cur.execute(
                "DELETE FROM public.service_package_item WHERE id = %s",
                [package_item_id(order)],
            )
        cur.execute("DELETE FROM public.service_package WHERE id = %s", [PKG_ID])

        # Every roster and booking at this branch, not just the ids computed
        # for today: the seed writes them onto dates that move with the
        # calendar, so a re-seed a week later would otherwise leave last
        # week's rows behind. The branch is the seed's own creation, so
        # "everything here" is exactly what this command put here.
        cur.execute(
            """
            DELETE FROM public.shift WHERE roster_id IN (
                SELECT id FROM public.shift_roster WHERE branch_id = %s
            )
            """,
            [BRANCH_ID],
        )
        cur.execute(
            """
            DELETE FROM public.shift_roster_member WHERE roster_id IN (
                SELECT id FROM public.shift_roster WHERE branch_id = %s
            )
            """,
            [BRANCH_ID],
        )
        cur.execute(
            "DELETE FROM public.shift_roster WHERE branch_id = %s", [BRANCH_ID]
        )
        cur.execute(
            """
            DELETE FROM public.booking_item WHERE booking_id IN (
                SELECT id FROM public.booking WHERE branch_id = %s
            )
            """,
            [BRANCH_ID],
        )
        cur.execute("DELETE FROM public.booking WHERE branch_id = %s", [BRANCH_ID])

        # Skills before the staff and services that reference them. The
        # platform-wide catalog_skill rows are deliberately NOT deleted: they
        # are reference data shared by every tenant, like badges, and another
        # tenant's service stage may point at the very row this seed inserted.
        for index in STAFF_SKILLS:
            cur.execute(
                "DELETE FROM public.staff_skill_assignment WHERE staff_member_id = %s",
                [staff_id(index)],
            )
        for index in SERVICE_SKILLS:
            cur.execute(
                "DELETE FROM public.service_stage WHERE id = %s", [stage_id(index)]
            )
        for index in range(len(TENANT_SKILLS)):
            cur.execute("DELETE FROM public.skill WHERE id = %s", [skill_id(index)])
        cur.execute(
            "DELETE FROM public.skill_category WHERE id = %s", [SKILL_CATEGORY_ID]
        )

        for index in range(len(STAFF)):
            cur.execute(
                "DELETE FROM public.staff_profile WHERE id = %s", [staff_id(index)]
            )
            cur.execute(
                "DELETE FROM public.user_account WHERE id = %s", [user_id(index)]
            )

        for index in range(len(SERVICES)):
            cur.execute(
                "DELETE FROM public.service_branch_availability WHERE id = %s",
                [availability_id(index)],
            )
            cur.execute(
                "DELETE FROM public.service WHERE id = %s", [service_id(index)]
            )

        # Children first: CAT_CUTS and CAT_BEARD both reference CAT_HAIR, so
        # deleting the parent first would fail the foreign key. Sorting on
        # "has no parent" puts the children (False) ahead of the parent (True).
        for cat_id, _, _, parent_id in sorted(CATEGORIES, key=lambda c: c[3] is None):
            cur.execute("DELETE FROM public.category WHERE id = %s", [cat_id])

        cur.execute(
            "UPDATE public.storefront SET live_version_id = NULL WHERE id = %s",
            [STOREFRONT_ID],
        )
        cur.execute("DELETE FROM public.storefront_version WHERE id = %s", [VERSION_ID])
        cur.execute("DELETE FROM public.storefront WHERE id = %s", [STOREFRONT_ID])
        cur.execute("DELETE FROM public.branch WHERE id = %s", [BRANCH_ID])
        cur.execute("DELETE FROM public.tenant WHERE id = %s", [TENANT_ID])
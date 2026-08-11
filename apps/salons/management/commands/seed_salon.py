# apps/salons/management/commands/seed_salon.py
"""
Insert one complete salon for local development.

The profile endpoints read across storefront, branch, tenant, categories,
services, availability, staff, packages and a published version snapshot.
Testing them needs all of those to exist and agree with each other, which no
fixture file expresses readably.

WRITES TO PLATFORM-OWNED TABLES. Every table here is managed = False and
belongs to gostyle-platform. That is acceptable on a local database and
nowhere else, so the command refuses to run against a non-local host.
"""

import json
import uuid
from datetime import datetime, timezone

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

# (service index, quantity). Members are Gentleman's Cut (199.00, 30m) and
# Hot Towel Shave (260.00, 35m): 459.00 and 65 minutes bought separately,
# against a bundle price of 370.00. None of those three derived numbers is
# stored anywhere, so this package is the only proof the Sum subqueries in
# salon_packages() compute price_before, save_amount and duration correctly.
PKG_ITEMS = [(0, 1), (2, 1)]
PKG_PRICE_MINOR = 37000

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


def package_item_id(order):
    return uuid.UUID(f"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb{order}")


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
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    [service_id(index), TENANT_ID, name,
                     f"{name} at Iron Razor.", minutes, price, "AED",
                     "PUBLISHED", code, "MALE", cat_id, True, now, now],
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

        self.stdout.write(self.style.SUCCESS("Seeded salon."))
        self.stdout.write(f"  GET /api/v1/salon/{STOREFRONT_ID}")
        self.stdout.write(f"  GET /api/v1/salon/{STOREFRONT_ID}/services")
        self.stdout.write(f"  GET /api/v1/salon/{STOREFRONT_ID}/stylists")
        self.stdout.write(f"  GET /api/v1/salon/{STOREFRONT_ID}/packages")

    def _purge(self, cur):
        """
        Delete in reverse dependency order.

        Children before parents throughout: package items and availability
        before the package, the package before the services it references,
        availability before service, profile before account, service before
        category, and live_version_id cleared before the version it points at.
        """
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
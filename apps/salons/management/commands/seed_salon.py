# apps/salons/management/commands/seed_salon.py
"""
Insert one complete salon for local development.

The profile endpoint reads across storefront, branch, tenant, media, reviews,
policy and a published version snapshot. Testing it needs all of those to
exist and agree with each other, which no fixture file expresses readably.

WRITES TO PLATFORM-OWNED TABLES. Every table here is managed = False and
belongs to gostyle-platform. That is acceptable on a local database and
nowhere else, so the command refuses to run against a non-local host.
"""

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
                 __import__("json").dumps(SNAPSHOT), [], now, True],
            )

            # live_version_id is set AFTER the version exists, which is also
            # the order the platform's publish handler uses.
            cur.execute(
                "UPDATE public.storefront SET live_version_id = %s WHERE id = %s",
                [VERSION_ID, STOREFRONT_ID],
            )

        self.stdout.write(self.style.SUCCESS("Seeded salon."))
        self.stdout.write(f"  GET /api/v1/salon/{STOREFRONT_ID}")

    def _purge(self, cur):
        cur.execute("UPDATE public.storefront SET live_version_id = NULL WHERE id = %s", [STOREFRONT_ID])
        cur.execute("DELETE FROM public.storefront_version WHERE id = %s", [VERSION_ID])
        cur.execute("DELETE FROM public.storefront WHERE id = %s", [STOREFRONT_ID])
        cur.execute("DELETE FROM public.branch WHERE id = %s", [BRANCH_ID])
        cur.execute("DELETE FROM public.tenant WHERE id = %s", [TENANT_ID])
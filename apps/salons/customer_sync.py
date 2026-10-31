"""Links an app account to its row in the platform directory.

WRITES to public.customer, which the platform service owns. This is a
deliberate exception to the read-only rule the other platform_data models
follow: the booking path needs the customer to exist before booking-api
writes a booking that points at it.

The new row takes the ACCOUNT's id on purpose. booking-api writes
`actor.id` from the token as booking.customer_id, so matching the ids is
what makes the booking resolve.
"""

import logging

from django.db import IntegrityError, connection, transaction

from apps.accounts.privacy import name_for_salons

from .phone import to_e164

log = logging.getLogger(__name__)


class NoUsablePhone(Exception):
    pass


UPSERT = """
INSERT INTO public.customer (
    id, tenant_id, first_name, last_name, phone, email,
    country, city, discovery_source, created_at, updated_at
)
VALUES (
    %(id)s::uuid, %(tenant_id)s::uuid, %(first_name)s, %(last_name)s,
    %(phone)s, %(email)s,
    %(country)s::"Country", %(city)s, 'APP'::"DiscoverySource",
    now(), now()
)
ON CONFLICT (tenant_id, phone) WHERE deleted_at IS NULL
DO UPDATE SET
    first_name = COALESCE(NULLIF(EXCLUDED.first_name, ''), customer.first_name),
    last_name  = COALESCE(EXCLUDED.last_name, customer.last_name),
    email      = COALESCE(customer.email, EXCLUDED.email),
    updated_at = now()
RETURNING id
"""


def _one(sql, params):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


def _name_of(account):
    # "Show my name to salons" off: "Sara A." (apps/accounts/privacy.py).
    full = name_for_salons(account.full_name, account.show_name_to_salons)
    if not full:
        return "Guest", None
    first, _, last = full.partition(" ")
    return first, (last or None)


RENAME = """
UPDATE public.customer
   SET first_name = %(first_name)s, last_name = %(last_name)s, updated_at = now()
 WHERE id = %(id)s::uuid AND deleted_at IS NULL
"""


def refresh_salon_name(account):
    """Put the name "Show my name to salons" allows on the salon's record of
    this customer, after the switch changes.

    ONLY THE ROW THIS SERVICE MADE: the one whose id is the account's own
    (resolve_customer creates it so). A customer matched to a salon's
    walk-in record keeps that record as the salon wrote it: it is the
    salon's, not the app's. Returns how many rows changed (0 or 1).
    """
    first, last = _name_of(account)
    with connection.cursor() as cur:
        cur.execute(RENAME, {"id": account.pk, "first_name": first, "last_name": last})
        return cur.rowcount


def resolve_customer(account, tenant_id, *, city="Dubai", country="UAE"):
    """Return this account's platform customer id, creating the row if needed.

    Also caches the result on account.platform_customer_id.
    """
    if account.platform_customer_id:
        return account.platform_customer_id

    phone = to_e164(account.phone)
    if not phone:
        raise NoUsablePhone(f"account {account.pk}: phone is {account.phone!r}")

    first, last = _name_of(account)
    params = {
        "id": account.pk,
        "tenant_id": tenant_id,
        "first_name": first,
        "last_name": last,
        "phone": phone,
        "email": (account.email or "").strip().lower() or None,
        "country": country,
        "city": city,
    }

    try:
        with transaction.atomic():
            found = _one(UPSERT, params)
    except IntegrityError as exc:
        msg = str(exc)
        if "customer_tenant_id_email_live_key" in msg:
            # That email already belongs to someone else in this tenant.
            with transaction.atomic():
                found = _one(UPSERT, {**params, "email": None})
        elif "customer_pkey" in msg:
            found = account.pk
        else:
            raise

    if str(found) != str(account.pk):
        log.warning(
            "account %s matched existing customer %s (walk-in). "
            "booking-api still writes %s, so bookings need the header change.",
            account.pk, found, account.pk,
        )

    account.platform_customer_id = found
    account.save(update_fields=["platform_customer_id", "updated_at"])
    return found
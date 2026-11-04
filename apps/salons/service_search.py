"""
GET /services across salons (SEARCH_POPULAR_V1, docs/SEARCH_POPULAR_API.md):
the rules, from the mobile team's doc (docs/search-popular-fe-contract.md).

Pure: rows in, rows out, no queryset. The view (service_search_views.py)
reads the rows with the selectors; this decides which services are listed,
which the search finds, their order and each result's fields.

ONE RESULT PER SALON AND SERVICE. A service belongs to a tenant, and every
public salon (storefront) of that tenant sells it at its own branch's price,
so "Haircut" at two salons is two results (doc 2.4).
"""

import types
from collections import Counter
from datetime import timedelta

from .menu import OTHER_CHIP, category_chip, service_currency, service_row
from .selectors import BRANCH_AVAILABILITY_ENABLED
from .serializers import card_category

# Popular: the bookings of the last POPULAR_DAYS days (doc 2.3).
POPULAR_DAYS = 30

# The bookings that count: every booking a customer made and did not call off
# (doc 2.3: cancelled ones left out). booking-api's BOOKING_STATES that hold
# or held a chair (map_availability.BLOCKING_STATUSES), and no_show: it was
# booked. Not counted: cancelled, expired (never paid or confirmed), skipped
# (a routine's session the customer skipped), rescheduled (a routine's old
# session, moved to a new one), draft and held (a slot held, never booked).
COUNTED_STATUSES = (
    "pending_payment",
    "pending_confirmation",
    "confirmed",
    "checked_in",
    "in_service",
    "completed",
    "settled",
    "no_show",
)

# At most this many services of one salon on a page of is_popular, so one
# busy salon does not fill the row (doc 2.3).
PER_SALON = 2


def popular_since(now):
    """The first moment whose bookings count."""
    return now - timedelta(days=POPULAR_DAYS)


def chip_label(categories, category_id):
    """The label of the chip a service is filed under: its category.label."""
    return category_chip(categories, category_id)["label"]


def label_ids(categories, term):
    """
    The ids of the categories whose chip label holds `term`, case ignored:
    the services filed under them are found by their category (doc 2.5).
    """
    wanted = term.lower()
    return {cid for cid in categories if wanted in chip_label(categories, cid).lower()}


def finds_other(term):
    """True when `term` is in "Other", the chip of a service with no category."""
    return term.lower() in OTHER_CHIP["label"].lower()


def matches(row, categories, term):
    """
    True when the search finds this service: `term` anywhere in its name or
    in its category.label, case ignored (doc 2.5). Not the description, not
    the salon's name (salons are searched with /discover?search=).
    """
    wanted = term.lower()
    return wanted in row.service["name"].lower() or wanted in chip_label(
        categories, row.service["category_0_id"]
    ).lower()


def listing(salons, services, branch_rows):
    """
    One row per salon and service of its tenant: a namespace holding the
    `salon` and `service` dicts, `branch_price_minor`, the salon's own
    branch price (menu.service_price's rule), and `branch_currency`, that
    price's currency (None unless SERVICE_CURRENCY_V1 read it).

    `salons` are selectors.search_salons, `services` selectors.menu_services,
    `branch_rows` selectors.branch_service_rows.
    With BRANCH_AVAILABILITY_ENABLED (off, as on the Services tab) a service
    is left out at a branch without an `available` row, as the tab does.
    """
    by_tenant = {}
    for service in services:
        by_tenant.setdefault(service["tenant_id"], []).append(service)

    rows = []
    for salon in salons:
        for service in by_tenant.get(salon["tenant_id"], ()):
            own = branch_rows.get((service["id"], salon["branch_id"])) or {}
            if BRANCH_AVAILABILITY_ENABLED and own.get("available") is not True:
                continue
            rows.append(
                types.SimpleNamespace(
                    salon=salon,
                    service=service,
                    branch_price_minor=own.get("price_minor"),
                    branch_currency=own.get("currency"),
                )
            )
    return rows


def _rank_ids(row):
    """The last tie-break: one order every time, so the pages never shift."""
    return (str(row.salon["id"]), str(row.service["id"]))


def _newest(row):
    created = row.service["created_at"]
    return -created.timestamp() if created is not None else float("inf")


def newest_first(rows):
    """Without is_popular: the newest service first (its created_at, doc 2.3)."""
    return sorted(rows, key=lambda row: (_newest(row), *_rank_ids(row)))


def popular_first(rows, counts):
    """
    is_popular (doc 2.3): the most bookings of the service at that salon
    first (`counts`, selectors.service_booking_counts). Ties: the salon's
    rating, highest first, a salon nobody rated after every rated one; then
    the newest service.
    """

    def key(row):
        booked = counts.get((row.salon["branch_id"], row.service["id"]), 0)
        rating = row.salon["avg_rating"]
        return (
            -booked,
            rating is None,
            -float(rating) if rating is not None else 0.0,
            _newest(row),
            *_rank_ids(row),
        )

    return sorted(rows, key=key)


def spread(rows, page_size, pages, per_salon=PER_SALON):
    """
    `rows` in their order, with each of the first `pages` pages of
    `page_size` holding at most `per_salon` services of one salon (doc 2.3).

    Page by page: the rows in order, skipping a salon's row once it has
    `per_salon` on the page; the skipped rows go on to the next page, ahead
    of the rows after them. A page still short (too few other salons) is
    filled with the skipped rows in order, so every page but the last is
    full and `count` stays right. Each page depends only on the pages before
    it, so page 2 of the same call is the same whichever page was asked
    first. The rows after the last spread page keep their order.
    """
    out, pending = [], list(rows)
    for _ in range(pages):
        if not pending:
            break
        page, skipped, taken, rest = [], [], Counter(), []
        for index, row in enumerate(pending):
            if len(page) == page_size:
                rest = pending[index:]
                break
            salon_id = row.salon["id"]
            if taken[salon_id] < per_salon:
                page.append(row)
                taken[salon_id] += 1
            else:
                skipped.append(row)
        short = page_size - len(page)
        page.extend(skipped[:short])
        out.extend(page)
        pending = skipped[short:] + rest
    return out + pending


def _after(fields, key, new_key, value):
    """`fields` with `new_key` placed right after `key`, the rest in their order."""
    out = {}
    for name, held in fields.items():
        out[name] = held
        if name == key:
            out[new_key] = value
    return out


def result(row, categories, with_currency=False):
    """
    One result, the doc's 2.2 fields: the service as on the salon's Services
    tab (menu.service_row: the same price and durations), its category as on
    the Service Detail screen (the tab's chip, { id, label }), and the salon.

    With `with_currency` (SERVICE_CURRENCY_V1), `currency` follows `price`:
    the currency that price is in, by menu.service_currency's rule (the
    branch's own when set, else the service's). The rows must then carry
    it (selectors.menu_services and branch_service_rows, with_currency).
    """
    service = row.service
    svc = types.SimpleNamespace(
        id=service["id"],
        name=service["name"],
        description=service["description"],
        price_minor=service["price_minor"],
        branch_price_minor=row.branch_price_minor,
        duration_minutes=service["duration_minutes"],
        currency=service.get("currency"),
        branch_currency=getattr(row, "branch_currency", None),
    )
    chip = category_chip(categories, service["category_0_id"])
    salon = row.salon
    shown = service_row(svc)
    if with_currency:
        shown = _after(shown, "price", "currency", service_currency(svc))
    return {
        **shown,
        "category": {"id": chip["id"], "label": chip["label"]},
        "salon": {
            "id": str(salon["id"]),
            # The card's name and category (SalonCardSerializer), so the
            # search's two lists name a salon the same way.
            "name": salon["published_name"] or salon["branch_name"],
            "category": card_category(salon["category"]),
        },
    }

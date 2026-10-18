"""
What the Services tab, the Service Detail screen and the Expert Profile
screen must agree on: a service's price, its row, the group and the chip it
is filed under, and whether it is on the menu. And the platform's branch
rule for a service, which booking-api sells packages by (packages.can_book).

One place for each, so the screens cannot drift
(docs/SERVICE_DETAIL_AUDIT.md, 2.1, 2.4 and 3.1; docs/EXPERT_PROFILE_AUDIT.md,
2.2). Pure: rows in, values out.
"""

from .money import major

# The chip for a service with no category, or one whose category was deleted.
OTHER_CHIP = {"id": "other", "label": "Other", "icon": None}


def service_price(svc):
    """
    One visit's price, before VAT, in major units (a Decimal).

    `svc` is a service row: a model instance, or a dict from `.values()`.

    booking-api's rule, exactly (the platform's gRPC ListServices, which is
    what booking-api prices from: `override?.priceMinor ?? s.priceMinor`):
    the salon's own branch price when one is set, so 0 means 0; no branch
    row, or a branch row with no price, means the service's own
    `price_minor`. The selectors read `branch_price_minor` for the salon's
    branch (`selectors.branch_price_minor`). Any other number here is an
    amount_mismatch when the customer books.
    """
    return major(service_price_minor(svc))


def service_price_minor(svc):
    """`service_price` in minor units, for code that adds prices up (packages.py)."""
    if isinstance(svc, dict):
        base, branch = svc.get("price_minor"), svc.get("branch_price_minor")
    else:
        base, branch = svc.price_minor, getattr(svc, "branch_price_minor", None)
    return branch if branch is not None else base


def service_currency(svc):
    """
    The currency `service_price_minor` is in, by the same rule: the
    platform's ListServices sends `override?.currency ?? s.currency`, so the
    branch row's currency when it has one, else the service's own. A row's
    price and its currency fall back separately, as they do there.

    `svc` as for `service_price_minor`, with `currency` and
    `branch_currency` (selectors.package_service_rows reads both).
    """
    if isinstance(svc, dict):
        base, branch = svc.get("currency"), svc.get("branch_currency")
    else:
        base, branch = getattr(svc, "currency", None), getattr(svc, "branch_currency", None)
    return branch if branch is not None else base


def category_chip(categories, category_id):
    """
    `{id, label, icon}` of the chip a service is filed under on the tab: its
    category's PARENT when it has one, else the category itself, else
    OTHER_CHIP (no category, or a deleted one).

    `categories` is `selectors.salon_categories` (id to row). A parent that
    was deleted is not in it, so the category is its own chip.
    """
    cat = categories.get(category_id)
    if cat is None:
        return dict(OTHER_CHIP)

    parent = categories.get(cat["parent_id"]) if cat["parent_id"] else None
    chip = parent or cat
    return {"id": str(chip["id"]), "label": chip["name_en"], "icon": chip.get("icon")}


def service_row(svc):
    """
    One service as a row of the Services tab. The Expert Profile's
    `service_groups` sends the same rows, so one renderer serves both.
    """
    # duration_min and duration_max are the SAME number. A real range needs
    # service_variant rows with differing durations; until the app reads
    # those, sending one value twice is honest and lets the app collapse
    # "20 - 20 mins" to "20 mins" itself.
    return {
        "id": str(svc.id),
        "name": svc.name,
        "description": svc.description,
        # service_price, shared with the Service Detail screen: every screen
        # must show the same number.
        "price": service_price(svc),
        "duration_min": svc.duration_minutes,
        "duration_max": svc.duration_minutes,
    }


def service_groups(services, categories):
    """
    The Services tab's grouping, as `(chips, groups)`.

    `services` in the tab's order (`selectors.salon_services`, by name);
    `categories` is `selectors.salon_categories` (id to row).

    groups: one per category a service is filed under, in the order each is
    first seen, as `{id, category_id, name, services}`; inside a group the
    services keep their order. `category_id` is the group's chip.

    chips: the chip of each group (`category_chip`: the parent, else the
    category itself), each once, in the order first seen.
    """
    # category_0_id, not category_id: the service table has a legacy text
    # column already named `category`, so inspectdb renamed the real foreign
    # key rather than colliding with it.
    #
    # A category that is not in `categories` (deleted) files the service
    # under None, with the services that have no category: both are "Other",
    # and two groups with the same id "other" would draw twice.
    grouped = {}
    for svc in services:
        cat_id = svc.category_0_id if svc.category_0_id in categories else None
        grouped.setdefault(cat_id, []).append(svc)

    chips = {}
    groups = []

    for cat_id, svcs in grouped.items():
        # A service with no category, or one pointing at a deleted row, still
        # has to appear: dropping it would silently hide a bookable service
        # from the menu.
        cat = categories.get(cat_id)

        chip = category_chip(categories, cat_id)
        chips[chip["id"]] = chip

        groups.append({
            "id": str(cat["id"]) if cat else OTHER_CHIP["id"],
            "category_id": chip["id"],
            "name": cat["name_en"] if cat else OTHER_CHIP["label"],
            "services": [service_row(s) for s in svcs],
        })

    return list(chips.values()), groups


def is_on_menu(svc):
    """
    True when the service is on the salon's menu today: exactly the three
    rules `selectors.salon_services` filters the tab on.

    `svc` is a model row or a dict from `.values()`, like `service_price`.
    """
    if isinstance(svc, dict):
        status, deleted_at, online = (
            svc.get("status"), svc.get("deleted_at"), svc.get("online_booking_enabled")
        )
    else:
        status, deleted_at, online = svc.status, svc.deleted_at, svc.online_booking_enabled
    return status == "PUBLISHED" and deleted_at is None and bool(online)


def offered_at_branch(svc):
    """
    True when the salon's branch offers the service, by the platform's own
    rule, isServiceAvailableAtBranch (gostyle-platform,
    packages/nest-service-menu/src/domain/services/branch-visibility.ts).
    Its ListServices and ListPackages sell by it, so booking-api books by
    it. In its order:

      1. this branch's own row for the service decides, either way;
      2. else a `branch_scoped` service is not offered: only a row turns
         one on;
      3. else this branch's row for the service's category decides (none
         for a service with no category);
      4. else it is offered.

    `svc` is a dict from `.values()`, or a model row annotated the same:
    `branch_available` and `category_branch_available` are this branch's
    rows (True, False, or None for no row), as
    selectors.package_service_rows reads them.

    Not the filter BRANCH_AVAILABILITY_ENABLED puts on the Services tab,
    which is still off and keeps only services WITH a true row
    (docs/SERVICE_DETAIL_AUDIT.md, F2).
    """
    if isinstance(svc, dict):
        get = svc.get
    else:
        def get(name):
            return getattr(svc, name, None)

    own = get("branch_available")
    if own is not None:
        return bool(own)
    if get("branch_scoped"):
        return False
    category = get("category_branch_available")
    if get("category_0_id") is not None and category is not None:
        return bool(category)
    return True

"""
What the Services tab and the Service Detail screen must agree on: a
service's price, the chip it is filed under, and whether it is on the menu.

One place for each, so the two screens cannot drift
(docs/SERVICE_DETAIL_AUDIT.md, 2.1, 2.4 and 3.1). Pure: rows in, values out.
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
    if isinstance(svc, dict):
        base, branch = svc.get("price_minor"), svc.get("branch_price_minor")
    else:
        base, branch = svc.price_minor, getattr(svc, "branch_price_minor", None)
    return major(branch if branch is not None else base)


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


def is_on_menu(svc):
    """
    True when the service is on the salon's menu today: exactly the three
    rules `selectors.salon_services` filters the tab on.
    """
    return (
        svc.status == "PUBLISHED"
        and svc.deleted_at is None
        and bool(svc.online_booking_enabled)
    )

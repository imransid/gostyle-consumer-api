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

    The branch price when the selector read one (`branch_price_minor`, only
    while selectors.BRANCH_AVAILABILITY_ENABLED is on), else the service's
    own `price_minor`. It must be the number booking-api charges, or the
    booking create answers amount_mismatch.
    """
    return major(getattr(svc, "branch_price_minor", None) or svc.price_minor)


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

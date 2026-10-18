"""
What the Packages tab and the Package Details screen must agree on: which
packages are on sale at a salon, what a package holds, what it costs and
what it saves (docs/PACKAGE_DETAIL_AUDIT.md), and whether it can be booked
as one visit at its price (`can_book`, docs/BOOKING_CREATE_API.md).

Pure: rows in, values out. The rows come from selectors.py
(`package_for_salon`, `live_salon_packages`, `package_lines`).

The platform's own rules, read from its code (gostyle-platform,
packages/nest-service-menu), so the app shows what the salon's live menu
shows:

  * The package row is the DRAFT. Publishing freezes the name, description,
    price and items into `published_snapshot`. The live menu reads those
    from the snapshot and everything else (theme, policy, scheduling, online
    booking, branch rules) from the row (`toPublishedDomain`). A package
    with no snapshot is read from the row and its items.
  * A package is sold at every branch unless that branch's row says false;
    a `branch_scoped` one only where that branch's row says true
    (`listForBranch`).
  * An item's price and minutes are its variant's when it names a live one,
    else its service's (`subtotalForItems`). A service's price is this
    branch's (menu.service_price_minor), as on the Services tab.
"""

import uuid

from .menu import is_on_menu, offered_at_branch, service_price_minor
from .money import major

PARALLEL = "PARALLEL"


def sold_here(pkg):
    """
    The platform's branch rule, for the salon's branch. `pkg.branch_row` is
    that branch's `service_package_branch_availability.available`, or None
    when there is no row.
    """
    if pkg.branch_scoped:
        return pkg.branch_row is True
    return pkg.branch_row is not False


def on_sale(pkg):
    """On sale at this salon now, before its services are looked at."""
    return (
        pkg.status == "PUBLISHED"
        and pkg.deleted_at is None
        and bool(pkg.online_booking_enabled)
        and sold_here(pkg)
    )


def text_or_none(value):
    """Free text as the salon typed it, trimmed. Blank, or not text, is None."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def snapshot(pkg):
    """The published snapshot, or None when the package was never published."""
    snap = pkg.published_snapshot
    return snap if isinstance(snap, dict) else None


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _quantity(value):
    # The platform defaults to 1 and never stores less.
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 1


def _snapshot_item(item):
    # An item that cannot be read keeps service_id None: it matches no
    # service, so the package is not bookable (see `lines`). Never dropped
    # quietly, which would show a smaller package as if it were whole.
    if not isinstance(item, dict):
        return {"service_id": None, "variant_id": None, "quantity": 1}
    variant = item.get("variantId")
    return {
        "service_id": _uuid(item.get("serviceId")),
        "variant_id": _uuid(variant) if variant else None,
        "quantity": _quantity(item.get("quantity")),
    }


def live_content(pkg, draft_items=()):
    """
    `{name, description, price_minor, items}` as the customer sees them:
    from the published snapshot, or from the row and `draft_items`
    (`selectors.package_item_rows`) when there is none.

    Each item is `{service_id, variant_id, quantity}`, in the package's own
    order.
    """
    snap = snapshot(pkg)
    if snap is None:
        return {
            "name": pkg.name,
            "description": text_or_none(pkg.description),
            "price_minor": pkg.price_minor,
            "items": [
                {
                    "service_id": item["service_id"],
                    "variant_id": item["variant_id"],
                    "quantity": _quantity(item["quantity"]),
                }
                for item in draft_items
            ],
        }

    price = snap.get("priceMinor")
    if not isinstance(price, int) or isinstance(price, bool):
        price = pkg.price_minor
    items = snap.get("items")
    return {
        "name": text_or_none(snap.get("name")) or pkg.name,
        "description": text_or_none(snap.get("description")),
        "price_minor": price,
        "items": [_snapshot_item(i) for i in items] if isinstance(items, list) else [],
    }


def lines(items, services, variants):
    """
    The items with what each costs and takes here, as `(lines, complete)`.

    `services` is `selectors.package_service_rows` (id to row: this tenant's
    only, in whatever state they are in now). `variants` is
    `selectors.package_variant_rows` (id to row: live ones only).

    Each line: `{service_id, name, description, unit_minor, minutes,
    quantity, bookable, offered_here}`. `bookable` is the Services tab's own
    rule (menu.is_on_menu); `offered_here` is the platform's branch rule
    (menu.offered_at_branch), which only `can_book` reads.

    An item whose service is not there (another tenant's, removed, an id
    that could not be read) cannot be shown: it is left out and `complete`
    is False. A variant that was deleted, or is not this service's, is
    ignored, and the service's own price and minutes are used, as the
    platform does.
    """
    out, complete = [], True
    for item in items:
        svc = services.get(item["service_id"]) if item["service_id"] else None
        if svc is None:
            complete = False
            continue

        variant = variants.get(item["variant_id"]) if item["variant_id"] else None
        if variant is not None and variant["service_id"] != svc["id"]:
            variant = None

        if variant is None:
            unit, minutes = service_price_minor(svc), svc["duration_minutes"]
        else:
            unit = variant["price_minor"]
            minutes = variant["duration_minutes"]
            if minutes is None:
                minutes = svc["duration_minutes"]

        out.append({
            "service_id": svc["id"],
            "name": svc["name"],
            "description": svc.get("description"),
            "unit_minor": unit,
            "minutes": minutes,
            "quantity": item["quantity"],
            "bookable": is_on_menu(svc),
            "offered_here": offered_at_branch(svc),
        })
    return out, complete


def is_active(pkg, package_lines, complete):
    """
    Can a customer book it here now: on sale (`on_sale`), holding at least
    one service, and every service in it found and on the menu (their §3:
    every service_id must be bookable at that salon).
    """
    return (
        on_sale(pkg)
        and complete
        and bool(package_lines)
        and all(line["bookable"] for line in package_lines)
    )


def pinned_to_variant(items):
    """
    True when any item names a service variant ("Long hair"), live or not.
    booking-api books services, not their variants, so it does not sell such
    a package: it would hold the base service's minutes for a longer visit.
    """
    return any(item["variant_id"] for item in items)


def _priced(minor):
    """A price booking-api will charge: a whole number of minor units above 0."""
    return isinstance(minor, int) and not isinstance(minor, bool) and minor > 0


def can_book(pkg, content, package_lines, complete):
    """
    Can it be booked here now, as one visit at its price
    (PACKAGE_BOOKING_V1, docs/BOOKING_CREATE_API.md): booking-api's rule
    for selling a package (gostyle-booking-api,
    platform-package-catalogue.ts, over the platform's ListPackages), on the
    content the customer is shown (`live_content`).

    `is_active` as it is, and four things it does not look at: every
    service offered at this branch by the platform's branch rule
    (`offered_here`), no item pinned to a variant, a price above 0, and
    every service in it priced above 0 at this branch. A package dearer
    than its services is still bookable: booking-api sells it at its price,
    saving nothing.

    A SERVICE PRICED AT NOTHING SPOILS THE WHOLE PACKAGE. booking-api loads
    a package's services to price and time them, and its catalogue refuses
    the whole load for one whose price here is not above 0
    (platform-service-catalogue.ts: 409 BOOKING_STATE_INVALID, "That
    service has no usable price in the catalogue."), on availability, hold
    and create alike. `unit_minor` is that price: this branch's when the
    salon set one, 0 included, else the service's own, as ListServices
    sends it (menu.service_price_minor). Every line here names no variant,
    or the package is refused already.
    """
    return (
        is_active(pkg, package_lines, complete)
        and all(line["offered_here"] for line in package_lines)
        and not pinned_to_variant(content["items"])
        and _priced(content["price_minor"])
        and all(_priced(line["unit_minor"]) for line in package_lines)
    )


def saving(price_minor, package_lines):
    """
    `{price_before, save_amount, save_percent}`.

    `price_before` is the services at their prices here, each times its
    quantity: what they cost booked separately. So `save_amount` is always
    `price_before - price`. All three are None when that is not more than
    the price (no saving: no chip, no strikethrough, no saving card), or
    when there is nothing to add up.

    `save_percent` is a whole number, rounded half up, and at least 1, so
    the chip never says "SAVE 0%".
    """
    before = sum(line["unit_minor"] * line["quantity"] for line in package_lines)
    save = before - price_minor
    if not package_lines or save <= 0:
        return {"price_before": None, "save_amount": None, "save_percent": None}
    percent = max(1, (save * 200 + before) // (before * 2))
    return {
        "price_before": major(before),
        "save_amount": major(save),
        "save_percent": percent,
    }


def duration_minutes(package_lines):
    """
    The services' minutes added up, each times its quantity: the Packages
    tab's number and the platform's. None when there is nothing to add up.
    """
    if not package_lines:
        return None
    return sum((line["minutes"] or 0) * line["quantity"] for line in package_lines)


def parts(package_lines):
    """
    The services one booking of the package performs, in its order: each
    line `quantity` times, so "2 x Cut" is two cuts. booking-api expands a
    package exactly so (platform-package-catalogue.ts), and books a visit
    that long.
    """
    return [line["service_id"] for line in package_lines for _ in range(line["quantity"])]


def includes(package_lines):
    """What's Included: one row per service, `price` for ONE of it, here."""
    return [
        {
            "service_id": str(line["service_id"]),
            "name": line["name"],
            "price": major(line["unit_minor"]),
            "quantity": line["quantity"],
        }
        for line in package_lines
    ]


def tab_services(package_lines):
    """
    A Packages tab bundle's own services: one row per line, in the
    package's order, in the Services tab's row shape plus `quantity`.
    `price` is ONE of it at this branch's price and the minutes are one of
    it, both the line's own, so they agree with `includes` and `timeline`.
    """
    return [
        {
            "id": str(line["service_id"]),
            "name": line["name"],
            "description": line["description"],
            "price": major(line["unit_minor"]),
            "duration_min": line["minutes"],
            "duration_max": line["minutes"],
            "quantity": line["quantity"],
        }
        for line in package_lines
    ]


def timeline(package_lines, scheduling):
    """
    The visit, one step per service in the package's order.

    SEQUENTIAL (the platform's default): each step starts when the steps
    before it end. PARALLEL: every step starts at 0. A quantity above 1 is
    still one step: a timeline is one visit.
    """
    parallel = scheduling == PARALLEL
    steps, at = [], 0
    for line in package_lines:
        minutes = line["minutes"] or 0
        steps.append({
            "service_id": str(line["service_id"]),
            "name": line["name"],
            "offset_minutes": 0 if parallel else at,
            "duration_minutes": minutes,
        })
        if not parallel:
            at += minutes
    return steps


def detail(salon, pkg, content, package_lines, complete):
    """The Package Details answer, in the contract's order, `theme` last."""
    return {
        "id": str(pkg.id),
        "salon_id": str(salon.id),
        "name": content["name"],
        "description": content["description"],
        "price": major(content["price_minor"]),
        **saving(content["price_minor"], package_lines),
        "duration_minutes": duration_minutes(package_lines),
        "is_active": is_active(pkg, package_lines, complete),
        "includes": includes(package_lines),
        "timeline": timeline(package_lines, pkg.scheduling),
        "terms": text_or_none(pkg.cancellation_policy),
        "theme": pkg.color_theme,
    }


def tab_row(pkg, content, package_lines):
    """
    One bundle on the Packages tab (PACKAGES_LIVE_RULES_V1 on): the tab's
    keys, from the same numbers the detail screen shows, and its own
    services (`tab_services`).
    """
    money = saving(content["price_minor"], package_lines)
    return {
        "id": str(pkg.id),
        "name": content["name"],
        "description": content["description"],
        "features": list(pkg.highlights) if isinstance(pkg.highlights, (list, tuple)) else [],
        "duration_minutes": duration_minutes(package_lines),
        "price": major(content["price_minor"]),
        "price_before": money["price_before"],
        "save_amount": money["save_amount"],
        "theme": pkg.color_theme,
        "services": tab_services(package_lines),
    }

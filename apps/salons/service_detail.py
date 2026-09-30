"""
The Service Detail screen's answer, built from rows the view has read.

Pure: rows in, dicts out, no queryset. The view (service_detail_views.py)
reads; this writes the contract's fields (docs/service-detail-fe-contract.md
§2). Built step by step (docs/SERVICE_DETAIL_AUDIT.md, section 7): the core
fields in S2; photos, content, experts and the rest in S3 to S6.
"""

from .menu import category_chip, is_on_menu, service_price


def core_fields(salon, service, categories):
    """
    id, salon_id, name, description, price, duration_min, duration_max,
    category and is_active: the same figures the Services tab shows.

    `categories` is `selectors.salon_categories` for the salon's tenant.
    """
    chip = category_chip(categories, service.category_0_id)
    return {
        "id": str(service.id),
        "salon_id": str(salon.id),
        "name": service.name,
        "description": service.description,
        # menu.service_price, the tab's own helper: a price here that differs
        # from the tab's, or from booking-api's, is an amount_mismatch.
        "price": service_price(service),
        # One duration is all the platform has, and booking-api books exactly
        # it. The same number twice is the contract's "fixed length".
        "duration_min": service.duration_minutes,
        "duration_max": service.duration_minutes,
        # The tab's chip, without its icon: the contract's { id, label }.
        # No category (or a deleted one) is the tab's "other", never null.
        "category": {"id": chip["id"], "label": chip["label"]},
        # The tab's three rules. False still answers 200: the app hides Book.
        "is_active": is_on_menu(service),
    }

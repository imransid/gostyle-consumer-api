"""
The Service Detail screen's answer, built from rows the view has read.

Pure: rows in, dicts out, no queryset. The view (service_detail_views.py)
reads; this writes the contract's fields (docs/service-detail-fe-contract.md
§2). Built step by step (docs/SERVICE_DETAIL_AUDIT.md, section 7): the core
fields in S2; photos, content, experts and the rest in S3 to S6.
"""

from .menu import category_chip, is_on_menu, service_price

# The app shows four thumbs and a fifth behind "+N" (contract §3).
GALLERY_MAX = 5


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


def photos(own, linked):
    """
    hero_url, gallery and gallery_count, from the two lists
    `selectors.service_photo_urls` reads.

    The service's own photos first, then the salon's gallery photos linked to
    it, each list in its own order. A URL already in the list is left out
    (the same picture twice would show as two thumbs and count twice), and
    so is a blank one. The hero is the first photo and stays in the gallery;
    the gallery is the first GALLERY_MAX, and gallery_count counts them all,
    so the app's "+N" is gallery_count - len(gallery).
    """
    urls, seen = [], set()
    for url in [*own, *linked]:
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    return {
        "hero_url": urls[0] if urls else None,
        "gallery": urls[:GALLERY_MAX],
        "gallery_count": len(urls),
    }

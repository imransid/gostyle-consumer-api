"""
The Service Detail screen's answer, built from rows the view has read.

Pure: rows in, dicts out, no queryset. The view (service_detail_views.py)
reads; this writes the contract's fields (docs/service-detail-fe-contract.md
§2). Built step by step (docs/SERVICE_DETAIL_AUDIT.md, section 7): the core
fields in S2; photos, content, experts and the rest in S3 to S6.
"""

import re

from .menu import category_chip, is_on_menu, service_price

# The app shows four thumbs and a fifth behind "+N" (contract §3).
GALLERY_MAX = 5

# service.audience (the platform's four values) as the "Suitable for" row.
# Rafa, Q9. Any other value gets no row.
SUITABLE_FOR = {"MALE": "Men", "FEMALE": "Women", "UNISEX": "Everyone", "KIDS": "Kids"}

# A bullet mark or a number the salon typed at the start of a care line:
# "- ", "* ", "\u2022 ", "1. ", "12) ". The app draws its own
# bullets, so these would show twice. A mark needs a space after it (or
# nothing: a line that is only a mark is empty), so "1.5 hours" and "-10%"
# stay as they are.
_TYPED_BULLET = re.compile(r"^(?:[-*\u2022]|\d{1,3}[.)])(?:\s+|$)")


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


def content(service, stages):
    """
    included, details and preparation: what the visit includes, the Key
    Details rows and the Preparation & Aftercare bullets.

    `stages` is `selectors.service_stages` (stage order, with `name_en`).
    """
    return {
        "included": included(stages, service.name),
        "details": details(service),
        "preparation": preparation(
            service.pre_care_instructions, service.post_care_instructions
        ),
    }


def included(stages, service_name=None):
    """
    The "What's Included" chips: the stage names, in stage order.

    The platform fills booking-api's `included_steps` the same way. A blank
    name is left out; a name already there (case and spacing ignored) is
    shown once, as first written. A stage named like the service itself is
    left out too (Rafa, S4): a one-stage service is often built that way, and
    the chip would only repeat the title above it. [] hides the block.
    """
    names, seen = [], {_name_key(service_name)}
    for stage in stages:
        name = " ".join((stage.get("name_en") or "").split())
        key = _name_key(name)
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def _name_key(name):
    """A name as `included` compares it: spacing tidied, case ignored."""
    return " ".join((name or "").split()).casefold()


def preparation(pre_care, post_care):
    """
    The "Preparation & Aftercare" bullets: the pre-care lines, then the
    post-care lines, one bullet per non-empty line, with any bullet mark or
    number the salon typed removed. [] hides the block.
    """
    bullets = []
    for text in (pre_care, post_care):
        for line in (text or "").splitlines():
            line = _TYPED_BULLET.sub("", line.strip()).strip()
            if line:
                bullets.append(line)
    return bullets


def details(service):
    """
    The "Key Details" rows, in this order (Rafa, Q9): Time Duration (always),
    Suitable for, then only when set: Consultation, Patch test, Minimum age.
    Values are short and written for a customer. `icon` is one of the app's
    hints: clock, scissors, sparkles, drop.
    """
    rows = []

    minutes = service.duration_minutes
    # Always there: the platform refuses a service without a positive
    # duration. The guard only keeps a broken row from printing "0 min".
    if isinstance(minutes, int) and minutes > 0:
        rows.append(_row("Time Duration", duration_text(minutes), "clock"))

    audience = SUITABLE_FOR.get(service.audience)
    if audience:
        rows.append(_row("Suitable for", audience, "scissors"))

    if service.requires_consultation:
        rows.append(_row("Consultation", "Needed before this service.", "sparkles"))

    if service.requires_patch_test:
        hours = service.patch_test_hours
        if hours:
            unit = "hour" if hours == 1 else "hours"
            value = f"An allergy test, at least {hours} {unit} before your visit."
        else:
            value = "An allergy test, before your visit."
        rows.append(_row("Patch test", value, "drop"))

    # 0 is the platform's "no minimum".
    if service.min_age:
        rows.append(_row("Minimum age", f"{service.min_age} years", "scissors"))

    return rows


def duration_text(minutes):
    """45 -> "45 min", 60 -> "1 hr", 90 -> "1 hr 30 min"."""
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} min"
    if not rest:
        return f"{hours} hr"
    return f"{hours} hr {rest} min"


def _row(label, value, icon):
    return {"label": label, "value": value, "icon": icon}

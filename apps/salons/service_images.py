"""
The photos of services on the app's lists of services, one rule for every
list (SERVICE_IMAGES_V1, docs/SERVICE_IMAGES_API.md).

The rule is Service Detail's (selectors.service_photo_urls and
service_detail.photos), so a list's first photo is that screen's hero:

  1. the service's own photos (service_media, the ones the salon uploads in
     the service editor): primary first, then sort order, then oldest. Not
     deleted. They belong to the service's business (tenant).
  2. then THIS salon's gallery photos the salon tagged with the service
     (storefront_media.linked_service_id): kind GALLERY, public, approved,
     not deleted, in sort order. Never another branch's.

A blank URL, or one already in the list, is left out. At most IMAGES.

Two queries for a whole page of rows, however many salons and services are
on it; none for an empty page. GET /search/services (search_results) reads
its `images` here too.
"""

from django.conf import settings

from apps.platform_data.models import ServiceMedia, StorefrontMedia

# At most this many photos on a row (the Figma's collage on the search
# results; a list row shows the first).
IMAGES = 4


def is_on():
    """SERVICE_IMAGES_V1: the lists that had no photos send `images`."""
    return settings.SERVICE_IMAGES_V1


def merge(own, linked):
    """
    One row's `images`: own first, then linked, blank and repeated URLs left
    out (the same picture twice would show twice), at most IMAGES.
    """
    urls, seen = [], set()
    for url in [*own, *linked]:
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls[:IMAGES]


def images_for(wanted):
    """
    {(salon id, service id): images} for every `(salon id, tenant id,
    service id)` in `wanted`, `[]` for one with no photo.

    The ids as they come (UUIDs): the keys are the same values.
    """
    wanted = list(wanted)
    if not wanted:
        return {}
    service_ids = {service_id for _, _, service_id in wanted}
    salon_ids = {salon_id for salon_id, _, _ in wanted}

    own = {}
    for service_id, tenant_id, url in (
        ServiceMedia.objects.filter(service_id__in=service_ids, deleted_at__isnull=True)
        .order_by("-is_primary", "sort_order", "created_at", "id")
        .values_list("service_id", "tenant_id", "url")
    ):
        own.setdefault((tenant_id, service_id), []).append(url)

    linked = {}
    for salon_id, service_id, url in (
        StorefrontMedia.objects.filter(
            storefront_id__in=salon_ids,
            linked_service_id__in=service_ids,
            kind="GALLERY",
            is_public=True,
            moderation_status="APPROVED",
            deleted_at__isnull=True,
        )
        .order_by("sort_order", "created_at", "id")
        .values_list("storefront_id", "linked_service_id", "url")
    ):
        linked.setdefault((salon_id, service_id), []).append(url)

    return {
        (salon_id, service_id): merge(
            own.get((tenant_id, service_id), []), linked.get((salon_id, service_id), [])
        )
        for salon_id, tenant_id, service_id in wanted
    }


def for_rows(rows):
    """
    {(salon id, service id): images} for a page of the searches' rows
    (service_search.listing: `row.salon` and `row.service` dicts): GET
    /services across salons and GET /search/services.
    """
    return images_for(
        (row.salon["id"], row.salon["tenant_id"], row.service["id"]) for row in rows
    )


def for_salon(salon, service_ids):
    """
    {service id: images} for services of ONE salon (a storefront row with
    `id` and `tenant_id`): the Services tab, the Expert Profile and the
    Packages tab.
    """
    found = images_for((salon.id, salon.tenant_id, service_id) for service_id in service_ids)
    return {service_id: urls for (_, service_id), urls in found.items()}

"""
The photo on each bundle of the Packages tab (PACKAGE_IMAGE_V1,
docs/PACKAGE_IMAGES_API.md): `image_url`.

A bundle's photos are the package's own (service_package_media), the ones
the salon uploads to the package on the platform (POST
v1/catalog/packages/:packageId/media). The platform lists them primary
first, then in the order the salon set (PackageMediaService.list): the first
upload is the primary, and a reorder makes the new first one primary. So
`image_url` is the first of that list: primary first, then sort order, then
oldest, then the id, so the same photo wins every time. Not deleted, this
business's own, never a blank URL. None when the package has no photo.

One query for the whole tab, none when it lists no bundle.
"""

from django.conf import settings

from apps.platform_data.models import ServicePackageMedia


def is_on():
    """PACKAGE_IMAGE_V1: every bundle on the Packages tab sends `image_url`."""
    return settings.PACKAGE_IMAGE_V1


def for_packages(salon, package_ids):
    """
    {package id: its photo URL, or None} for packages of this salon's
    business (a storefront row with `tenant_id`). The ids as they come
    (UUIDs): the keys are the same values.
    """
    package_ids = set(package_ids)
    if not package_ids:
        return {}
    first = {}
    for package_id, url in (
        ServicePackageMedia.objects.filter(
            package_id__in=package_ids,
            tenant_id=salon.tenant_id,
            deleted_at__isnull=True,
        )
        .order_by("-is_primary", "sort_order", "created_at", "id")
        .values_list("package_id", "url")
    ):
        if url:
            first.setdefault(package_id, url)
    return {package_id: first.get(package_id) for package_id in package_ids}

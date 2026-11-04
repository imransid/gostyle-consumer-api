"""
GET /api/v1/search/options: the Smart Search's lists (SEARCH_OPTIONS_V1,
docs/SEARCH_OPTIONS_API.md), in the map drawer's `sections` shape. Every id
is one GET /search/salons and GET /search/services take back.

ONLY WHAT HAS DATA IS SENT (the audit, 4.1): a chip no public salon could
match is left out, so the app never shows a chip that always answers
nothing, and a chip appears the day its data does, with no app release.
Mall Shops, Home Service, Metro Near, Membership Card, the points chips,
"Verified" and the regions are not here at all: they wait on the client
(D2 to D5).
"""

import types

from django.core.cache import cache

from apps.platform_data.models import Service, Storefront

from . import search_results
from .models import SearchTag
from .search_results import AMENITY_OPTIONS, TAB_OPTIONS, SearchQuery
from .selectors import discoverable_salons

CACHE_KEY = "search-options:v1"
CACHE_SECONDS = 60

# The sections' titles. The app may print its own; these say what each is.
TITLES = {
    "tab": "Search in",
    "quick": "Quick picks",
    "amenities": "Amenities",
    "service_tags": "Services",
}

# The sub tag groups, in the Figma's order, titled as the admin's choices.
GROUPS = (
    SearchTag.Group.CUTS_STYLING,
    SearchTag.Group.BEARD_SHAVE,
    SearchTag.Group.COLOR_TREATMENTS,
)


def public_menu():
    """
    Every service on a public salon's menu (the Services tab's rules), as
    rows service_search.matches reads: a namespace holding the `service`
    dict (name, category_0_id, tags).
    """
    tenants = set(
        Storefront.objects.filter(visibility="PUBLIC", deleted_at__isnull=True).values_list("tenant_id", flat=True)
    )
    if not tenants:
        return []
    services = Service.objects.filter(
        tenant_id__in=list(tenants),
        status="PUBLISHED",
        deleted_at__isnull=True,
        online_booking_enabled=True,
    ).values("name", "category_0_id", "tags")
    return [types.SimpleNamespace(service=service) for service in services]


def has_data():
    """
    {option id: True/False} for the tab and amenity options that depend on
    data: is there at least one public salon it would keep.
    """
    def any_salon(**query):
        qs, _ = search_results.salon_queryset(SearchQuery(**query), with_text=False)
        return qs.exists()

    packages = any(search_results.tab_has_packages(salon) for salon in discoverable_salons())
    return {
        "near_me": True,
        "packages": packages,
        "parking": any_salon(amenities=("parking",)),
        "free_parking": any_salon(amenities=("free_parking",)),
    }


def _tag_option(slug, tag):
    return {"id": slug, "label": tag["label"], "icon": tag["icon"]}


def build(tags, data, menu, categories):
    """
    The sections from what was read: `tags` is search_results.active_tags(),
    `data` has_data(), `menu` public_menu(), `categories` the chip labels'
    categories. A section or a group with no option left is left out.
    """
    def matched(tag):
        return any(search_results.words_match(row, categories, tag["terms"]) for row in menu)

    with_data = {slug: tag for slug, tag in tags.items() if matched(tag)}

    sections = [
        {
            "key": "tab",
            "title": TITLES["tab"],
            "multiple": False,
            "default": ["near_me"],
            "options": [{"id": i, "label": label} for i, label in TAB_OPTIONS if data.get(i)],
        },
        {
            "key": "quick",
            "title": TITLES["quick"],
            "multiple": False,
            "default": [],
            "options": [
                _tag_option(slug, tag) for slug, tag in with_data.items()
                if tag["group"] == SearchTag.Group.QUICK
            ],
        },
        {
            "key": "amenities",
            "title": TITLES["amenities"],
            "multiple": True,
            "default": [],
            "options": [{"id": i, "label": label} for i, label in AMENITY_OPTIONS if data.get(i)],
        },
        {
            "key": "service_tags",
            "title": TITLES["service_tags"],
            "multiple": True,
            "default": [],
            "groups": [
                {
                    "key": group.value,
                    "title": group.label,
                    "options": [
                        _tag_option(slug, tag) for slug, tag in with_data.items() if tag["group"] == group
                    ],
                }
                for group in GROUPS
            ],
        },
    ]
    for section in sections:
        if "groups" in section:
            section["groups"] = [group for group in section["groups"] if group["options"]]
    return {"sections": [s for s in sections if s.get("options") or s.get("groups")]}


def options():
    """GET /search/options' body, cached 60 s (an admin edit clears it at once)."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    body = build(
        search_results.active_tags(),
        has_data(),
        public_menu(),
        search_results.public_categories(),
    )
    cache.set(CACHE_KEY, body, CACHE_SECONDS)
    return body


def forget():
    """Drop the cached lists, after the admin changed a chip."""
    cache.delete_many([CACHE_KEY, search_results.TAGS_CACHE_KEY])

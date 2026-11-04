"""
GET /api/v1/search/suggest (SEARCH_SUGGEST_V1, docs/SEARCH_SUGGEST_API.md):
the type-ahead under the search bar, while the customer types.

LIGHT ON PURPOSE. It runs on every few key presses, so it reads names only:
never the salon card's subqueries, at most SCAN rows of each kind, and at
most LIMIT of each in the answer. The view throttles it per address
(SEARCH_SUGGEST_RATE).

The same rules as the screens it leads to: a salon is a /discover salon
found as /discover?search= finds it (name, branch name, city); a service
is on a public salon's menu by the Services tab's rules; a stylist is one
the app shows at a public salon (selectors.showable_stylists, the
Stylists tab's rules without the salon). Products wait (D12).
"""

from collections import Counter

from django.db.models import Exists, OuterRef, Subquery, TextField, Value
from django.db.models.functions import Coalesce, Concat

from apps.platform_data.models import FileItem, Service, Storefront

from .geo import format_distance
from .params import SEARCH_MAX_LENGTH, ParamError, _number, _text
from .selectors import (
    _live_login,
    discoverable_salons,
    filter_by_search,
    showable_stylists,
    with_distance,
    with_published_card_fields,
)

# At most this many of each kind in the answer.
LIMIT = 5
# Rows read per kind before the ranking picks LIMIT of them.
SCAN = 50
# Service rows read: a short word ("cut") is on many menus.
SERVICE_SCAN = 500
MIN_LENGTH = 2


def parse(params):
    """(q, latitude, longitude), checked. Raises ParamError naming the param."""
    q = _text(params, "q", max_length=SEARCH_MAX_LENGTH)
    if q is None:
        raise ParamError("q", f"Send at least {MIN_LENGTH} characters.", code="required")
    if len(q) < MIN_LENGTH:
        raise ParamError("q", f"Send at least {MIN_LENGTH} characters.", code="too_short")
    latitude = _number(params, "latitude", minimum=-90, maximum=90)
    longitude = _number(params, "longitude", minimum=-180, maximum=180)
    if (latitude is None) != (longitude is None):
        missing = "longitude" if longitude is None else "latitude"
        raise ParamError(missing, "Send latitude and longitude together.", code="required")
    return q, latitude, longitude


def rank(name, q):
    """0: the name starts with q. 1: a word of it does. 2: q is somewhere inside."""
    name, wanted = (name or "").casefold(), q.casefold()
    if name.startswith(wanted):
        return 0
    if any(word.startswith(wanted) for word in name.split()):
        return 1
    return 2


def _name_key(name):
    return " ".join((name or "").split()).casefold()


def salon_query(q, latitude=None, longitude=None):
    """The salons /discover?search= finds, names, logo and distance only (lazy, not capped)."""
    qs = filter_by_search(with_published_card_fields(discoverable_salons()), q)
    fields = ["id", "published_name", "branch_name", "logo_url"]
    if latitude is not None:
        qs = with_distance(qs, latitude, longitude)
        fields.append("distance_km")
    return qs.order_by("id").values(*fields)


def salons(q, latitude=None, longitude=None):
    """
    Up to LIMIT salons, as [{id, name, logo_url, distance}]: the best match
    first, then the nearest (with a position), then by name.
    """
    rows = list(salon_query(q, latitude, longitude)[:SCAN])

    def key(row):
        name = row["published_name"] or row["branch_name"]
        km = row.get("distance_km")
        return (rank(name, q), km if km is not None else float("inf"), _name_key(name), str(row["id"]))

    return [
        {
            "id": str(row["id"]),
            "name": row["published_name"] or row["branch_name"],
            "logo_url": row["logo_url"],
            "distance": format_distance(row.get("distance_km")),
        }
        for row in sorted(rows, key=key)[:LIMIT]
    ]


def services(q):
    """
    Up to LIMIT service NAMES, as [{name, salons}]: one per name across every
    public salon (spaces and case ignored, the spelling most salons use),
    with how many public salons offer it. The best match first, then the
    most offered. Tapping one runs the search with that name.
    """
    salons_of = Counter(
        Storefront.objects.filter(visibility="PUBLIC", deleted_at__isnull=True).values_list("tenant_id", flat=True)
    )
    if not salons_of:
        return []
    rows = (
        Service.objects.filter(
            tenant_id__in=list(salons_of),
            status="PUBLISHED",
            deleted_at__isnull=True,
            online_booking_enabled=True,
            name__icontains=q,
        )
        .order_by("id")
        .values_list("name", "tenant_id")[:SERVICE_SCAN]
    )

    spellings, tenants = {}, {}
    for name, tenant_id in rows:
        key = _name_key(name)
        if not key:
            continue
        spellings.setdefault(key, Counter())[" ".join(name.split())] += 1
        tenants.setdefault(key, set()).add(tenant_id)

    found = []
    for key, counter in spellings.items():
        label = min(counter.items(), key=lambda item: (-item[1], item[0]))[0]
        offered = sum(salons_of[tenant_id] for tenant_id in tenants[key])
        found.append({"name": label, "salons": offered})
    found.sort(key=lambda item: (rank(item["name"], q), -item["salons"], _name_key(item["name"])))
    return found[:LIMIT]


def stylist_query(q):
    """
    The stylists the app shows at a public salon (their home branch's) whose
    name holds q, as rows (lazy, not capped). The name and photo come from
    their login account, as the salon's Stylists tab reads them
    (selectors.salon_stylists).
    """
    user = _live_login()
    at_public_salon = Storefront.objects.filter(
        branch_id=OuterRef("branch_id"),
        tenant_id=OuterRef("tenant_id"),
        visibility="PUBLIC",
        deleted_at__isnull=True,
    )
    return (
        showable_stylists()
        .filter(Exists(at_public_salon))
        .annotate(
            first_name=Subquery(user.values("first_name")[:1], output_field=TextField()),
            last_name=Subquery(user.values("last_name")[:1], output_field=TextField()),
            avatar_file_id=Subquery(user.values("avatar_file_item_id")[:1]),
        )
        .annotate(
            full_name=Concat(
                Coalesce("first_name", Value(""), output_field=TextField()),
                Value(" "),
                Coalesce("last_name", Value(""), output_field=TextField()),
                output_field=TextField(),
            ),
            avatar_url=Subquery(
                FileItem.objects.filter(id=OuterRef("avatar_file_id")).values("url")[:1],
                output_field=TextField(),
            ),
        )
        .filter(full_name__icontains=q)
        .order_by("id")
        .values("id", "branch_id", "first_name", "last_name", "avatar_url")
    )


def stylists(q):
    """Up to LIMIT stylists, as [{id, name, photo_url, salon: {id, name}}], the best match first."""
    rows = list(stylist_query(q)[:SCAN])
    if not rows:
        return []

    salon_of = {
        row["branch_id"]: {"id": str(row["id"]), "name": row["published_name"] or row["branch_name"]}
        for row in with_published_card_fields(discoverable_salons())
        .filter(branch_id__in={row["branch_id"] for row in rows})
        .values("id", "branch_id", "published_name", "branch_name")
    }

    found = []
    for row in rows:
        name = " ".join(filter(None, [row["first_name"], row["last_name"]])) or None
        salon = salon_of.get(row["branch_id"])
        if name is None or salon is None:
            continue
        found.append({"id": str(row["id"]), "name": name, "photo_url": row["avatar_url"], "salon": salon})
    found.sort(key=lambda item: (rank(item["name"], q), _name_key(item["name"]), item["id"]))
    return found[:LIMIT]


def suggest(q, latitude=None, longitude=None):
    """GET /search/suggest's body."""
    return {
        "salons": salons(q, latitude, longitude),
        "services": services(q),
        "stylists": stylists(q),
    }


from apps.platform_data.models import StorefrontVersion

# Every section the platform can write. Order matches gostyle-platform's
# STORED_SECTION_KEYS, plus FEATURED which that repo synthesises on read.
SECTION_KEYS = (
    "COVER",
    "IDENTITY",
    "BADGES",
    "POLICY",
    "AUDIENCE",
    "AMENITIES",
    "SOCIALS",
    "HOURS",
    "LOGO",
    "MAP",
    "GALLERY_FEATURED",
    "GOVERNANCE",
    "FEATURED",
)


def empty_snapshot() -> dict:
    """Every section present and empty. The shape callers can always rely on."""
    return {key: {} for key in SECTION_KEYS}


def read_snapshot(storefront) -> dict:
    """
    Return the live published content for a storefront.

    Always returns a dict with all thirteen section keys present. An
    unpublished salon gets empty sections, never an exception, because a
    salon that has not published yet is an ordinary state, not an error.
    """
    version_id = getattr(storefront, "live_version_id", None)
    if not version_id:
        return empty_snapshot()

    raw = (
        StorefrontVersion.objects
        .filter(id=version_id)
        .values_list("snapshot", flat=True)
        .first()
    )

    return normalize(raw)


def normalize(raw) -> dict:
    """
    Coerce whatever came out of the JSONB column into the guaranteed shape.

    Defensive on purpose. The column is ours but it is old by definition: a
    version published last year has whatever sections existed last year. An
    unexpected shape means empty, never a crash.
    """
    snap = empty_snapshot()

    if not isinstance(raw, dict):
        return snap

    for key in SECTION_KEYS:
        value = raw.get(key)
        if isinstance(value, dict):
            snap[key] = value

    return snap


def field(snapshot: dict, section: str, name: str, default=None):
    """
    Read one field. Blank strings collapse to the default.

    The platform stores a cleared text box as null, but an older snapshot may
    hold "" or "   ". Callers want the same answer for all three.
    """
    value = snapshot.get(section, {}).get(name)

    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    return value


def items(snapshot: dict, section: str, name: str) -> list:
    """Same as field(), for list-valued keys. Always returns a list."""
    value = snapshot.get(section, {}).get(name)
    return value if isinstance(value, list) else []
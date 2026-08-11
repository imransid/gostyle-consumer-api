"""
Platform vocabulary to app vocabulary.

The platform stores what it needs; the mobile contract wants something else.
Every conversion between the two lives here so the mapping is in one readable
place rather than scattered through serializers.

PURE. No Django. Same discipline as snapshot.py, hours.py and money.py.
"""

# ─── Amenities ────────────────────────────────────────────────────────
# The platform's closed set of eight. Free text was deliberately removed so
# amenities could be translated, filtered and given icons, so this list is
# the whole vocabulary and a ninth value is a platform change.
AMENITY_SLUGS = {
    "WIFI": "wifi",
    "REFRESHMENTS": "refreshments",
    "PARKING": "parking",
    "PRAYER_ROOM": "prayer_room",
    "CARD_PAYMENT": "card_payment",
    "AIR_CONDITIONING": "air_conditioning",
    "KIDS_CORNER": "kids_corner",
    "WHEELCHAIR_ACCESS": "wheelchair_access",
}


def amenities(keys):
    """
    ['WIFI', 'PARKING'] -> ['wifi', 'parking']

    Unknown keys are DROPPED, not passed through. A snapshot may hold a value
    from a future platform release; emitting it would send the app a slug it
    has no icon for.
    """
    if not isinstance(keys, list):
        return []
    return [AMENITY_SLUGS[k] for k in keys if k in AMENITY_SLUGS]


# ─── Social links ─────────────────────────────────────────────────────
# The platform stores HANDLES, not URLs, on purpose: a manager who pastes a
# full URL into the box would produce a doubled, dead link. So the URL is
# built here, at the read edge.
#
# whatsapp is a PHONE NUMBER, not a handle. website is already a full URL.
SOCIAL_URLS = {
    "instagram": "https://instagram.com/{}",
    "tiktok": "https://tiktok.com/@{}",
    "youtube": "https://youtube.com/@{}",
    "facebook": "https://facebook.com/{}",
    "whatsapp": "https://wa.me/{}",
}


def social_links(socials):
    """
    Build the public link list from the SOCIALS section.

    Two rules the platform sets and this must honour:
      - `hidden` lists networks the salon switched OFF. A handle may still be
        stored for them; it must not be published.
      - `website` is already a URL and is emitted as-is.
    """
    if not isinstance(socials, dict):
        return []

    hidden_raw = socials.get("hidden")
    hidden = {h.lower() for h in hidden_raw if isinstance(h, str)} if isinstance(hidden_raw, list) else set()

    links = []
    for platform, template in SOCIAL_URLS.items():
        if platform in hidden:
            continue
        handle = socials.get(platform)
        if not isinstance(handle, str) or not handle.strip():
            continue
        value = handle.strip().lstrip("@")
        if platform == "whatsapp":
            # wa.me wants digits only: strip +, spaces, dashes, brackets.
            value = "".join(ch for ch in value if ch.isdigit())
            if not value:
                continue
        links.append({"platform": platform, "url": template.format(value)})

    website = socials.get("website")
    if "website" not in hidden and isinstance(website, str) and website.strip():
        links.append({"platform": "website", "url": website.strip()})

    return links


# ─── Price tier ───────────────────────────────────────────────────────
# The platform has FOUR tiers; the app renders a 1-3 dollar-sign scale.
# UPSCALE and PREMIUM both map to 3, which is lossy and accepted: the app
# cannot draw a fourth sign today.
PRICE_LEVELS = {
    "BUDGET": 1,
    "MID_RANGE": 2,
    "UPSCALE": 3,
    "PREMIUM": 3,
}


def price_level(tier):
    """'UPSCALE' -> 3. Unknown or missing -> None so the app hides the row."""
    return PRICE_LEVELS.get(tier)


# ─── Audience ─────────────────────────────────────────────────────────
def category(mode):
    """AUDIENCE.mode 'GENTS' -> 'gents'."""
    return mode.lower() if mode in ("LADIES", "GENTS", "UNISEX") else None


# ─── Booking policy ───────────────────────────────────────────────────
def booking_policy(deposit_mode, deposit_bps, cancel_window_hours):
    """
    Shape the POLICY projection into the app's booking_policy object.

    `free_cancellation` is a boolean here and a number of hours in the
    platform, which is lossy: the real model is a tiered refund ladder.
    cancel_window_hours is sent alongside so the app can show the real window
    when it is ready to.
    """
    from .money import bps_to_percent

    required = deposit_mode not in (None, "NONE")
    percent = bps_to_percent(deposit_bps) or 0 if required else 0

    return {
        "deposit_required": required,
        "deposit_percentage": percent,
        "deposit_amount": 0,          # platform stores percent, not flat amount
        "free_cancellation": bool(cancel_window_hours and cancel_window_hours > 0),
        "cancel_window_hours": cancel_window_hours,
    }
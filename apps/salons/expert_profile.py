"""
The Expert Profile screen's answer, built from rows the view has read.

Pure: rows in, dicts out, no queryset. The view (expert_profile_views.py)
reads; this writes the contract's fields (docs/expert-profile-fe-contract.md
§2). Built step by step (docs/EXPERT_PROFILE_AUDIT.md, section 8): the person
in E2; the services in E4; the salon in E5; the media in E6; the day off in
E7; the fields with no data yet, and the contract's key order, in E8.
"""

from .hours import CLOSED_TODAY

# The hero strip shows five and the grid draws a "+N" tile (contract §3).
MEDIA_MAX = 5


def core_fields(salon, row):
    """
    The person, from `id` to `is_network_member`, in the contract's order.

    id, salon_id, name, title, role, avatar_url and day_off are exactly what
    the Stylists tab and the Expert step show. `row` is this stylist's own
    row of GET /salon/<id>/stylists (`views._stylist_row`). Taking the
    finished row, and not the staff record, is the point: one piece of code
    decides how a name is joined and that a blank title is null, so the same
    person cannot read differently on the two screens.

    The rest has no data yet (docs/EXPERT_PROFILE_AUDIT.md, section 5), and
    the contract allows each empty.

    Only the contract's keys go out. The list row's `tenant_id` and
    `branch_id` stay in the list.
    """
    return {
        "id": row["id"],
        "salon_id": str(salon.id),
        "name": row["name"],
        "title": row["title"],
        "role": row["role"],
        # No column anywhere: not on the staff record, not on the person's
        # account. The salon's own "about" is the salon's. null hides the
        # block.
        "bio": None,
        "avatar_url": row["avatar_url"],
        # Reviews belong to a salon (storefront_review) and nothing links one
        # to a stylist. null is the contract's "nobody has rated them", 0 its
        # "No reviews yet". The list row says null for the count; here the
        # contract types it a number (Rafa, Q14).
        "rating": None,
        "review_count": 0,
        # The list row's own value: null until the platform has the column.
        "years_experience": row["years_experience"],
        # Nothing prices a stylist: the platform has a price tier for a SALON
        # and a level per skill, not this. null hides the row (Rafa, Q6).
        "price_level": None,
        # The row's own text, worked out from the roster (roster.py):
        # "Tuesday", "Friday, Saturday", or null, which hides the row.
        "day_off": row["day_off"],
        # Nothing in the platform has this meaning. false, never null, until
        # the app team says what the badge should mean (Rafa, Q8).
        "is_network_member": False,
    }


def service_groups(tab_groups, covered_ids):
    """
    service_groups: only what this stylist does, grouped and ordered as the
    Services tab.

    `tab_groups` is the WHOLE menu as the tab groups it (the groups of
    `menu.service_groups`); `covered_ids` is the services this stylist can do
    alone (`selectors.stylist_service_coverage`, the Expert step's own rule).

    The whole menu is grouped first and narrowed after, on purpose: the
    groups then come in the tab's order even when this stylist cannot do the
    service that put a group first on the tab. Each row is the tab's own row,
    untouched, so the price and the keys are the tab's. A group with nothing
    left is dropped. A group goes out as `{id, name, services}`: the tab's
    `category_id` is for its chips, and this screen has none.
    """
    covered = {str(service_id) for service_id in covered_ids}

    groups = []
    for group in tab_groups:
        rows = [row for row in group["services"] if row["id"] in covered]
        if rows:
            groups.append({"id": group["id"], "name": group["name"], "services": rows})
    return groups


def salon_block(page):
    """
    salon: the salon this stylist is read at, as `{id, name, is_open,
    hours_today, latitude, longitude}`.

    `page` is GET /salon/<id>'s own answer for that salon
    (`views.salon_profile_data`). Taking the finished answer is the point:
    the published name, "open now" and the pin are decided in one place, so
    the salon reads the same here as on its own page.

    Two things differ from that page, both on purpose (Rafa, Q16):

      * `hours_today` is null on a day the salon does not open (a closed
        weekday, or closed by hand), where the page says "Closed". The
        contract's null hides the row.
      * The pin is two flat numbers, without the page's address and map link.
        The published MAP pin, else the branch's, else null and null.

    `is_open` is the page's, null included: null is "no hours published",
    which is not the same as closed.
    """
    location = page["location"]
    hours_today = page["hours_today"]
    return {
        "id": str(page["id"]),
        "name": page["name"],
        "is_open": page["is_open"],
        "hours_today": None if hours_today == CLOSED_TODAY else hours_today,
        "latitude": location["latitude"],
        "longitude": location["longitude"],
    }


def _media_item(row):
    """
    One photo row as an item of `media`, or None when it cannot be shown.

    `type` is read from the file's mime type. An image has no thumbnail (the
    contract: null). A video needs one to draw its tile, so a video with none
    is left out; the platform stores none (it takes no video at all today),
    so that is every video until the row carries a `thumbnail_url`. A file
    that is neither, and a row with no URL, are left out too.
    """
    if not row.get("url"):
        return None

    mime_type = (row.get("mime_type") or "").lower()
    if mime_type.startswith("image/"):
        kind, thumbnail_url = "image", None
    elif mime_type.startswith("video/") and row.get("thumbnail_url"):
        kind, thumbnail_url = "video", row["thumbnail_url"]
    else:
        return None

    return {
        "id": str(row["id"]),
        "type": kind,
        "url": row["url"],
        "thumbnail_url": thumbnail_url,
    }


def media(rows):
    """
    has_story, media and media_count, from the rows
    `selectors.stylist_media_rows` reads (this salon's photos tagged with the
    stylist, newest first).

    media is the first MEDIA_MAX that can be shown, in the rows' order;
    media_count counts every one that can be shown, so the app's "+N" is
    media_count - len(media). A row that cannot be shown (`_media_item`) is
    neither listed nor counted.

    has_story is "the stylist has media" (Rafa, Q10). No stylist stories
    exist, and the contract's own open point says the avatar ring and the
    grid both read `media` today.
    """
    items = [item for item in map(_media_item, rows) if item is not None]
    return {
        "has_story": bool(items),
        "media": items[:MEDIA_MAX],
        "media_count": len(items),
    }

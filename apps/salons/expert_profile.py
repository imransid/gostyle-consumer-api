"""
The Expert Profile screen's answer, built from rows the view has read.

Pure: rows in, dicts out, no queryset. The view (expert_profile_views.py)
reads; this writes the contract's fields (docs/expert-profile-fe-contract.md
§2). Built step by step (docs/EXPERT_PROFILE_AUDIT.md, section 8): the person
in E2; the services in E4; the salon, the media and the rest in E5 to E8.
"""


def core_fields(salon, row):
    """
    id, salon_id, name, title, role and avatar_url: the person, exactly as
    the Stylists tab and the Expert step show them. Plus rating and
    review_count, empty until stylist reviews exist.

    `row` is this stylist's own row of GET /salon/<id>/stylists
    (`views._stylist_row`). Taking the finished row, and not the staff record,
    is the point: one piece of code decides how a name is joined and that a
    blank title is null, so the same person cannot read differently on the
    two screens.

    Only the contract's keys go out. The list row's `tenant_id` and
    `branch_id` stay in the list.
    """
    return {
        "id": row["id"],
        "salon_id": str(salon.id),
        "name": row["name"],
        "title": row["title"],
        "role": row["role"],
        "avatar_url": row["avatar_url"],
        # No data yet: reviews belong to a salon (storefront_review) and
        # nothing links one to a stylist. null is the contract's "nobody has
        # rated them", 0 its "No reviews yet". The list row says null for the
        # count; here the contract types it a number (Rafa, Q14).
        "rating": None,
        "review_count": 0,
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

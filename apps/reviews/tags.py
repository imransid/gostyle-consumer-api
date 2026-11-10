"""
The "What stood out?" tags (docs/rate-your-visit-fe-contract.md, section 1, `tags`).

A code is stable and never renamed: the app sends codes and the review
stores them. The label is what the customer reads, and a saved review shows
the label of today, so a label can change here without touching any review.
The app shows the tags in the order of these lists.
"""

SALON_TAGS = (
    ("cleanliness", "Cleanliness"),
    ("ambience", "Ambience"),
    ("music_vibe", "Music vibe"),
    ("comfortable_seats", "Comfortable seats"),
    ("premium_feel", "Premium feel"),
    ("fair_pricing", "Fair pricing"),
    ("parking", "Parking"),
    ("wifi", "Wi-Fi"),
)

STYLIST_TAGS = (
    ("skill", "Skill"),
    ("attention_to_detail", "Attention to detail"),
    ("speed", "Speed"),
    ("professionalism", "Professionalism"),
    ("friendliness", "Friendliness"),
    ("listened", "Listened to me"),
    ("style_advice", "Style advice"),
    ("beard_work", "Beard work"),
)

# Left out of a ladies salon's stylist list, as the doc's own example says.
NOT_FOR_LADIES = frozenset({"beard_work"})

SALON_LABELS = dict(SALON_TAGS)
STYLIST_LABELS = dict(STYLIST_TAGS)


def tags_for(salon_category):
    """The two lists for one salon. `salon_category` is "gents", "ladies", "unisex" or None."""
    stylist = STYLIST_TAGS
    if salon_category == "ladies":
        stylist = tuple(tag for tag in STYLIST_TAGS if tag[0] not in NOT_FOR_LADIES)
    return {
        "salon": [{"code": code, "label": label} for code, label in SALON_TAGS],
        "stylist": [{"code": code, "label": label} for code, label in stylist],
    }


def with_labels(codes, labels):
    """Stored codes as the review object shows them: [{code, label}], in the order saved."""
    return [{"code": code, "label": labels.get(code, code)} for code in codes]

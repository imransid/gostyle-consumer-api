"""
The Smart Search's word chips as the Figma shows them (audit D6). Edited in
the admin afterwards. "Offers (20 Pts)" is not here: it waits on D5.

A service matches a chip when one of its words is in the service's name,
its category's label or one of its tags, case ignored. "cut" finds
"Haircut", "Kids Cut" and every service filed under "Haircut and Styling".
"""

from django.db import migrations

# (group, slug, label, words), in the Figma's order inside each group.
TAGS = (
    ("quick", "cuts_fade", "Cuts & Fade", ["cut", "fade"]),
    ("quick", "beard_shave", "Beard & Shave", ["beard", "shave"]),
    ("quick", "color_styles", "Color & Styles", ["color", "colour", "highlight", "balayage", "blow"]),
    ("cuts_styling", "fade", "Fade", ["fade"]),
    ("cuts_styling", "buzz", "Buzz", ["buzz"]),
    ("cuts_styling", "scissor", "Scissor", ["scissor"]),
    ("beard_shave", "beard_trim", "Beard Trim", ["beard trim", "beard"]),
    ("beard_shave", "hot_towel", "Hot Towel", ["hot towel"]),
    ("beard_shave", "shave", "Shave", ["shave"]),
    ("color_treatments", "grey_blend", "Grey Blend", ["grey blend", "gray blend", "grey", "gray"]),
    ("color_treatments", "highlights", "Highlights", ["highlight"]),
    ("color_treatments", "keratin", "Keratin", ["keratin"]),
)


def seed(apps, schema_editor):
    SearchTag = apps.get_model("salons", "SearchTag")
    for order, (group, slug, label, words) in enumerate(TAGS):
        SearchTag.objects.update_or_create(
            slug=slug,
            defaults={"group": group, "label_en": label, "match_terms": words, "sort_order": order},
        )


def unseed(apps, schema_editor):
    SearchTag = apps.get_model("salons", "SearchTag")
    SearchTag.objects.filter(slug__in=[slug for _, slug, _, _ in TAGS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("salons", "0003_search_tag"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

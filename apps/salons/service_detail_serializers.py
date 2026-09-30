"""
The Service Detail answer, described for the OpenAPI page.

Documentation only: the view writes plain dicts (service_detail.py), and
nothing here validates or renders. Names start with SalonServiceDetail so
they never collide with services-details' own ServiceDetail component.
"""

from rest_framework import serializers


class SalonServiceDetailCategorySerializer(serializers.Serializer):
    id = serializers.CharField(help_text='The Services tab\'s chip: a category UUID, or "other".')
    label = serializers.CharField()


class SalonServiceDetailRowSerializer(serializers.Serializer):
    label = serializers.CharField(
        help_text="Time Duration, Suitable for, Consultation, Patch test or Minimum age."
    )
    value = serializers.CharField(help_text="Written out for the customer.")
    icon = serializers.ChoiceField(choices=["clock", "scissors", "sparkles", "drop"])


class SalonServiceDetailProductSerializer(serializers.Serializer):
    """The Shop tab's product card. Always [] for now."""

    id = serializers.UUIDField()
    variant_id = serializers.UUIDField(help_text="What a booking sends as the product's id.")
    name = serializers.CharField()
    price = serializers.FloatField(help_text="Unit price, before VAT.")
    image_url = serializers.CharField(allow_null=True)


class SalonServiceDetailExpertSerializer(serializers.Serializer):
    """Exactly a row of GET /salon/{salon_id}/stylists?service_ids={service_id}."""

    id = serializers.UUIDField(help_text="The stylist id the booking flow sends.")
    tenant_id = serializers.UUIDField()
    branch_id = serializers.UUIDField(allow_null=True)
    name = serializers.CharField(allow_null=True)
    title = serializers.CharField(allow_null=True, help_text="The job title on the staff record.")
    role = serializers.CharField(allow_null=True, help_text="The expertise line on the person's account.")
    avatar_url = serializers.CharField(allow_null=True)
    rating = serializers.FloatField(allow_null=True, help_text="Always null: no stylist reviews yet.")
    review_count = serializers.IntegerField(allow_null=True, help_text="Always null: no stylist reviews yet.")
    years_experience = serializers.IntegerField(allow_null=True, help_text="Always null for now.")
    day_off = serializers.CharField(
        allow_null=True,
        help_text=(
            "Worked out from the roster: the weekday or weekdays the stylist "
            "never works (\"Tuesday\", \"Friday, Saturday\"). Null when it "
            "cannot be told."
        ),
    )
    service_ids = serializers.ListField(
        child=serializers.UUIDField(), help_text="This service's id: the one asked about."
    )


class SalonServiceDetailResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    salon_id = serializers.UUIDField()
    name = serializers.CharField()
    description = serializers.CharField(allow_null=True)
    price = serializers.FloatField(help_text="One visit, before VAT: the Services tab's number.")
    duration_min = serializers.IntegerField(help_text="Minutes.")
    duration_max = serializers.IntegerField(help_text="Minutes. Today always equal to duration_min.")
    category = SalonServiceDetailCategorySerializer()
    rating = serializers.FloatField(allow_null=True, help_text="Always null for now: no per-service reviews yet.")
    review_count = serializers.IntegerField(help_text="Always 0 for now.")
    is_active = serializers.BooleanField(help_text="false: pulled from sale. Hide Book Appointment.")
    hero_url = serializers.CharField(allow_null=True, help_text="The first photo. null: use the salon's cover.")
    gallery = serializers.ListField(child=serializers.CharField(), help_text="At most 5; the hero is the first.")
    gallery_count = serializers.IntegerField(help_text="All the photos. \"+N\" is gallery_count - len(gallery).")
    included = serializers.ListField(child=serializers.CharField(), help_text="The stage names. [] hides the block.")
    details = SalonServiceDetailRowSerializer(many=True)
    preparation = serializers.ListField(child=serializers.CharField(), help_text="[] hides the block.")
    products = SalonServiceDetailProductSerializer(many=True, help_text="Always [] for now.")
    experts = SalonServiceDetailExpertSerializer(
        many=True, help_text="[] means nobody here can do it (or it is pulled): keep Book disabled."
    )


# What the server really answered for the seeded salon, with photos, stages
# and care text added (see ExampleIsRealTests, which rebuilds it from rows).
EXAMPLE = {
    "id": "66666666-6666-6666-6666-666666666660",
    "salon_id": "33333333-3333-3333-3333-333333333333",
    "name": "The Gentleman's Cut",
    "description": "A classic cut tailored to you, with a wash, a scalp massage and a styled finish.",
    "price": 199.0,
    "duration_min": 30,
    "duration_max": 30,
    "category": {
        "id": "55555555-5555-5555-5555-555555555551",
        "label": "Haircut and Styling",
    },
    "rating": None,
    "review_count": 0,
    "is_active": True,
    "hero_url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/1.jpg",
    "gallery": [
        "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/1.jpg",
        "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/2.jpg",
        "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/3.jpg",
        "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/4.jpg",
        "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/services/66666666-6666-6666-6666-666666666660/media/5.jpg",
    ],
    "gallery_count": 7,
    "included": [
        "Consultation",
        "Wash",
        "Scalp massage",
        "Hair cut",
        "Styling",
    ],
    "details": [
        {
            "label": "Time Duration",
            "value": "30 min",
            "icon": "clock",
        },
        {
            "label": "Suitable for",
            "value": "Men",
            "icon": "scissors",
        },
        {
            "label": "Consultation",
            "value": "Needed before this service.",
            "icon": "sparkles",
        },
    ],
    "preparation": [
        "Arrive with clean, dry hair.",
        "Ask your stylist which products suit your hair.",
    ],
    "products": [],
    "experts": [
        {
            "id": "99999999-9999-9999-9999-999999999991",
            "tenant_id": "11111111-1111-1111-1111-111111111111",
            "branch_id": "22222222-2222-2222-2222-222222222222",
            "name": "Darius Stone",
            "title": "Master Barber",
            "role": "Haircut and Styling Expert",
            "avatar_url": None,
            "rating": None,
            "review_count": None,
            "years_experience": None,
            "day_off": None,
            "service_ids": [
                "66666666-6666-6666-6666-666666666660",
            ],
        },
        {
            "id": "99999999-9999-9999-9999-999999999990",
            "tenant_id": "11111111-1111-1111-1111-111111111111",
            "branch_id": "22222222-2222-2222-2222-222222222222",
            "name": "Liam Johnson",
            "title": "Senior Barber",
            "role": "Barber and Grooming Expert",
            "avatar_url": None,
            "rating": None,
            "review_count": None,
            "years_experience": None,
            "day_off": "Tuesday",
            "service_ids": [
                "66666666-6666-6666-6666-666666666660",
            ],
        },
    ],
}

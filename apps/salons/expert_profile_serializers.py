"""
The Expert Profile answer, described for the OpenAPI page.

Documentation only: the view writes plain dicts (expert_profile.py), and
nothing here validates or renders. Names start with SalonExpertProfile so
they never collide with another screen's components.
"""

from rest_framework import serializers


class SalonExpertProfileMediaSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The photo's own id.")
    type = serializers.ChoiceField(
        choices=["image", "video"],
        help_text="Every item is an image today: a salon cannot upload video yet.",
    )
    url = serializers.CharField(help_text="The image (or the video file). A plain storage URL.")
    thumbnail_url = serializers.CharField(allow_null=True, help_text="Always null for an image.")


class SalonExpertProfileSalonSerializer(serializers.Serializer):
    """What GET /salon/{salon_id} says about the same salon."""

    id = serializers.UUIDField(help_text="The salon_id from the path.")
    name = serializers.CharField(allow_null=True)
    is_open = serializers.BooleanField(
        allow_null=True, help_text="Open right now. null: the salon published no hours."
    )
    hours_today = serializers.CharField(
        allow_null=True,
        help_text=(
            "For example \"10:00 AM - 9:00 PM\". null on a day the salon does "
            "not open, and when it published no hours."
        ),
    )
    latitude = serializers.FloatField(allow_null=True, help_text="null: the salon has no pin.")
    longitude = serializers.FloatField(allow_null=True)


class SalonExpertProfileServiceSerializer(serializers.Serializer):
    """Exactly a row of GET /salon/{salon_id}/services."""

    id = serializers.UUIDField(help_text="Goes to the booking flow as one of serviceIds.")
    name = serializers.CharField()
    description = serializers.CharField(allow_null=True)
    price = serializers.FloatField(help_text="One visit, before VAT: the Services tab's number.")
    duration_min = serializers.IntegerField(help_text="Minutes.")
    duration_max = serializers.IntegerField(help_text="Minutes. Today always equal to duration_min.")


class SalonExpertProfileServiceGroupSerializer(serializers.Serializer):
    id = serializers.CharField(help_text='The Services tab\'s group id: a category UUID, or "other".')
    name = serializers.CharField()
    services = SalonExpertProfileServiceSerializer(many=True, help_text="Never empty.")


class SalonExpertProfileResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The stylist id the booking flow sends.")
    salon_id = serializers.UUIDField(help_text="The salon_id from the path, echoed.")
    name = serializers.CharField(allow_null=True)
    title = serializers.CharField(allow_null=True, help_text="The job title on the staff record.")
    role = serializers.CharField(allow_null=True, help_text="The expertise line on the person's account.")
    bio = serializers.CharField(allow_null=True, help_text="Always null for now.")
    avatar_url = serializers.CharField(allow_null=True, help_text="null: draw the initials.")
    rating = serializers.FloatField(allow_null=True, help_text="Always null for now: no stylist reviews yet.")
    review_count = serializers.IntegerField(help_text="Always 0 for now.")
    years_experience = serializers.IntegerField(allow_null=True, help_text="Always null for now.")
    price_level = serializers.IntegerField(allow_null=True, help_text="Always null for now.")
    day_off = serializers.CharField(
        allow_null=True,
        help_text=(
            "Worked out from the roster: the weekday or weekdays the stylist "
            "never works (\"Tuesday\", \"Friday, Saturday\"). Null when it "
            "cannot be told. The same text as in GET /salon/{salon_id}/stylists."
        ),
    )
    is_network_member = serializers.BooleanField(help_text="Always false for now.")
    is_favorite = serializers.BooleanField(
        help_text=(
            "The caller's own heart on this stylist (POST /favourite with "
            "stylist_id). Send the token to get it: a guest always gets false."
        )
    )
    has_story = serializers.BooleanField(help_text="True when media_count is above 0.")
    media = SalonExpertProfileMediaSerializer(
        many=True,
        help_text="This salon's photos tagged with the stylist, newest first. At most 5.",
    )
    media_count = serializers.IntegerField(
        help_text="All of them. \"+N\" is media_count - len(media)."
    )
    salon = SalonExpertProfileSalonSerializer()
    service_groups = SalonExpertProfileServiceGroupSerializer(
        many=True,
        help_text=(
            "Only what this stylist can do, in the groups, order, rows and "
            "prices of GET /salon/{salon_id}/services. [] when nothing."
        ),
    )


# What the server really answered for the seeded salon's Liam Johnson, as a
# guest, on Wednesday 2026-09-30 at 15:00 in Dubai, with seven gallery photos
# tagged with him (see ExampleIsRealTests, which rebuilds it from rows).
EXAMPLE = {
    "id": "99999999-9999-9999-9999-999999999990",
    "salon_id": "33333333-3333-3333-3333-333333333333",
    "name": "Liam Johnson",
    "title": "Senior Barber",
    "role": "Barber and Grooming Expert",
    "bio": None,
    "avatar_url": None,
    "rating": None,
    "review_count": 0,
    "years_experience": None,
    "price_level": None,
    "day_off": "Tuesday",
    "is_network_member": False,
    "is_favorite": False,
    "has_story": True,
    "media": [
        {
            "id": "8e2b1f10-0000-4000-8000-000000000007",
            "type": "image",
            "url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/storefronts/33333333-3333-3333-3333-333333333333/media/kids-cut.jpg",
            "thumbnail_url": None,
        },
        {
            "id": "8e2b1f10-0000-4000-8000-000000000006",
            "type": "image",
            "url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/storefronts/33333333-3333-3333-3333-333333333333/media/taper.jpg",
            "thumbnail_url": None,
        },
        {
            "id": "8e2b1f10-0000-4000-8000-000000000005",
            "type": "image",
            "url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/storefronts/33333333-3333-3333-3333-333333333333/media/crew-cut.jpg",
            "thumbnail_url": None,
        },
        {
            "id": "8e2b1f10-0000-4000-8000-000000000004",
            "type": "image",
            "url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/storefronts/33333333-3333-3333-3333-333333333333/media/classic-side-part.jpg",
            "thumbnail_url": None,
        },
        {
            "id": "8e2b1f10-0000-4000-8000-000000000003",
            "type": "image",
            "url": "https://gostyle-media.s3.me-central-1.amazonaws.com/tenants/11111111-1111-1111-1111-111111111111/storefronts/33333333-3333-3333-3333-333333333333/media/hot-towel-shave.jpg",
            "thumbnail_url": None,
        },
    ],
    "media_count": 7,
    "salon": {
        "id": "33333333-3333-3333-3333-333333333333",
        "name": "The Iron Razor Barbershop",
        "is_open": True,
        "hours_today": "10:00 AM - 10:00 PM",
        "latitude": 25.2213,
        "longitude": 55.2621,
    },
    "service_groups": [
        {
            "id": "55555555-5555-5555-5555-555555555553",
            "name": "Beard Care",
            "services": [
                {
                    "id": "66666666-6666-6666-6666-666666666662",
                    "name": "Hot Towel Shave",
                    "description": "Hot Towel Shave at Iron Razor.",
                    "price": 260.0,
                    "duration_min": 35,
                    "duration_max": 35,
                },
            ],
        },
        {
            "id": "55555555-5555-5555-5555-555555555552",
            "name": "Precision Cuts",
            "services": [
                {
                    "id": "66666666-6666-6666-6666-666666666660",
                    "name": "The Gentleman's Cut",
                    "description": "The Gentleman's Cut at Iron Razor.",
                    "price": 199.0,
                    "duration_min": 30,
                    "duration_max": 30,
                },
            ],
        },
    ],
}

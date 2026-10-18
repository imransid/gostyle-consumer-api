"""
The Package Details answer, described for the OpenAPI page.

Documentation only: the view writes plain dicts (packages.detail), and
nothing here validates or renders. EXAMPLE is a real answer (see the test
`ExampleIsRealTests` in test_package_detail.py).
"""

from rest_framework import serializers


class SalonPackageDetailIncludeSerializer(serializers.Serializer):
    """One row of What's Included."""

    service_id = serializers.UUIDField(help_text="The service the booking flow would preselect.")
    name = serializers.CharField()
    price = serializers.FloatField(
        help_text="ONE of this service, at this branch's price, before VAT."
    )
    quantity = serializers.IntegerField(
        help_text="How many of it the package holds. 1 almost always."
    )


class SalonPackageDetailStepSerializer(serializers.Serializer):
    """One step of the visit timeline."""

    service_id = serializers.UUIDField()
    name = serializers.CharField()
    offset_minutes = serializers.IntegerField(
        help_text=(
            "Minutes from the start of the visit: 0 for the first step. Each "
            "step starts when the ones before it end; every step is 0 when "
            "the salon does them at the same time."
        )
    )
    duration_minutes = serializers.IntegerField(help_text="One of this service, in minutes.")


class SalonPackageDetailResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    salon_id = serializers.UUIDField()
    name = serializers.CharField()
    description = serializers.CharField(allow_null=True, help_text="Plain text. Null hides the line.")
    price = serializers.FloatField(help_text="The package price, before VAT. The same at every branch.")
    price_before = serializers.FloatField(
        allow_null=True,
        help_text=(
            "The included services booked separately at this branch, each "
            "times its quantity. Null when that is not more than `price`."
        ),
    )
    save_amount = serializers.FloatField(
        allow_null=True,
        help_text="Always price_before - price. Null with price_before.",
    )
    save_percent = serializers.IntegerField(
        allow_null=True,
        help_text="save_amount / price_before, a whole number, at least 1. Null with price_before.",
    )
    duration_minutes = serializers.IntegerField(
        allow_null=True,
        help_text="The included services' minutes added up, each times its quantity.",
    )
    is_active = serializers.BooleanField(
        help_text=(
            "On sale at this salon now, and every included service can be "
            "booked here. False hides Book with this Package; where "
            "`can_book` is sent, it decides the button."
        )
    )
    includes = SalonPackageDetailIncludeSerializer(many=True)
    timeline = SalonPackageDetailStepSerializer(many=True)
    terms = serializers.CharField(
        allow_null=True,
        help_text="The salon's own cancellation policy for this package, as typed. Plain text.",
    )
    theme = serializers.CharField(
        allow_null=True, help_text="The same colour theme the Packages tab row has."
    )
    can_book = serializers.BooleanField(
        required=False,
        help_text=(
            "Sent only while PACKAGE_BOOKING_V1 is on (absent otherwise). True: "
            "it can be booked here now as one visit at `price`, so show Book "
            "with this Package. Never true when `is_active` is false. "
            "docs/BOOKING_CREATE_API.md, Booking a package."
        ),
    )


# The real answer for the seeded salon's package, published with three
# services (one at this branch's own price, 150 against 180) and a policy.
# Captured from the running code on 2026-10-15; the ids are test data.
EXAMPLE = {
    "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
    "salon_id": "33333333-3333-3333-3333-333333333333",
    "name": "Groom and Go",
    "description": "Leave sharp, stay sharp: a cut, a scalp treatment and a hot towel shave.",
    "price": 520.0,
    "price_before": 609.0,
    "save_amount": 89.0,
    "save_percent": 15,
    "duration_minutes": 85,
    "is_active": True,
    "includes": [
        {
            "service_id": "66666666-6666-6666-6666-666666666660",
            "name": "The Gentleman's Cut",
            "price": 199.0,
            "quantity": 1,
        },
        {
            "service_id": "66666666-6666-6666-6666-666666666663",
            "name": "Scalp Treatment",
            "price": 150.0,
            "quantity": 1,
        },
        {
            "service_id": "66666666-6666-6666-6666-666666666662",
            "name": "Hot Towel Shave",
            "price": 260.0,
            "quantity": 1,
        },
    ],
    "timeline": [
        {
            "service_id": "66666666-6666-6666-6666-666666666660",
            "name": "The Gentleman's Cut",
            "offset_minutes": 0,
            "duration_minutes": 30,
        },
        {
            "service_id": "66666666-6666-6666-6666-666666666663",
            "name": "Scalp Treatment",
            "offset_minutes": 30,
            "duration_minutes": 20,
        },
        {
            "service_id": "66666666-6666-6666-6666-666666666662",
            "name": "Hot Towel Shave",
            "offset_minutes": 50,
            "duration_minutes": 35,
        },
    ],
    "terms": "Free to cancel up to 24 hours before your visit. After that, the deposit is kept.",
    "theme": "gold",
}

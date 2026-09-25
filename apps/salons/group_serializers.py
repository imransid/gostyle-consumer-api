"""
What the app may send to the two group routes.

UNLIKE `POST /booking`, THESE VALIDATE. The single booking forwards the app's
bytes to booking-api untouched and lets that service's 422 be the only
answer, because its payload IS booking-api's contract. A group is not: the
app speaks `members[]` and booking-api speaks `participants[]`, so this
service rebuilds the body from scratch, and a body it cannot read is a body
it cannot rebuild. Refusing here is also the only way to say so in this
project's envelope rather than as booking-api's 400 about fields the app
never sent.

The codes are the app's contract (docs/BOOKING_GROUP_API.md §7):
`invalid_party_size`, `member_no_services`, `invalid_member_kind`,
`member_id_required`. The ones that need the database -- `unknown_user`,
`foreign_id`, `stylist_mismatch` -- are group_views.py's.

Fields the app sends and nothing here uses -- the money figures, the promo
code, the statuses -- are simply not declared, and DRF drops them. The
server decides every one of them (§5).
"""

import uuid
from datetime import datetime

from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail

# Mirrors booking-api's own bounds for an online party (settings
# groups.minParticipants / maxParticipants).
MIN_MEMBERS = 2
MAX_MEMBERS = 8

SELF, REGISTERED, GUEST = "self", "registered", "guest"
ADULT, CHILD = "adult", "child"


def _refusal(message, code, field=None):
    detail = ErrorDetail(message, code=code)
    return serializers.ValidationError({field: [detail]} if field else detail)


def _canonical_id(value):
    """A uuid in its one spelling, so an upper-case id still matches the
    token's. Anything that is not a uuid is kept as sent: it matches no
    account, and that is the answer it gets."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return text


@extend_schema_field(OpenApiTypes.DATETIME)
class OffsetDateTimeField(serializers.Field):
    """
    An ISO 8601 instant WITH an offset, as an aware datetime.

    DRF's DateTimeField quietly reads a naive time in the server's timezone
    (UTC here), which for a salon in Dubai is four hours out and says so
    nowhere. A start time without an offset names no moment, so it is
    refused rather than guessed.
    """

    default_error_messages = {
        "invalid": "Use an ISO 8601 date-time, e.g. 2026-10-11T15:00:00+04:00.",
        "offset_required": "Include the UTC offset, e.g. +04:00.",
    }

    def to_internal_value(self, data):
        try:
            value = datetime.fromisoformat(data) if isinstance(data, str) else None
        except ValueError:
            value = None
        if value is None:
            self.fail("invalid")
        if value.tzinfo is None:
            self.fail("offset_required")
        return value

    def to_representation(self, value):
        return value.isoformat()


class PartySerializer(serializers.ListSerializer):
    """
    `members`, sized BEFORE any member is read.

    DRF's own min_length / max_length answer `min_length`; the app branches
    on `invalid_party_size`. And a party of fifty is refused without
    validating fifty.
    """

    def to_internal_value(self, data):
        if isinstance(data, list) and not MIN_MEMBERS <= len(data) <= MAX_MEMBERS:
            raise _refusal(
                f"A group booking is for {MIN_MEMBERS} to {MAX_MEMBERS} people.",
                "invalid_party_size",
            )
        members = super().to_internal_value(data)
        refs = [m["ref"] for m in members]
        if len(set(refs)) != len(refs):
            # Every answer is matched back to its row by ref. Two rows with
            # one ref would each be given the other's stylist.
            raise _refusal("Every member needs its own ref.", "duplicate_ref")
        return members


MAX_PRODUCT_QUANTITY = 99


def _product_line(line):
    """
    One product line as booking-api's POST /v1/mobile-booking/group takes it.

    Shape only: whether the variant is sold here, at that price and in stock,
    is booking-api's answer (it asks the platform catalogue), in the same
    words it gives a single booking.
    """
    if not isinstance(line, dict):
        raise serializers.ValidationError("Each product is {id, amount, quantity}.", code="invalid")
    try:
        variant = str(uuid.UUID(str(line.get("id"))))
    except ValueError:
        raise serializers.ValidationError(
            "A product's id is its variant id from GET /salon/<id>/products.",
            code="unknown_product",
        ) from None
    amount = line.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0:
        raise serializers.ValidationError("A product's amount is the price shown.", code="invalid")
    quantity = line.get("quantity", 1)
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_PRODUCT_QUANTITY:
        raise serializers.ValidationError(
            f"A product's quantity is a whole number from 1 to {MAX_PRODUCT_QUANTITY}.",
            code="invalid",
        )
    return {"id": variant, "amount": amount, "quantity": quantity}


def _no_services():
    return _refusal("Pick at least one service for every member.", "member_no_services")


def _stylist_or_none(attrs):
    return str(attrs["stylist_id"]) if attrs.get("stylist_id") else None


# ------------------------------------------------------------ availability


class AvailabilityMemberSerializer(serializers.Serializer):
    ref = serializers.IntegerField(
        min_value=0,
        help_text="The app's index for this member. Echoed back in `plan`.",
    )
    service_ids = serializers.ListField(
        child=serializers.UUIDField(),
        help_text="The services this member picked. Empty is `member_no_services`.",
    )
    stylist_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="A stylist this member wants. Null or left out: the salon picks.",
    )

    class Meta:
        list_serializer_class = PartySerializer

    def validate_service_ids(self, value):
        if not value:
            raise _no_services()
        return value

    def validate(self, attrs):
        attrs["service_ids"] = [str(s) for s in attrs["service_ids"]]
        attrs["stylist_id"] = _stylist_or_none(attrs)
        return attrs


_SALON_ID_HELP = (
    "The salon id this service already gave you -- the storefront uuid from "
    "/discover or /salon/<id>. The branch and tenant booking-api needs are "
    "resolved from it here."
)


class GroupAvailabilityRequestSerializer(serializers.Serializer):
    salon_id = serializers.UUIDField(help_text=_SALON_ID_HELP)
    date = serializers.DateField(help_text="The salon-local day.")
    members = AvailabilityMemberSerializer(many=True)


# ------------------------------------------------------------ booking


class GroupServiceLineSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The service's id from GET /salon/<id>/services.")
    amount = serializers.JSONField(
        required=False,
        help_text=(
            "Accepted and IGNORED. The price is the salon's, read on the "
            "server; a figure from the app is never forwarded or trusted."
        ),
    )


class BookingMemberSerializer(serializers.Serializer):
    ref = serializers.IntegerField(
        min_value=0,
        help_text="The app's index for this member, unique in the payload. Echoed back.",
    )
    kind = serializers.ChoiceField(
        choices=[SELF, REGISTERED, GUEST],
        help_text=(
            "`self` is the signed-in booker (exactly one); `registered` an "
            "account found with GET /user/lookup; `guest` a name only."
        ),
    )
    id = serializers.CharField(
        max_length=64,
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text=(
            "The member's user id. `self`: the token's own. `registered`: "
            "the id GET /user/lookup returned. `guest`: ignored."
        ),
    )
    name = serializers.CharField(
        max_length=80,
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text=(
            "Required for a guest. For `self` and `registered` the account's "
            "own name is used, and this only fills in for one that has none."
        ),
    )
    age_group = serializers.ChoiceField(
        choices=[ADULT, CHILD],
        help_text=(
            "With GROUP_BOOKING_V2 on, booking-api prices it: a child pays "
            "half of each service. Off, it is only echoed back."
        ),
    )
    services = GroupServiceLineSerializer(many=True)
    products = serializers.ListField(
        child=serializers.JSONField(),
        required=False,
        default=list,
        help_text=(
            "With GROUP_BOOKING_V2 on: `{id, amount, quantity}` per line, `id` "
            "the VARIANT id from GET /salon/<id>/products, as POST /booking "
            "takes them. Off: must be empty, because the old path carries no "
            "products and dropping them silently would leave a customer "
            "believing they bought something."
        ),
    )
    packages = serializers.ListField(
        child=serializers.JSONField(),
        required=False,
        default=list,
        help_text="Must be empty, for the same reason as products.",
    )
    stylist_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="A stylist this member wants. Null or left out: the salon assigns one.",
    )

    class Meta:
        list_serializer_class = PartySerializer

    def validate_services(self, value):
        if not value:
            raise _no_services()
        return value

    def validate_products(self, value):
        if not value:
            return value
        if not settings.GROUP_BOOKING_V2:
            raise serializers.ValidationError(
                "Group bookings cannot include products yet. Remove them to continue.",
                code="products_not_supported",
            )
        return [_product_line(line) for line in value]

    def validate_packages(self, value):
        if value:
            raise serializers.ValidationError(
                "Group bookings cannot include packages yet. Choose the services instead.",
                code="packages_not_supported",
            )
        return value

    def validate(self, attrs):
        kind = attrs["kind"]
        member_id = _canonical_id(attrs.get("id"))
        name = (attrs.get("name") or "").strip() or None

        if kind in (SELF, REGISTERED) and member_id is None:
            raise _refusal(
                "This member needs the id of their account.", "member_id_required", "id"
            )
        if kind == GUEST and name is None:
            raise _refusal("A guest needs a name.", "required", "name")

        # A guest's id is dropped, never looked up: someone without an
        # account is a name, and an id riding along would file their booking
        # under whoever it happens to belong to.
        attrs["id"] = None if kind == GUEST else member_id
        attrs["name"] = name
        attrs["service_ids"] = [str(line["id"]) for line in attrs["services"]]
        attrs["stylist_id"] = _stylist_or_none(attrs)
        return attrs


class GroupBookingRequestSerializer(serializers.Serializer):
    """
    Needs `booker_id` in its context: the signed-in account, which the one
    `self` member must be.
    """

    salon_id = serializers.UUIDField(help_text=_SALON_ID_HELP)
    date = serializers.DateField(help_text="The salon-local day. Must be the date of `start_time`.")
    start_time = OffsetDateTimeField(
        help_text=(
            "When the party arrives: everyone starts together. One of the "
            "`available` starts from /booking/group-availability. Must fall "
            "on `date`."
        ),
    )
    members = BookingMemberSerializer(many=True)

    def validate_members(self, members):
        selves = [m for m in members if m["kind"] == SELF]
        if len(selves) != 1:
            raise _refusal(
                "Exactly one member must be you (kind: self).", "invalid_member_kind"
            )
        if selves[0]["id"] != _canonical_id(str(self.context["booker_id"])):
            raise _refusal(
                "The member marked self must be the signed-in account.",
                "invalid_member_kind",
            )

        ids = [m["id"] for m in members if m["id"]]
        if len(set(ids)) != len(ids):
            raise _refusal(
                "Each account can be in the party only once.", "invalid_member_kind"
            )
        return members

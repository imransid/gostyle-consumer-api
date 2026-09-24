"""
What the app may send to the group routes.

UNLIKE `POST /booking`, THESE VALIDATE. The single booking forwards the app's
bytes to booking-api untouched and lets that service's 422 be the only
answer, because its payload IS booking-api's contract. A group is not: the
app speaks `members[]` and booking-api speaks `participants[]`, so this
service rebuilds the body from scratch, and a body it cannot read is a body
it cannot rebuild. Refusing here is also the only way to say so in this
project's envelope rather than as booking-api's 400 about fields the app
never sent.

The codes are the app's contract: `invalid_party_size`, `duplicate_ref`,
`member_no_services`. The ones that need the database -- `foreign_id`,
`stylist_mismatch`, `stylist_repeated` -- are group_views.py's.
"""

from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail

# Mirrors booking-api's own bounds for an online party (settings
# groups.minParticipants / maxParticipants).
MIN_MEMBERS = 2
MAX_MEMBERS = 8

def _refusal(message, code, field=None):
    detail = ErrorDetail(message, code=code)
    return serializers.ValidationError({field: [detail]} if field else detail)


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

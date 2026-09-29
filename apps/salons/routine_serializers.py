"""
What the app may send to the routine contract's routes
(docs/routine-booking-fe-contract.md).

ONLY WHAT THIS SERVICE MUST READ IS CHECKED HERE, as series_serializers.py
does for the old routes: the ids it looks up (salon, services, stylist) and
that the rest is there. start_time and cadence are the translator's
(routine_contract.py: invalid_time, invalid_cadence); the routine's own
rules (2 to 6 sessions, the days, the stylist's diary) are booking-api's,
answered in our envelope.
"""

from rest_framework import serializers


class RoutinePreviewRequestSerializer(serializers.Serializer):
    salon_id = serializers.UUIDField()
    service_ids = serializers.ListField(
        child=serializers.UUIDField(), help_text="At least one. The same services every session.",
    )
    stylist_id = serializers.UUIDField(
        required=False, allow_null=True,
        help_text="Null or left out: Any Available Expert, the server picks one for the whole plan.",
    )
    start_time = serializers.CharField(
        help_text="ISO 8601 with an offset: the first session, an offered start.",
    )
    cadence = serializers.CharField(help_text="week, fortnight or month.")
    sessions = serializers.IntegerField(help_text="2 to 6.")

    def validate_service_ids(self, value):
        if not value:
            raise serializers.ValidationError("Pick at least one service.", code="no_services")
        return value


class RoutineContractServiceSerializer(serializers.Serializer):
    """One service of the create. Its `amount` is echoed, never checked (Q5)."""

    id = serializers.UUIDField()


class RoutineCreateRequestSerializer(serializers.Serializer):
    """
    POST /booking/routine. The payment fields (section 4) and the four totals
    are not declared: routine_contract.check_payment reads them as the app
    sent them, and booking-api checks the totals (amount_mismatch).
    """

    salon_id = serializers.UUIDField()
    cadence = serializers.CharField(help_text="week, fortnight or month.")
    services = RoutineContractServiceSerializer(many=True)
    stylist_id = serializers.UUIDField(
        required=False, allow_null=True,
        help_text="The stylist the preview showed. Null: the server picks again, the same way.",
    )
    start_time = serializers.CharField(
        help_text="The preview's own start_time: the anchor of the cadence (Q1).",
    )
    sessions = serializers.ListField(
        child=serializers.DictField(),
        help_text="index, date, start_time, end_time: each its cadence slot or an alternative.",
    )

    def validate_services(self, value):
        if not value:
            raise serializers.ValidationError("Pick at least one service.", code="no_services")
        return value


class RoutineMoveRequestSerializer(serializers.Serializer):
    """
    PATCH /booking/<id>/sessions/<session_id> (draft section 6). dry_run is
    checked by the view as the old cancel checks it: true or false only.
    """

    start_time = serializers.CharField(help_text="ISO 8601 with an offset: the new start.")
    stylist_id = serializers.UUIDField(
        required=False, allow_null=True,
        help_text="Left out: the session keeps its stylist.",
    )

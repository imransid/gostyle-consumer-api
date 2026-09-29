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

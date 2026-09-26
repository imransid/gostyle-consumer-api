"""
What the app may send to POST /api/v1/booking/series.

ONLY WHAT THIS SERVICE MUST READ IS CHECKED HERE: the shapes it converts
(every day and time goes onto booking-api's clock, so a time that is not
HH:MM cannot be converted) and the ids it looks up (salon, services,
stylist). THE ROUTINE'S OWN RULES ARE booking-api's (2 to 6 sessions, the
frequencies, the 90 day horizon, the plans, the picks): it answers them in
the app's envelope, and a second copy here would be the one that lags
(docs/SERIES_BOOKING_AUDIT.md, E.3).

The money figures and the products are not declared: they are forwarded as
the app sent them, for booking-api to check.
"""

import re

from rest_framework import serializers

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

TIME_HELP = "The salon's own clock, HH:MM (for example 16:30)."


class RoutineTimeField(serializers.CharField):
    """HH:MM on a 24 hour clock. The 5 minute grid is booking-api's rule."""

    default_error_messages = {
        "invalid_time": "Use HH:MM on a 24 hour clock, for example 16:30.",
    }

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if not _HHMM.match(value):
            self.fail("invalid_time")
        return value


class RoutineServiceSerializer(serializers.Serializer):
    id = serializers.UUIDField()


class RoutinePickSerializer(serializers.Serializer):
    """D4: the alternative the customer chose for one busy session."""

    index = serializers.IntegerField(min_value=0)
    date = serializers.DateField()
    time = RoutineTimeField(help_text=TIME_HELP)
    stylist_id = serializers.UUIDField(required=False, allow_null=True)


class SeriesBookingRequestSerializer(serializers.Serializer):
    dry_run = serializers.BooleanField(
        required=False, default=False,
        help_text="true: a preview, nothing is saved. Send no Idempotency-Key with it.",
    )
    salon_id = serializers.UUIDField()
    services = RoutineServiceSerializer(many=True, help_text="One or more (D1).")
    stylist_id = serializers.UUIDField(required=False, allow_null=True)
    frequency = serializers.CharField(
        help_text="DAILY, WEEKLY, EVERY_2_WEEKS, MONTHLY or CUSTOM.",
    )
    start_date = serializers.DateField(required=False, allow_null=True)
    sessions = serializers.IntegerField(required=False, allow_null=True)
    dates = serializers.ListField(
        child=serializers.DateField(), required=False, allow_null=True,
        help_text="CUSTOM only.",
    )
    time = RoutineTimeField(
        required=False, allow_null=True,
        help_text=TIME_HELP + " Leave it out of a dry run to get the free times.",
    )
    payment_plan = serializers.CharField(
        help_text="PAY_AT_SALON, PAY_AS_YOU_GO or UPFRONT. Only PAY_AT_SALON books.",
    )
    picks = RoutinePickSerializer(many=True, required=False)

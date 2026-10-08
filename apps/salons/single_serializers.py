"""
What the app may send to POST /api/v1/booking/<id>/reschedule for a SINGLE
booking (SINGLE_BOOKING_ACTIONS_V1).

Only the new time and, if it changes, the stylist. Everything else the move
needs (the salon, the services, the stylist kept) comes from the booking
itself, and the body booking-api gets is built in single_views.py. A key the
app sends that is not here is ignored, never forwarded.
"""

from rest_framework import serializers

from .series_serializers import TIME_HELP, RoutineTimeField


class SingleRescheduleRequestSerializer(serializers.Serializer):
    date = serializers.DateField(help_text="The salon's own day, YYYY-MM-DD.")
    time = RoutineTimeField(help_text=TIME_HELP)
    stylist_id = serializers.UUIDField(
        required=False, allow_null=True,
        help_text="Left out or null: the booking keeps its own stylist.",
    )

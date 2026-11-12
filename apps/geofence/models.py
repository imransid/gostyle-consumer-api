from django.conf import settings
from django.db import models
from django.utils import timezone


class GeofenceState(models.Model):
    """
    Where one customer stands against one ring of one salon on one day.

    ONE ROW PER (customer, branch, salon day, ring). It remembers whether the
    customer is inside (so entering and leaving can be told apart from being
    there), and whether this ring's alert has gone out, which it does once
    per salon per day: leaving and coming back says nothing new.

    No coordinates are kept. A reading is measured and thrown away; only the
    distance to the salon, rounded to the metre, stays for the log.
    """

    account = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="geofence_states",
    )
    # booking-api's branch id: the salon's premises the visit is at.
    branch_id = models.UUIDField()
    # The visit's day on the salon's clock (booking-api's trading_day).
    trading_day = models.DateField()
    # apps.geofence.zones.ZONES: arrival, auto_trigger or primary.
    zone = models.CharField(max_length=20)
    # The visit the last reading was measured for.
    booking_id = models.UUIDField()

    inside = models.BooleanField(default=False)
    entered_at = models.DateTimeField(null=True, blank=True)
    exited_at = models.DateTimeField(null=True, blank=True)
    # Set once, when the ring's alert went out or was made pointless by an
    # inner ring's. Never cleared: it is what stops a second alert.
    notified_at = models.DateTimeField(null=True, blank=True)
    # Why the last reading inside did not alert ("visit_started", ...), or
    # "inner_ring_first" for a ring passed in silence. Empty once alerted.
    suppressed_reason = models.CharField(max_length=40, blank=True, default="")

    last_distance_m = models.PositiveIntegerField(null=True, blank=True)
    last_reading_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "geofence_state"
        constraints = [
            models.UniqueConstraint(
                fields=["account", "branch_id", "trading_day", "zone"],
                name="geofence_state_once",
            )
        ]
        indexes = [models.Index(fields=["account", "trading_day"], name="geofence_state_day")]

    def __str__(self):
        return f"{self.zone} {'in' if self.inside else 'out'} {self.branch_id} {self.trading_day}"

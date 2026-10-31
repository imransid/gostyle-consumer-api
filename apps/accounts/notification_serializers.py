from django.conf import settings
from django.db import models
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import NotificationPreference
from .notifications import verified_email

PREFERENCE_FIELDS = (
    "booking_confirmation",
    "appointment_reminder",
    "booking_changes",
    "special_offers",
    "new_salons",
    "loyalty",
    "push",
)
# Taken only while NOTIFICATION_CHANNELS_V1 is on.
CHANNEL_FIELDS = ("email", "sms")
# Taken only while REMINDER_TIMING_V1 is on.
TIMING_FIELDS = ("reminder_lead_minutes",)
REMINDER_LEADS = (30, 60, 120, 1440)


def writable_fields():
    """The preferences this server takes right now, by its switches. Off,
    a key is as unknown as a typo, which is what it was before."""
    fields = PREFERENCE_FIELDS
    if settings.NOTIFICATION_CHANNELS_V1:
        fields += CHANNEL_FIELDS
    if settings.REMINDER_TIMING_V1:
        fields += TIMING_FIELDS
    return fields


class StrictBooleanField(serializers.BooleanField):
    """Only real JSON true/false. DRF's default also accepts "true", "yes", 1."""

    default_error_messages = {"invalid": "Must be a boolean."}

    def to_internal_value(self, data):
        if isinstance(data, bool):
            return data
        self.fail("invalid")


class ReminderLeadField(serializers.IntegerField):
    """One of the four times the screen offers, as a JSON number. Not "60",
    not 60.0, not true."""

    default_error_messages = {"invalid": "Choose 30, 60, 120 or 1440."}

    def to_internal_value(self, data):
        if type(data) is int and data in REMINDER_LEADS:
            return data
        self.fail("invalid")


class ChannelsAvailableSerializer(serializers.Serializer):
    push = serializers.BooleanField()
    email = serializers.BooleanField()
    sms = serializers.BooleanField()


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    serializer_field_mapping = {
        **serializers.ModelSerializer.serializer_field_mapping,
        models.BooleanField: StrictBooleanField,
    }
    reminder_lead_minutes = ReminderLeadField(required=False)
    # Read-only: whether each channel can reach this customer at all, so the
    # app can grey a switch out. Sent back in a PATCH, it is ignored.
    channels_available = serializers.SerializerMethodField()

    class Meta:
        model = NotificationPreference
        fields = (*PREFERENCE_FIELDS, *CHANNEL_FIELDS, *TIMING_FIELDS, "channels_available")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        shown = set(self._accepted_keys())
        for name in list(self.fields):
            if name not in shown:
                self.fields.pop(name)

    @staticmethod
    def _accepted_keys():
        keys = writable_fields()
        if settings.NOTIFICATION_CHANNELS_V1:
            keys += ("channels_available",)
        return keys

    def to_internal_value(self, data):
        # Unknown keys are refused before anything else is looked at, so a
        # typo like "specail_offers" can never be silently ignored.
        if isinstance(data, dict):
            accepted = self._accepted_keys()
            unknown = [key for key in data if key not in accepted]
            if unknown:
                raise serializers.ValidationError(
                    {
                        key: [
                            serializers.ErrorDetail(
                                "This notification preference is not supported.",
                                code="unknown_field",
                            )
                        ]
                        for key in unknown
                    }
                )
        return super().to_internal_value(data)

    def validate(self, attrs):
        # Only turning a channel ON needs anything. Off always works, and a
        # switch sent back unchanged (an app echoing the object) is not
        # asked again.
        if attrs.get("email") and not self.instance.email:
            self._require_verified_email()
        if attrs.get("sms") and not self.instance.sms:
            self._require_sms()
        return attrs

    def _require_verified_email(self):
        account = self.instance.account
        if not account.email:
            self._refuse("email", "no_email_on_account", "Add an email address first.")
        if verified_email(account) is None:
            self._refuse("email", "email_not_verified", "Verify your email address first.")

    def _require_sms(self):
        account = self.instance.account
        if not account.phone:
            self._refuse("sms", "no_phone_on_account", "Add a phone number first.")
        if account.phone_verified_at is None:
            self._refuse("sms", "phone_not_verified", "Verify your phone number first.")
        # No SMS provider yet (docs/NOTIFICATIONS_SETTINGS_AUDIT.md, D8).
        self._refuse("sms", "sms_unavailable", "Text messages are not available yet.")

    @staticmethod
    def _refuse(field, code, message):
        raise serializers.ValidationError({field: [serializers.ErrorDetail(message, code=code)]})

    @extend_schema_field(ChannelsAvailableSerializer)
    def get_channels_available(self, prefs):
        return {
            "push": bool(settings.PUSH_API_KEY),
            "email": verified_email(prefs.account) is not None,
            "sms": False,
        }

    def update(self, instance, validated_data):
        # Write only the columns that were sent, so two quick PATCHes to
        # different fields cannot overwrite each other.
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if validated_data:
            instance.save(update_fields=[*validated_data, "updated_at"])
        return instance

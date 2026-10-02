from django.db import models
from rest_framework import serializers

from .models import NotificationPreference

PREFERENCE_FIELDS = (
    "booking_confirmation",
    "appointment_reminder",
    "booking_changes",
    "special_offers",
    "new_salons",
    "loyalty",
    "push",
)


class StrictBooleanField(serializers.BooleanField):
    """Only real JSON true/false. DRF's default also accepts "true", "yes", 1."""

    default_error_messages = {"invalid": "Must be a boolean."}

    def to_internal_value(self, data):
        if isinstance(data, bool):
            return data
        self.fail("invalid")


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    serializer_field_mapping = {
        **serializers.ModelSerializer.serializer_field_mapping,
        models.BooleanField: StrictBooleanField,
    }

    class Meta:
        model = NotificationPreference
        fields = PREFERENCE_FIELDS

    def to_internal_value(self, data):
        # Unknown keys are refused before anything else is looked at, so a
        # typo like "specail_offers" can never be silently ignored.
        if isinstance(data, dict):
            unknown = [key for key in data if key not in PREFERENCE_FIELDS]
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

    def update(self, instance, validated_data):
        # Write only the columns that were sent, so two quick PATCHes to
        # different fields cannot overwrite each other.
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if validated_data:
            instance.save(update_fields=[*validated_data, "updated_at"])
        return instance
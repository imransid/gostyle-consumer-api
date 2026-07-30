from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .identifiers import InvalidDestination, normalize_destination
from .models import ConsumerAccount, DestinationType, Purpose, Gender


class DestinationMixin:
    """Normalizes `destination` against `destination_type` for every serializer
    that accepts a contact. Runs in validate() so both fields are present.
    """

    def _normalize(self, attrs):
        try:
            attrs["destination"] = normalize_destination(
                attrs["destination"], attrs["destination_type"]
            )
        except InvalidDestination as exc:
            raise serializers.ValidationError({"destination": str(exc)})
        return attrs


class OtpRequestSerializer(DestinationMixin, serializers.Serializer):
    destination_type = serializers.ChoiceField(choices=DestinationType.choices)
    destination = serializers.CharField(max_length=254)
    purpose = serializers.ChoiceField(choices=Purpose.choices)

    def validate(self, attrs):
        return self._normalize(attrs)


class OtpVerifySerializer(DestinationMixin, serializers.Serializer):
    destination_type = serializers.ChoiceField(choices=DestinationType.choices)
    destination = serializers.CharField(max_length=254)
    purpose = serializers.ChoiceField(choices=Purpose.choices)
    code = serializers.RegexField(r"^[0-9]{6}\Z")

    def validate(self, attrs):
        return self._normalize(attrs)

class RegisterResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()
    access = serializers.CharField()
    refresh = serializers.CharField()

class RegisterSerializer(DestinationMixin, serializers.Serializer):
    # No token: register names the contact it verified, and the service checks
    # there is a recent, unused Verification for it.
    destination_type = serializers.ChoiceField(choices=DestinationType.choices)
    destination = serializers.CharField(max_length=254)
    full_name = serializers.CharField(max_length=120)
    password = serializers.CharField(write_only=True)
    confirm_password = serializers.CharField(write_only=True)
    gender = serializers.ChoiceField(choices=Gender.choices)

    def validate(self, attrs):
        attrs = self._normalize(attrs)
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError(
                {"confirm_password": "Passwords do not match."}
            )
        try:
            validate_password(attrs["password"])
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)})
        return attrs


class LoginSerializer(DestinationMixin, serializers.Serializer):
    destination_type = serializers.ChoiceField(choices=DestinationType.choices)
    destination = serializers.CharField(max_length=254)
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        return self._normalize(attrs)


class ProfileSerializer(serializers.ModelSerializer):
    # Live/derived values — NOT stored on the profile, exposed read-only.
    points = serializers.SerializerMethodField()
    tier_level = serializers.SerializerMethodField()
    push_token = serializers.SerializerMethodField()

    class Meta:
        model = ConsumerAccount
        fields = [
            "id",
            "full_name",
            "email",
            "phone",
            "phone_verified_at",
            "email_verified_at",
            "created_at",
            # preferences (editable via PATCH)
            "image",
            "location",
            "language",
            "currency",
            "is_hijab_mode",
            # live / derived (read-only)
            "points",
            "tier_level",
            "push_token",
        ]
        read_only_fields = [
            "id",
            "email",
            "phone",
            "phone_verified_at",
            "email_verified_at",
            "created_at",
            "points",
            "tier_level",
            "push_token",
        ]

    def get_points(self, obj):
        # TODO: wire to the loyalty app when it exists. No loyalty system yet.
        return 0

    def get_tier_level(self, obj):
        # TODO: derive from points once the loyalty app exists.
        return "BRONZE"

    def get_push_token(self, obj):
        device = (
            obj.devices.filter(is_active=True)
            .order_by("-last_seen_at")
            .first()
        )
        return device.push_token if device else None

class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()


# --- response shapes (for OpenAPI docs) ---------------------------------
class TokenPairSerializer(serializers.Serializer):
    access = serializers.CharField()
    refresh = serializers.CharField()


class DetailSerializer(serializers.Serializer):
    detail = serializers.CharField()


class OtpRequestedSerializer(serializers.Serializer):
    detail = serializers.CharField()
    retry_after = serializers.IntegerField()


class OtpVerifiedSerializer(serializers.Serializer):
    verified = serializers.BooleanField()
    account_exists = serializers.BooleanField()

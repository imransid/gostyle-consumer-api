from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .identifiers import InvalidDestination, normalize_destination
from .models import ConsumerAccount, DestinationType, Purpose


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
        ]
        # Only the display name is editable here; changing email/phone must go
        # through a verification flow, not a plain profile PATCH.
        read_only_fields = [
            "id",
            "email",
            "phone",
            "phone_verified_at",
            "email_verified_at",
            "created_at",
        ]


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

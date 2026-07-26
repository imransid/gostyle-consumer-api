from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .identifiers import InvalidIdentifier, resolve_identifier
from .models import ConsumerAccount


def validate_new_password(password, confirm):
    """Shared rule for register + reset: passwords match and pass the policy."""
    if password != confirm:
        raise serializers.ValidationError({"confirm_password": "Passwords do not match."})
    try:
        validate_password(password)
    except DjangoValidationError as exc:
        raise serializers.ValidationError({"password": list(exc.messages)})


class IdentifierSerializer(serializers.Serializer):
    """Base for any endpoint whose input is a single email-or-phone field.

    On success, validated_data gains `kind` ("email"/"phone") and the
    normalized `destination`.
    """

    identifier = serializers.CharField()

    def validate_identifier(self, value):
        try:
            self._kind, self._destination = resolve_identifier(value)
        except InvalidIdentifier as exc:
            raise serializers.ValidationError(str(exc))
        return value

    def validate(self, attrs):
        attrs["kind"] = self._kind
        attrs["destination"] = self._destination
        return attrs


class RegisterSerializer(IdentifierSerializer):
    full_name = serializers.CharField(max_length=120)
    password = serializers.CharField(write_only=True)
    confirm_password = serializers.CharField(write_only=True)
    accept_terms = serializers.BooleanField()

    def validate_accept_terms(self, value):
        if not value:
            raise serializers.ValidationError(
                "You must accept the Terms & Conditions and Privacy Policy."
            )
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        validate_new_password(attrs["password"], attrs["confirm_password"])
        return attrs


class VerifySerializer(IdentifierSerializer):
    code = serializers.CharField(min_length=6, max_length=6)


class ResendSerializer(IdentifierSerializer):
    pass


class LoginSerializer(IdentifierSerializer):
    password = serializers.CharField(write_only=True)


class ForgotPasswordSerializer(IdentifierSerializer):
    pass


class ResetPasswordSerializer(IdentifierSerializer):
    code = serializers.CharField(min_length=6, max_length=6)
    new_password = serializers.CharField(write_only=True)
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        validate_new_password(attrs["new_password"], attrs["confirm_password"])
        return attrs


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


class OtpSentSerializer(serializers.Serializer):
    detail = serializers.CharField()
    destination = serializers.CharField()
    channel = serializers.CharField(required=False)
    retry_after = serializers.IntegerField()
    expires_in = serializers.IntegerField(required=False)

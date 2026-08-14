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



class PasswordChangeSerializer(serializers.Serializer):
    """Logged-in change. The OLD password is the proof of identity here, so
    there is no OTP: the caller already holds a valid token AND knows the
    secret, which is two factors of the same account.
    """

    old_password = serializers.CharField(max_length=128, write_only=True)
    new_password = serializers.CharField(max_length=128, write_only=True)

    def validate_new_password(self, value):
        # Same validators as registration: 8+, uppercase, number, symbol.
        # Running them here rather than in the service keeps every password
        # rule in one place and the error shape consistent with register.
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value

    def validate(self, attrs):
        if attrs["old_password"] == attrs["new_password"]:
            raise serializers.ValidationError(
                {"new_password": "New password must differ from the old one."}
            )
        return attrs


class PasswordForgotSerializer(DestinationMixin, serializers.Serializer):
    """Step 1. No purpose field: it is always PASSWORD_RESET, and letting the
    client choose would allow a register-purpose code to be spent on a reset.
    """

    destination_type = serializers.ChoiceField(choices=DestinationType.choices)
    destination = serializers.CharField(max_length=254)

    def validate(self, attrs):
        return self._normalize(attrs)


class PasswordVerifySerializer(DestinationMixin, serializers.Serializer):
    """Step 2. Trades a correct code for a single-use reset token."""

    destination_type = serializers.ChoiceField(choices=DestinationType.choices)
    destination = serializers.CharField(max_length=254)
    code = serializers.RegexField(r"^[0-9]{6}\Z")

    def validate(self, attrs):
        return self._normalize(attrs)


class PasswordResetSerializer(serializers.Serializer):
    """Step 3. The reset token is the only proof; the destination is read from
    the stored Verification row rather than taken from the client, so a valid
    token cannot be redirected at someone else's account.
    """

    reset_token = serializers.UUIDField()
    new_password = serializers.CharField(max_length=128, write_only=True)

    def validate_new_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value


class PasswordResetTokenSerializer(serializers.Serializer):
    reset_token = serializers.UUIDField()
    expires_in = serializers.IntegerField()
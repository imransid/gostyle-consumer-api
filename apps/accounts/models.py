import uuid
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .managers import ConsumerAccountManager


class DestinationType(models.TextChoices):
    PHONE = "phone", "phone"
    EMAIL = "email", "email"


class Purpose(models.TextChoices):
    REGISTER = "register", "register"
    LOGIN = "login", "login"
    PASSWORD_RESET = "password_reset", "password_reset"


class ConsumerAccount(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Phone is optional: a member can register with email only. It stays unique
    # (multiple NULLs are allowed) and remains the USERNAME_FIELD for staff.
    phone = models.CharField(max_length=20, unique=True, null=True, blank=True, db_index=True)
   
    email = models.EmailField(unique=True, null=True, blank=True)
    full_name = models.CharField(max_length=120, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    phone_verified_at = models.DateTimeField(null=True, blank=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    # When the user accepted Terms & Conditions / Privacy Policy at sign-up.
    accepted_terms_at = models.DateTimeField(null=True, blank=True)

    account_verified = models.BooleanField(default=False,  blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ConsumerAccountManager()

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "consumer_account"

    def __str__(self):
        return self.phone or self.email or str(self.id)


class OtpCode(models.Model):
    MAX_ATTEMPTS = 5

    # Kept as class attributes for backwards-compatible references; the choices
    # live on the module-level DestinationType / Purpose enums.
    DestinationType = DestinationType
    Purpose = Purpose

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # The phone number (E.164) or email address the code was sent to.
    destination = models.CharField(max_length=254, db_index=True)
    destination_type = models.CharField(max_length=10, choices=DestinationType.choices)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    code_hash = models.CharField(max_length=128)

    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "otp_code"
        indexes = [
            # Lookups only ever want the newest live code for a
            # (destination, purpose) pair, so index exactly that and skip the
            # consumed rows entirely.
            models.Index(
                fields=["destination", "purpose", "-created_at"],
                name="otp_code_live_idx",
                condition=Q(consumed_at__isnull=True),
            ),
        ]

    def set_code(self, raw_code, ttl_seconds=300):
        self.code_hash = make_password(raw_code)
        self.expires_at = timezone.now() + timedelta(seconds=ttl_seconds)

    def check_code(self, raw_code):
        return check_password(raw_code, self.code_hash)

    @property
    def is_usable(self):
        return (
            self.consumed_at is None
            and self.expires_at > timezone.now()
            and self.attempts < self.MAX_ATTEMPTS
        )


class Verification(models.Model):
    """A recent successful OTP verification for a destination.

    Issued by verify_otp and redeemed once by register. It is how the
    verify-then-register flow proves the destination was verified without the
    client carrying a token: register looks up the newest usable row for the
    (destination, destination_type, purpose) it was asked to register.
    """

    TTL_SECONDS = 600  # 10 minutes

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    destination = models.CharField(max_length=254, db_index=True)
    destination_type = models.CharField(max_length=10, choices=DestinationType.choices)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "verification"
        indexes = [
            models.Index(
                fields=["destination", "destination_type", "purpose", "-created_at"],
                name="verification_live_idx",
                condition=Q(consumed_at__isnull=True),
            ),
        ]

    @classmethod
    def issue(cls, destination, destination_type, purpose):
        return cls.objects.create(
            destination=destination,
            destination_type=destination_type,
            purpose=purpose,
            expires_at=timezone.now() + timedelta(seconds=cls.TTL_SECONDS),
        )

    @property
    def is_usable(self):
        return self.consumed_at is None and self.expires_at > timezone.now()

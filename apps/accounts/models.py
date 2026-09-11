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


class Gender(models.TextChoices):
    MALE = 'male', 'male'
    FEMALE = 'female', 'female'

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


    image = models.URLField(blank=True)
    gender = models.CharField(max_length=30, choices=Gender.choices, blank=True, default="")
    location = models.CharField(max_length=255, blank=True)
    language = models.CharField(max_length=10, default="en")
    currency = models.CharField(max_length=3, default="AED")
    is_hijab_mode = models.BooleanField(default=False)

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



class Device(models.Model):
    """A push-notification target. One row per device, per member.

    A member can be logged in on several devices, so push tokens live here
    rather than as a single column on ConsumerAccount, which would silently
    drop every device but the last.
    """

    class Platform(models.TextChoices):
        IOS = "ios", "ios"
        ANDROID = "android", "android"
        WEB = "web", "web"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        ConsumerAccount,
        related_name="devices",
        on_delete=models.CASCADE,
    )
    push_token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=10, choices=Platform.choices)
    device_id = models.CharField(max_length=128, blank=True)
    is_active = models.BooleanField(default=True)

    last_seen_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "device"
        indexes = [
            models.Index(
                fields=["account", "-last_seen_at"],
                name="device_active_idx",
                condition=Q(is_active=True),
            ),
        ]

    def __str__(self):
        return f"{self.platform} device for {self.account_id}"


class Favourite(models.Model):
    """
    A salon a customer saved.

    storefront_id is a PLAIN UUID, not a ForeignKey. The salon lives in the
    platform database, which this service only reads, so a real foreign key
    would be a constraint across two databases that Postgres cannot enforce
    and Django cannot migrate.

    UNIQUE on (account, storefront_id): tapping the heart twice must not
    create two rows. The toggle relies on there being at most one.
    """

    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey(
        ConsumerAccount,
        on_delete=models.CASCADE,
        related_name="favourites",
    )
    storefront_id = models.UUIDField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "favourite"
        constraints = [
            models.UniqueConstraint(
                fields=["account", "storefront_id"],
                name="favourite_account_storefront_unique",
            )
        ]
        indexes = [
            models.Index(fields=["account", "-created_at"]),
        ]
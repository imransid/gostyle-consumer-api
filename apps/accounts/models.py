import uuid
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from .managers import ActiveConsumerAccountManager, ConsumerAccountManager


class DestinationType(models.TextChoices):
    PHONE = "phone", "phone"
    EMAIL = "email", "email"


class Gender(models.TextChoices):
    MALE = 'male', 'male'
    FEMALE = 'female', 'female'
    OTHER = 'other', 'other'

class Purpose(models.TextChoices):
    REGISTER = "register", "register"
    LOGIN = "login", "login"
    PASSWORD_RESET = "password_reset", "password_reset"
    CHANGE_CONTACT = "change_contact", "change_contact"


class DeletionReason(models.TextChoices):
    NOT_USING = "not_using", "Not using the app"
    PRIVACY = "privacy", "Privacy concerns"
    TOO_MANY_NOTIFICATIONS = "too_many_notifications", "Too many notifications"
    FOUND_ALTERNATIVE = "found_alternative", "Found an alternative"
    BAD_EXPERIENCE = "bad_experience", "Bad experience"
    OTHER = "other", "Other"

class ConsumerAccount(AbstractBaseUser, PermissionsMixin):
    PURGE_AFTER_DAYS = 30
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Phone is optional: a member can register with email only. It stays unique
    # (multiple NULLs are allowed) and remains the USERNAME_FIELD for staff.
    phone = models.CharField(max_length=20, unique=True, null=True, blank=True, db_index=True)
   
    email = models.EmailField(unique=True, null=True, blank=True)
    full_name = models.CharField(max_length=120, blank=True)

    # The person's row in the platform directory (public.customer).
    # A plain UUID, not a ForeignKey, for the reason Favourite.storefront_id
    # is one: that table belongs to the platform, not to this service.
    platform_customer_id = models.UUIDField(null=True, blank=True, db_index=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    phone_verified_at = models.DateTimeField(null=True, blank=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    # When the user accepted Terms & Conditions / Privacy Policy at sign-up.
    accepted_terms_at = models.DateTimeField(null=True, blank=True)

    account_verified = models.BooleanField(default=False,  blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ActiveConsumerAccountManager()   # only living accounts (the default)
    all_objects = ConsumerAccountManager()     # everyone, including deleted




    image = models.URLField(blank=True)
    gender = models.CharField(max_length=30, choices=Gender.choices, blank=True, default="")
    # A date, not a timestamp: a birthday has no time and no zone.
    date_of_birth = models.DateField(null=True, blank=True)
    location = models.CharField(max_length=255, blank=True)
    language = models.CharField(max_length=10, default="en")
    currency = models.CharField(max_length=3, default="AED")
    is_hijab_mode = models.BooleanField(default=False)

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deletion_reason = models.CharField(
        max_length=30, choices=DeletionReason.choices, blank=True, default=""
    )
    deletion_note = models.TextField(blank=True, default="")

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    purge_at = models.DateTimeField(null=True, blank=True, db_index=True)

    @property
    def is_pending_deletion(self):
        return self.purge_at is not None and self.deleted_at is None

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = []


    def request_deletion(self, reason=DeletionReason.OTHER, note=""):
        if self.is_deleted:
            return
        self.purge_at = timezone.now() + timedelta(days=self.PURGE_AFTER_DAYS)
        self.deletion_reason = reason
        self.deletion_note = note[:1000]
        self.save(update_fields=["purge_at", "deletion_reason", "deletion_note", "updated_at"])

    def cancel_deletion(self):
        if self.is_deleted:
            return
        self.purge_at = None
        self.deletion_reason = ""
        self.deletion_note = ""
        self.save(update_fields=["purge_at", "deletion_reason", "deletion_note", "updated_at"])


    @transaction.atomic
    def purge(self):
        if self.is_deleted:
            return

        # 1. Remember the old contacts before we clear them
        old_contacts = [c for c in (self.phone, self.email) if c]

        # 2. Mark as deleted and block login
        self.deleted_at = timezone.now()
        self.is_active = False
        self.set_unusable_password()

        # 3. Free the unique fields and clear personal data
        self.phone = None
        self.email = None
        self.phone_verified_at = None
        self.email_verified_at = None
        self.account_verified = False
        self.full_name = ""
        self.image = ""
        self.date_of_birth = None
        self.location = ""
        self.save()

        # 4. End every session. Without this a refresh token (30 days) reaches
        # simplejwt's refresh, whose objects.get() no longer finds the row and
        # answers 500. Blacklisted, it is a 401 before the lookup.
        # Imported here: services imports this module.
        from .services import _blacklist_all_refresh_tokens

        _blacklist_all_refresh_tokens(self)

        # 5. Clean related data
        self.devices.all().delete()
        self.sessions.all().delete()
        self.favourites.all().delete()
        self.favourite_stylists.all().delete()
        NotificationPreference.objects.filter(account=self).delete()
        if old_contacts:
            OtpCode.objects.filter(
                destination__in=old_contacts, consumed_at__isnull=True
            ).update(consumed_at=timezone.now())
            Verification.objects.filter(destination__in=old_contacts).delete()


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
    account = models.ForeignKey(
        "ConsumerAccount",
        on_delete=models.CASCADE,
        related_name="verifications",
        null=True,
        blank=True,
    )
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


class Session(models.Model):
    """A login session: one row per sign-in (or register), shown on
    GET /api/v1/me/sessions so the customer can see where they are signed in.

    Tied 1-to-1 with simplejwt's OutstandingToken: when the refresh token is
    blacklisted (logout, password reset, purge) the session stays for history
    but is_active turns false.

    The location comes from a GeoIP lookup of the IP address at sign-in (and
    is refreshed on token refresh when the IP changes).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        ConsumerAccount,
        related_name="sessions",
        on_delete=models.CASCADE,
    )

    # The IP the request arrived from (X-Real-IP from nginx, or REMOTE_ADDR).
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    # Raw User-Agent header, for building the device name.
    user_agent = models.TextField(blank=True, default="")
    # A human-friendly label built from user_agent, e.g. "iPhone 15".
    device_name = models.CharField(max_length=255, blank=True, default="")
    # City (or country) from a GeoIP lookup, e.g. "Dubai". None/null if not found.
    location = models.CharField(max_length=255, null=True, blank=True, default=None)
    # Full location from GeoIP, e.g. "Dubai, United Arab Emirates". None/null if not found.
    full_location = models.CharField(max_length=255, null=True, blank=True, default=None)

    created_at = models.DateTimeField(auto_now_add=True)
    # auto_now_add only — we bump it manually via save(update_fields=["last_seen_at"])
    # on token refresh, so it must NOT use auto_now (which ignores update_fields).
    last_seen_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "session"
        ordering = ["-last_seen_at"]

    def __str__(self):
        return f"{self.device_name or 'Unknown device'} — {self.account_id}"


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


class FavouriteStylist(models.Model):
    """
    A stylist a customer saved: the heart on the expert profile.

    staff_id is a PLAIN UUID, not a ForeignKey, for the reason
    Favourite.storefront_id is one: the stylist (staff_profile) lives in the
    platform's tables, which this service only reads.

    ITS OWN TABLE, not a second column on `favourite`. That table's
    storefront_id is NOT NULL and its unique rule is on it, so holding
    stylists there means loosening both; and after a rollback the older code
    would read a stylist row as a salon. A new table is additive: code that
    does not know it never sees it.

    UNIQUE on (account, staff_id): one heart per customer and stylist,
    wherever the stylist was opened. The toggle relies on at most one row.
    """

    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey(
        ConsumerAccount,
        on_delete=models.CASCADE,
        related_name="favourite_stylists",
    )
    staff_id = models.UUIDField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "favourite_stylist"
        constraints = [
            models.UniqueConstraint(
                fields=["account", "staff_id"],
                name="favourite_stylist_account_staff_unique",
            )
        ]
        indexes = [
            models.Index(fields=["account", "-created_at"]),
        ]




class NotificationPreference(models.Model):
    """
    A customer's notification settings. One row per account.

    ONE-TO-ONE with ConsumerAccount, so a customer can never have two sets.
    The row is created lazily with the defaults the first time it is read
    (see for_account), so no backfill is needed for existing accounts.

    `push` is the master switch for push delivery. Turning it off does not
    touch the six individual preferences: they keep their values so that
    turning push back on restores what the customer had chosen.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.OneToOneField(
        ConsumerAccount,
        on_delete=models.CASCADE,
        related_name="notification_preference",
    )

    booking_confirmation = models.BooleanField(default=True)
    appointment_reminder = models.BooleanField(default=True)
    booking_changes = models.BooleanField(default=True)
    special_offers = models.BooleanField(default=False)
    new_salons = models.BooleanField(default=False)
    loyalty = models.BooleanField(default=True)
    push = models.BooleanField(default=True)
    # Booking updates on WhatsApp to the account's phone. Read and written
    # through /auth/me, not the preferences endpoint. Nothing sends them yet.
    # db_default keeps the column's DEFAULT, so an older image (a rollback)
    # can still insert a row that does not name it.
    whatsapp_booking_updates = models.BooleanField(default=False, db_default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "notification_preference"

    @classmethod
    def for_account(cls, account):
        """The account's preferences, created with the defaults on first use."""
        # get_or_create already survives two first requests at once: it catches
        # the unique-constraint error and re-reads. No extra try/except needed.
        prefs, _ = cls.objects.get_or_create(account=account)
        return prefs
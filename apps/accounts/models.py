import uuid
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.contrib.postgres.functions import RandomUUID
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from .managers import ActiveConsumerAccountManager, ConsumerAccountManager
from .notifications import send_account_deleted_notice, verified_email


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

    # 2-step verification (docs/TWO_FACTOR_API.md): when on, a password
    # sign-in on a device that has not passed it needs one more code.
    # db_default too, so a row written by code from before this column is off.
    two_factor_enabled = models.BooleanField(default=False, db_default=False)
    two_factor_changed_at = models.DateTimeField(null=True, blank=True)

    # Sign-in & Security (docs/SIGN_IN_ALERTS_API.md). When the password was
    # last set: at register, on a change and on a reset. Null without one.
    password_changed_at = models.DateTimeField(null=True, blank=True)
    # "Alert me about new sign-ins" (sessions.start). On until the customer
    # turns it off. db_default too, so a row written by older code is on.
    sign_in_alerts = models.BooleanField(default=True, db_default=True)

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
        # The address to tell the deletion is done, once it is. Held only
        # until the commit; nothing stores it after.
        farewell_to = verified_email(self)

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
        self.two_factor_enabled = False
        self.two_factor_changed_at = None
        self.password_changed_at = None  # no password any more
        self.save()

        # 4. End every session. Without this a refresh token (30 days) reaches
        # simplejwt's refresh, whose objects.get() no longer finds the row and
        # answers 500. Blacklisted, it is a 401 before the lookup.
        # Imported here: services imports this module.
        from .services import _blacklist_all_refresh_tokens

        _blacklist_all_refresh_tokens(self)

        # 5. Clean related data
        self.devices.all().delete()
        # Sessions hold IP addresses, places and device names. Their push
        # token links go with them (CASCADE).
        self.sessions.all().delete()
        self.favourites.all().delete()
        self.favourite_stylists.all().delete()
        NotificationPreference.objects.filter(account=self).delete()
        # 2-step verification: the authenticator's secret, and any code,
        # token or sign-in challenge still waiting.
        UserAuthenticator.objects.filter(user=self).delete()
        self.verification_codes.all().delete()
        self.verification_tokens.all().delete()
        self.login_challenges.all().delete()
        if old_contacts:
            OtpCode.objects.filter(
                destination__in=old_contacts, consumed_at__isnull=True
            ).update(consumed_at=timezone.now())
            Verification.objects.filter(destination__in=old_contacts).delete()

        if farewell_to:
            transaction.on_commit(lambda: send_account_deleted_notice(farewell_to))


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

    # The channel switches beside `push`, read and written on the
    # preferences endpoint only while NOTIFICATION_CHANNELS_V1 is on. Email
    # starts on: reminder emails already went to verified addresses. SMS
    # starts off, and sends nothing until there is a provider.
    email = models.BooleanField(default=True, db_default=True)
    sms = models.BooleanField(default=False, db_default=False)
    # How long before a visit its reminder goes, in minutes (REMINDER_TIMING_V1).
    # booking-api asks for it (gRPC CustomerNotifications.GetReminderLeads).
    reminder_lead_minutes = models.PositiveSmallIntegerField(
        choices=[(30, "30 minutes"), (60, "1 hour"), (120, "2 hours"), (1440, "1 day")],
        default=1440,
        db_default=1440,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "notification_preference"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(reminder_lead_minutes__in=[30, 60, 120, 1440]),
                name="notification_preference_reminder_lead_valid",
            ),
        ]

    @classmethod
    def for_account(cls, account):
        """The account's preferences, created with the defaults on first use."""
        # get_or_create already survives two first requests at once: it catches
        # the unique-constraint error and re-reads. No extra try/except needed.
        prefs, _ = cls.objects.get_or_create(account=account)
        return prefs

# DEFAULT now(), as the brief's table has it. Django's Now() would be
# STATEMENT_TIMESTAMP().
_NOW = models.Func(function="now", output_field=models.DateTimeField())


class UserSession(models.Model):
    """
    One sign-in on one device: a row of Where you're signed in. The table is
    the one the mobile brief suggests (docs/DEVICE_SESSIONS_API.md), column
    for column. apps/accounts/sessions.py opens, checks and ends them;
    nothing else writes here.

    A session starts whenever the API issues a token pair and owns the
    refresh token from then on: both tokens carry its id as the `sid` claim,
    refresh_token_hash is the hash of the refresh token it holds now, and a
    rotation finds the session by it. It ends on logout, on Sign Out from
    another device, on a newer sign-in from the same device, and when its
    refresh token is revoked (a password reset). A session whose refresh
    token has expired has ended too: simplejwt's outstanding-token list says
    so (sessions.live).

    The device fields come from the app's X-Device-* headers, which anyone
    can send. They name a session; they never decide access.
    """

    class DeviceType(models.TextChoices):
        PHONE = "phone", "phone"
        TABLET = "tablet", "tablet"
        BROWSER = "browser", "browser"
        DESKTOP = "desktop", "desktop"
        UNKNOWN = "unknown", "unknown"

    class Platform(models.TextChoices):
        IOS = "ios", "ios"
        ANDROID = "android", "android"
        WEB = "web", "web"

    # default for the code, db_default for the table, as the brief has them.
    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, db_default=RandomUUID(), editable=False
    )
    # No plain index on user_id: the brief's partial one below covers the
    # live sessions, which is what every lookup by account asks for.
    user = models.ForeignKey(
        ConsumerAccount,
        on_delete=models.CASCADE,
        related_name="sessions",
        db_index=False,
    )

    # NULL for every value the app did not send: the API answers null for
    # them, and the one-per-device rule below skips a NULL device.
    device_id = models.TextField(null=True, blank=True)
    device_name = models.TextField(null=True, blank=True)
    device_type = models.TextField(
        choices=DeviceType.choices, default=DeviceType.UNKNOWN, db_default=DeviceType.UNKNOWN
    )
    platform = models.TextField(choices=Platform.choices, null=True, blank=True)
    os_version = models.TextField(null=True, blank=True)
    app_version = models.TextField(null=True, blank=True)
    app_build = models.TextField(null=True, blank=True)
    # The client's real IP (sessions.client_ip), and the place it is in. The
    # place name only, never the coordinates.
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    location = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_default=_NOW)
    last_seen_at = models.DateTimeField(default=timezone.now, db_default=_NOW)
    ended_at = models.DateTimeField(null=True, blank=True)

    # SHA-256 of the refresh token this session holds now (the brief: store
    # the refresh token, or its hash, so a rotation finds its session).
    # Moves on every rotation.
    refresh_token_hash = models.CharField(max_length=64)

    # When this sign-in passed 2-step verification (docs/TWO_FACTOR_API.md
    # section 6). A device with a live session that passed is not asked again
    # at its next sign-in, and that sign-in's session carries the stamp on.
    # NULL: never asked.
    two_factor_passed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "user_sessions"
        indexes = [
            models.Index(
                fields=["user"],
                name="user_sessions_user_active_idx",
                condition=Q(ended_at__isnull=True),
            ),
            models.Index(fields=["refresh_token_hash"], name="user_sessions_refresh_hash_idx"),
        ]
        constraints = [
            # One live session per device per account. A sign-in from the
            # same device ends the old one first; this catches two at once.
            models.UniqueConstraint(
                fields=["user", "device_id"],
                condition=Q(ended_at__isnull=True, device_id__isnull=False),
                name="user_sessions_device_active_idx",
            ),
            # The brief's two CHECKs. Spelled out: Meta cannot see the
            # DeviceType and Platform classes above.
            models.CheckConstraint(
                condition=Q(device_type__in=["phone", "tablet", "browser", "desktop", "unknown"]),
                name="user_sessions_device_type_check",
            ),
            models.CheckConstraint(
                condition=Q(platform__in=["ios", "android", "web"]) | Q(platform__isnull=True),
                name="user_sessions_platform_check",
            ),
        ]

    def __str__(self):
        return f"{self.device_name or 'Unknown device'} for {self.user_id}"


# --- 2-step verification (docs/TWO_FACTOR_API.md) ------------------------------
# The brief's four tables, column for column, written only by
# apps/accounts/two_factor.py. Each is deleted with its account (ON DELETE
# CASCADE, migration 0026). otp_code, which register, login and the password
# reset use, has no part in any of it.


class TwoFactorPurpose(models.TextChoices):
    """What a 2-step code or verification token is for. The first four are
    the app's own names (src/lib/security.ts), each asked before one change;
    sign_in is the code of a sign-in challenge, and has no token."""

    TWO_STEP_ON = "two_step_on", "two_step_on"
    TWO_STEP_OFF = "two_step_off", "two_step_off"
    AUTHENTICATOR_ADD = "authenticator_add", "authenticator_add"
    AUTHENTICATOR_REMOVE = "authenticator_remove", "authenticator_remove"
    SIGN_IN = "sign_in", "sign_in"


class TwoFactorMethod(models.TextChoices):
    """Where a 2-step code comes from: the account's own email or phone, or
    its authenticator app."""

    EMAIL = "email", "email"
    PHONE = "phone", "phone"
    AUTHENTICATOR = "authenticator", "authenticator"


class UserAuthenticator(models.Model):
    """
    The account's authenticator app (TOTP). One per account: pending from
    POST /me/security/authenticator until its first code confirms it, then
    active until it is removed, which deletes the row.

    The secret is never stored in plain text: secret_encrypted is AES-GCM
    under TWO_FACTOR_ENCRYPTION_KEY.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "pending"
        ACTIVE = "active", "active"

    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, db_default=RandomUUID(), editable=False
    )
    user = models.OneToOneField(
        ConsumerAccount, on_delete=models.CASCADE, related_name="authenticator"
    )
    secret_encrypted = models.BinaryField()
    status = models.TextField(choices=Status.choices)
    # The session that started a pending setup: only it may confirm it.
    session_id = models.UUIDField(null=True, blank=True)
    # The last 30-second step a code passed for, so no code passes twice.
    last_used_step = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_default=_NOW)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "user_authenticators"
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["pending", "active"]),
                name="user_authenticators_status_check",
            ),
        ]

    def __str__(self):
        return f"Authenticator ({self.status}) for {self.user_id}"


class VerificationCode(models.Model):
    """
    A six-digit code sent to the account's own email or phone: for Verify
    it's you, or for a sign-in challenge (purpose sign_in). At most one live
    code per (user, purpose): a new send voids the last one by setting its
    used_at. Only the code's hash is stored.
    """

    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, db_default=RandomUUID(), editable=False
    )
    user = models.ForeignKey(
        ConsumerAccount, on_delete=models.CASCADE, related_name="verification_codes"
    )
    method = models.TextField(choices=DestinationType.choices)
    purpose = models.TextField(choices=TwoFactorPurpose.choices)
    code_hash = models.TextField()
    attempts = models.IntegerField(default=0, db_default=0)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "verification_codes"
        constraints = [
            models.CheckConstraint(
                condition=Q(method__in=["email", "phone"]),
                name="verification_codes_method_check",
            ),
        ]

    def __str__(self):
        return f"{self.purpose} code by {self.method} for {self.user_id}"


class VerificationToken(models.Model):
    """
    What a passed Verify it's you answers: the proof one sensitive change
    carries. Bound to the user, the session (the bearer's sid) and the
    purpose, valid for 5 minutes, used once. Only the token's SHA-256 is
    stored, as its key.
    """

    token_hash = models.TextField(primary_key=True)
    user = models.ForeignKey(
        ConsumerAccount, on_delete=models.CASCADE, related_name="verification_tokens"
    )
    session_id = models.UUIDField()
    purpose = models.TextField(choices=TwoFactorPurpose.choices)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "verification_tokens"

    def __str__(self):
        return f"{self.purpose} token for {self.user_id}"


class LoginChallenge(models.Model):
    """
    A password sign-in waiting for its second code: what POST /auth/login
    answers instead of tokens while 2-step is on. Valid for 10 minutes,
    allows 5 wrong codes, used up by the sign-in it finishes. Only the
    challenge_token's SHA-256 is stored, as its key.
    """

    token_hash = models.TextField(primary_key=True)
    user = models.ForeignKey(
        ConsumerAccount, on_delete=models.CASCADE, related_name="login_challenges"
    )
    # The sign-in's X-Device-Id, if it sent one: the code must come from the
    # same device, and that device is remembered once it passes.
    device_id = models.TextField(null=True, blank=True)
    attempts = models.IntegerField(default=0, db_default=0)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "login_challenges"

    def __str__(self):
        return f"Sign-in challenge for {self.user_id}"

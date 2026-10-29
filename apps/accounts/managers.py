from django.contrib.auth.base_user import BaseUserManager
from django.utils import timezone


class ConsumerAccountManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, phone, **extra):
        if not phone:
            raise ValueError("Phone is required")
        user = self.model(phone=phone, **extra)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, phone, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_active", True)
        user = self.model(phone=phone, **extra)
        user.set_password(password)
        if user.has_usable_password():
            user.password_changed_at = timezone.now()
        user.save(using=self._db)
        return user


class ActiveConsumerAccountManager(ConsumerAccountManager):
    """The default manager: hides purged accounts (deleted_at set).

    An account waiting for its purge (purge_at set, deleted_at null) is still
    here, so its owner can sign in and cancel.
    """

    # Migrations get a plain manager in its place, so a data migration still
    # sees every row, purged ones too.
    use_in_migrations = False

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)

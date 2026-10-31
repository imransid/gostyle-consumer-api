from django.contrib import admin

from .models import Notification, NotificationDelivery

# apps/accounts/admin.py gives every model a generic, editable admin, and it
# runs first (INSTALLED_APPS order). These two are read-only instead: the
# deliveries show inside their notification.
for model in (Notification, NotificationDelivery):
    if admin.site.is_registered(model):
        admin.site.unregister(model)


class NotificationDeliveryInline(admin.TabularInline):
    model = NotificationDelivery
    extra = 0
    can_delete = False
    fields = [
        "channel",
        "status",
        "attempts",
        "next_attempt_at",
        "sent_at",
        "skip_reason",
        "last_error",
        "provider_ref",
    ]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """Read-only: what a customer was told, and how it went out. Written
    only by apps.notifications.service.notify()."""

    list_display = ["created_at", "type", "category", "title", "account", "read_at"]
    list_filter = ["category", "type"]
    search_fields = ["account__id", "account__email", "dedupe_key"]
    raw_id_fields = ["account"]
    inlines = [NotificationDeliveryInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

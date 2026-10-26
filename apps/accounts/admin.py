from django.apps import apps
from django.contrib import admin
from django.contrib.admin.sites import AlreadyRegistered
from django.db import models as dj_models

from config.read_only_admin import READ_ONLY_APPS

from .models import ConsumerAccount


class DeletionFilter(admin.SimpleListFilter):
    title = "deletion"
    parameter_name = "deletion"

    def lookups(self, request, model_admin):
        return [("pending", "Pending deletion"), ("purged", "Purged")]

    def queryset(self, request, queryset):
        if self.value() == "pending":
            return queryset.filter(purge_at__isnull=False, deleted_at__isnull=True)
        if self.value() == "purged":
            return queryset.filter(deleted_at__isnull=False)
        return queryset


@admin.register(ConsumerAccount)
class ConsumerAccountAdmin(admin.ModelAdmin):
    """Every account, purged ones too.

    No delete, one by one or in bulk. An account leaves through the app's
    deletion request and purge_due_accounts, which clear its personal data
    and keep the row, so ids that booking-api holds still resolve.
    """

    list_display = [
        "id",
        "phone",
        "email",
        "full_name",
        "is_active",
        "created_at",
        "deleted_at",
        "purge_at",
        "deletion_reason",
    ]
    list_filter = [DeletionFilter]
    readonly_fields = ["deleted_at", "purge_at", "deletion_reason", "deletion_note"]
    ordering = ["-created_at"]
    list_per_page = 50
    actions = None

    def get_queryset(self, request):
        # ModelAdmin.get_queryset reads the default manager, which hides
        # purged rows.
        qs = ConsumerAccount.all_objects.get_queryset()
        ordering = self.get_ordering(request)
        if ordering:
            qs = qs.order_by(*ordering)
        return qs

    def has_delete_permission(self, request, obj=None):
        return False


for model in apps.get_models():
    # Other services' tables: read-only, registered by their own admin.py
    # (config/read_only_admin.py)
    if model._meta.app_label in READ_ONLY_APPS:
        continue

    # Django admin cannot handle composite primary keys
    if isinstance(model._meta.pk, dj_models.CompositePrimaryKey):
        continue

    try:
        model_admin = type(
            f"{model.__name__}Admin",
            (admin.ModelAdmin,),
            {
                "list_display": [f.name for f in model._meta.fields][:10],
                "list_per_page": 50,
            },
        )
        admin.site.register(model, model_admin)
    except AlreadyRegistered:
        pass

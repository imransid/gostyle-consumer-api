from django.apps import apps
from django.contrib import admin
from django.contrib.admin.sites import AlreadyRegistered
from django.db import models as dj_models


class ReadOnlyAdmin(admin.ModelAdmin):
    """Platform tables live in the public schema, where the consumer_app DB
    user has SELECT-only access. Block all writes so nobody hits a DB
    permission error from the admin."""

    list_per_page = 50

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def _list_display_for(model):
    """First 10 concrete columns, skipping JSON blobs (they make rows huge)."""
    fields = []
    for f in model._meta.fields:
        if isinstance(f, dj_models.JSONField):
            continue
        fields.append(f.name)
        if len(fields) == 10:
            break
    return fields or ["pk"]


def _search_fields_for(model):
    """Up to 3 text columns so the admin search box works out of the box."""
    names = []
    for f in model._meta.fields:
        if f.get_internal_type() in ("CharField", "TextField"):
            names.append(f.name)
        if len(names) == 3:
            break
    return names


for model in apps.get_models():
    if model._meta.app_label != "platform_data":
        continue

    # The Django admin does not support composite primary keys. Those
    # models (junction tables) stay available via the ORM but are not
    # registered here.
    if isinstance(model._meta.pk, dj_models.CompositePrimaryKey):
        continue

    try:
        model_admin = type(
            f"{model.__name__}Admin",
            (ReadOnlyAdmin,),
            {
                "list_display": _list_display_for(model),
                "search_fields": _search_fields_for(model),
            },
        )
        admin.site.register(model, model_admin)
    except AlreadyRegistered:
        pass
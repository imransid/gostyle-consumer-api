"""Read-only admin pages for other services' tables.

Each read-only app's admin.py calls register_read_only(). The admin shows every
row and every column of those tables and never offers add, change or delete:
the database users behind them can only SELECT.
"""

from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.contrib.admin.sites import AlreadyRegistered
from django.db import models as dj_models

from config.db_router import READ_ONLY_DATABASES

# Never registered by apps/accounts/admin.py, which gives full write access.
READ_ONLY_APPS = {"platform_data", *READ_ONLY_DATABASES}


class ReadOnlyAdmin(admin.ModelAdmin):
    list_per_page = 50

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ListOnlyAdmin(ReadOnlyAdmin):
    """For the list_only twins of tables with a primary key of several columns.

    The twin's primary key is only the first of them, so one value can match
    several rows: no detail page and no selection, just the list.
    """

    list_display_links = None
    actions = None


def _list_display_for(model):
    """First 10 columns, skipping JSON blobs (they make rows huge)."""
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


def register_read_only(app_label):
    # Off until that database is configured: the page would only crash.
    alias = READ_ONLY_DATABASES.get(app_label)
    if alias and alias not in settings.DATABASES:
        return

    for model in apps.get_app_config(app_label).get_models():
        # The Django admin refuses composite primary keys. Those models stay
        # for the ORM; their list_only twin is what the admin shows.
        if isinstance(model._meta.pk, dj_models.CompositePrimaryKey):
            continue

        base = ListOnlyAdmin if getattr(model, "list_only", False) else ReadOnlyAdmin
        model_admin = type(
            f"{model.__name__}Admin",
            (base,),
            {
                "list_display": _list_display_for(model),
                "search_fields": _search_fields_for(model),
            },
        )
        try:
            admin.site.register(model, model_admin)
        except AlreadyRegistered:
            pass

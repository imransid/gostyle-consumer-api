from django.apps import apps
from django.contrib import admin
from django.contrib.admin.sites import AlreadyRegistered
from django.db import models as dj_models

for model in apps.get_models():
    # platform_data has its own admin.py with read-only registration
    if model._meta.app_label == "platform_data":
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
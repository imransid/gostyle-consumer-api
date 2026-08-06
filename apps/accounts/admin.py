# from django.contrib import admin
# from django.contrib.auth.admin import UserAdmin
# from .models import Device
# from .models import ConsumerAccount


# @admin.register(Device)
# class DeviceAdmin(admin.ModelAdmin):
#     list_display = ("account", "platform", "is_active", "last_seen_at")
#     list_filter = ("platform", "is_active")
#     search_fields = ("account__phone", "push_token", "device_id")
#     readonly_fields = ("created_at", "updated_at", "last_seen_at")

# @admin.register(ConsumerAccount)
# class ConsumerAccountAdmin(UserAdmin):
#     ordering = ("-created_at",)
#     list_display = ("phone", "full_name", "is_active", "phone_verified_at")
#     search_fields = ("phone", "full_name", "email")

#     fieldsets = (
#         (None, {"fields": ("phone", "password")}),
#         ("Profile", {"fields": ("full_name", "email")}),
#         ("Preferences", {"fields": ("image", "location", "language", "currency", "is_hijab_mode")}),
#         ("Status", {"fields": ("is_active", "phone_verified_at")}),
#         ("Permissions", {"fields": ("is_staff", "is_superuser", "groups", "user_permissions")}),
#     )
#     add_fieldsets = (
#         (None, {"classes": ("wide",), "fields": ("phone", "password1", "password2")}),
#     )


from django.apps import apps
from django.contrib import admin
from django.contrib.admin.sites import AlreadyRegistered

app_models = apps.get_models()

for model in app_models:
    try:
        # dynamically build a ModelAdmin showing all fields
        model_admin = type(
            f"{model.__name__}Admin",
            (admin.ModelAdmin,),
            {
                "list_display": [f.name for f in model._meta.fields if not f.many_to_many],
                "list_per_page": 50,
                "search_fields": [
                    f.name for f in model._meta.fields
                    if f.get_internal_type() in ("CharField", "TextField")
                ][:3],
            },
        )
        admin.site.register(model, model_admin)
    except AlreadyRegistered:
        pass
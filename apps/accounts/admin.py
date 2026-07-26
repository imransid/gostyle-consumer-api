from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import ConsumerAccount


@admin.register(ConsumerAccount)
class ConsumerAccountAdmin(UserAdmin):
    ordering = ("-created_at",)
    list_display = ("phone", "full_name", "is_active", "phone_verified_at")
    search_fields = ("phone", "full_name", "email")

    fieldsets = (
        (None, {"fields": ("phone", "password")}),
        ("Profile", {"fields": ("full_name", "email")}),
        ("Status", {"fields": ("is_active", "phone_verified_at")}),
        ("Permissions", {"fields": ("is_staff", "is_superuser", "groups", "user_permissions")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("phone", "password1", "password2")}),
    )
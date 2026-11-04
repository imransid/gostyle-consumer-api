from django.contrib import admin
from django.utils import timezone

from . import search_options
from . import verified as verified_rule
from .models import SalonDiscoveryFlag, SearchTag

# apps.accounts.admin registers every model with a plain admin first.
for _model in (SearchTag, SalonDiscoveryFlag):
    if admin.site.is_registered(_model):
        admin.site.unregister(_model)


@admin.register(SearchTag)
class SearchTagAdmin(admin.ModelAdmin):
    """
    The Smart Search's word chips (GET /search/options). A change shows in
    the app at once: saving clears the cached lists.
    """

    list_display = ("label_en", "slug", "group", "match_terms", "sort_order", "active")
    list_filter = ("group", "active")
    list_editable = ("sort_order", "active")
    search_fields = ("label_en", "slug")
    ordering = ("group", "sort_order", "slug")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        search_options.forget()

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        search_options.forget()

    def delete_queryset(self, request, queryset):
        super().delete_queryset(request, queryset)
        search_options.forget()


@admin.register(SalonDiscoveryFlag)
class SalonDiscoveryFlagAdmin(admin.ModelAdmin):
    """
    Verified salons (MAP_FILTER_VERIFIED_V1). Every public salon is listed:
    opening the list adds the missing ones (not verified) and refreshes the
    names. Tick "Is verified" in the list and Save, or pick salons and use
    the actions. The rows follow the salons: nothing to add or delete here.
    """

    list_display = ("salon_name", "is_verified", "updated_at", "updated_by", "salon_id")
    list_editable = ("is_verified",)
    list_filter = ("is_verified",)
    search_fields = ("salon_name", "=salon_id")
    readonly_fields = ("salon_id", "salon_name", "updated_at", "updated_by")
    fields = ("salon_name", "salon_id", "is_verified", "updated_at", "updated_by")
    actions = ("mark_verified", "unmark_verified")
    list_per_page = 50

    def changelist_view(self, request, extra_context=None):
        verified_rule.sync_flags()
        return super().changelist_view(request, extra_context)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    def _set(self, request, queryset, value):
        changed = queryset.exclude(is_verified=value).update(
            is_verified=value, updated_by=request.user, updated_at=timezone.now()
        )
        self.message_user(request, f"{changed} salon(s) {'marked' if value else 'unmarked'} verified.")

    @admin.action(description="Mark verified")
    def mark_verified(self, request, queryset):
        self._set(request, queryset, True)

    @admin.action(description="Unmark verified")
    def unmark_verified(self, request, queryset):
        self._set(request, queryset, False)

from django.contrib import admin

from . import search_options
from .models import SearchTag

# apps.accounts.admin registers every model with a plain admin first.
if admin.site.is_registered(SearchTag):
    admin.site.unregister(SearchTag)


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

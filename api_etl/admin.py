from django.contrib import admin

from api_etl.models import ETLHeldRecord, ETLSyncState


class _ReadOnlyAdmin(admin.ModelAdmin):
    """Operational state: inspectable in the admin, never editable by hand."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]


class ResolvedFilter(admin.SimpleListFilter):
    title = "resolved"
    parameter_name = "resolved"

    def lookups(self, request, model_admin):
        return (("no", "Open"), ("yes", "Resolved"))

    def queryset(self, request, queryset):
        if self.value() == "no":
            return queryset.filter(resolved_at__isnull=True)
        if self.value() == "yes":
            return queryset.filter(resolved_at__isnull=False)
        return queryset


@admin.register(ETLHeldRecord)
class ETLHeldRecordAdmin(_ReadOnlyAdmin):
    list_display = ("source_name", "external_id", "reasons", "times_seen",
                    "first_seen", "last_seen", "resolved_at")
    list_filter = ("source_name", ResolvedFilter)
    search_fields = ("external_id",)


@admin.register(ETLSyncState)
class ETLSyncStateAdmin(_ReadOnlyAdmin):
    list_display = ("source_name", "status", "cursor", "records_pulled", "batches_pushed",
                    "run_started", "run_finished")
    list_filter = ("status",)
    search_fields = ("source_name",)

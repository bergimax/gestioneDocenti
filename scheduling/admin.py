from django.contrib import admin

from .models import AuditLog, Slot, Timetable


@admin.register(Timetable)
class TimetableAdmin(admin.ModelAdmin):
    readonly_fields = ("state", "approved_at", "version")


admin.site.register(Slot)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "week_start")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

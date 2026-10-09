from django.contrib import admin

from .models import Slot, Timetable


@admin.register(Timetable)
class TimetableAdmin(admin.ModelAdmin):
    readonly_fields = ("state", "approved_at", "version")


admin.site.register(Slot)

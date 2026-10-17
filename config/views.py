from django.conf import settings
from django.shortcuts import render
from django.views.decorators.http import require_POST

from config.backup_status import read_backup_status
from projects.permissions import (
    can_view_backup_status,
    can_view_timetable,
    permission_required,
)


@permission_required(can_view_timetable)
def home(request):
    context = {}
    if can_view_backup_status(request.user):
        context["backup"] = read_backup_status(
            settings.BACKUP_MARKER_FILE, settings.BACKUP_STALE_HOURS
        )
    return render(request, "overview.html", context)


@permission_required(can_view_timetable)
@require_POST
def overview_ping(request):
    return render(request, "partials/ping.html")

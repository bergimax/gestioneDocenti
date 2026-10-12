from django.http import HttpResponse
from django.views.decorators.http import require_GET

from projects.permissions import can_export_timetable, permission_required
from scheduling.models import Timetable
from scheduling.views import _parse_week, _school_or_404

from .layout import build_layout
from .pdf import render_pdf

MSG_NO_TIMETABLE = "Nessun orario per questa settimana."


@permission_required(can_export_timetable)
@require_GET
def export_pdf(request, week):
    """Download the week as a PDF, in any state. Reads only: no audit, no bump."""
    school = _school_or_404()
    start = _parse_week(school, week)["start"]
    timetable = Timetable.objects.filter(school=school, week_start=start).first()
    if timetable is None:
        return HttpResponse(
            MSG_NO_TIMETABLE, status=404, content_type="text/plain; charset=utf-8"
        )
    response = HttpResponse(
        render_pdf(build_layout(school, start, timetable)),
        content_type="application/pdf",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="orario_{start:%Y-%m-%d}.pdf"'
    )
    return response

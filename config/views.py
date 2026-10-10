from django.shortcuts import render
from django.views.decorators.http import require_POST

from projects.permissions import can_view_timetable, permission_required


@permission_required(can_view_timetable)
def home(request):
    return render(request, "overview.html")


@permission_required(can_view_timetable)
@require_POST
def overview_ping(request):
    return render(request, "partials/ping.html")


def _placeholder(name, predicate):
    @permission_required(predicate)
    def view(request):
        return render(request, "placeholder.html", {"page_name": name})

    return view


timetable = _placeholder("Orario", can_view_timetable)

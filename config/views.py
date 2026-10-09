from django.shortcuts import render
from django.views.decorators.http import require_POST

from projects.permissions import (
    can_edit_availability,
    can_run_setup,
    can_view_timetable,
    permission_required,
)


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


availability = _placeholder("Disponibilita", can_edit_availability)
timetable = _placeholder("Orario", can_view_timetable)
setup = _placeholder("Setup", can_run_setup)

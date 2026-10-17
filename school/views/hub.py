from django.shortcuts import render

from projects.permissions import (
    can_edit_school_data,
    can_run_setup,
    permission_required,
)


@permission_required(can_edit_school_data)
def school_data(request):
    return render(
        request,
        "school/data_home.html",
        {"can_replace": can_run_setup(request.user)},
    )

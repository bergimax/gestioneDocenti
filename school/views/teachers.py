from django.contrib import messages
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from projects.permissions import (
    can_edit_school_data,
    can_edit_teachers,
    can_view_school_data,
    permission_required,
)

from ..forms import TeacherForm
from ..selectors import current_school, teacher_reference_summary, teachers_of

TPL = "school/teachers/"
NO_SCHOOL = "Nessuna scuola configurata: esegui prima il setup."


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _section(request, school, status=200):
    """The list section (flash messages included): the HTMX success response."""
    context = {
        "school": school,
        "teachers": teachers_of(school) if school else [],
        "can_edit": can_edit_teachers(request.user),
        "flash": True,
    }
    return render(request, TPL + "_section.html", context, status=status)


def _form_response(request, template, context, status=200):
    """A form/confirm screen: a partial for HTMX, the full page otherwise."""
    if _is_htmx(request):
        response = render(request, TPL + "_form_area.html", context, status=status)
        response["HX-Retarget"] = "#teacher-form-area"
        response["HX-Reswap"] = "innerHTML"
        return response
    return render(request, template, context, status=status)


def _done(request, school, message, level=messages.SUCCESS):
    messages.add_message(request, level, message)
    if _is_htmx(request):
        return _section(request, school)
    return redirect("teacher_list")


def _no_school(request):
    context = {"school": None, "can_edit": True, "can_open_hub": True}
    if _is_htmx(request):
        return _section(request, None)
    return render(request, TPL + "list.html", context)


@permission_required(can_view_school_data)
def teacher_list(request):
    school = current_school()
    context = {
        "school": school,
        "teachers": teachers_of(school) if school else [],
        "can_edit": can_edit_teachers(request.user),
        "can_open_hub": can_edit_school_data(request.user),
    }
    return render(request, TPL + "list.html", context)


def _save_form(request, school, instance, creating):
    if request.method == "POST":
        form = TeacherForm(request.POST, instance=instance, school=school)
        if form.is_valid():
            teacher = form.save_or_report()
            if teacher is not None:
                verb = "aggiunto" if creating else "aggiornato"
                return _done(request, school, f"Docente {teacher.name} {verb}.")
    else:
        form = TeacherForm(instance=instance, school=school)
    context = {
        "school": school,
        "form": form,
        "teacher": instance,
        "creating": creating,
    }
    return _form_response(request, TPL + "form_page.html", context)


@permission_required(can_edit_teachers)
@require_http_methods(["GET", "POST"])
def teacher_create(request):
    school = current_school()
    if school is None:
        return _no_school(request)
    return _save_form(request, school, None, creating=True)


@permission_required(can_edit_teachers)
@require_http_methods(["GET", "POST"])
def teacher_edit(request, pk):
    school = current_school()
    if school is None:
        return _no_school(request)
    teacher = get_object_or_404(teachers_of(school), pk=pk)
    return _save_form(request, school, teacher, creating=False)


@permission_required(can_edit_teachers)
@require_http_methods(["GET", "POST"])
def teacher_delete(request, pk):
    school = current_school()
    if school is None:
        return _no_school(request)
    teacher = get_object_or_404(teachers_of(school), pk=pk)
    blockers = teacher_reference_summary(teacher)
    if request.method == "POST":
        if not blockers:
            try:
                teacher.delete()
            except ProtectedError:
                blockers = teacher_reference_summary(teacher) or "altri record"
            else:
                return _done(request, school, f"Docente {teacher.name} eliminato.")
        return _done(
            request,
            school,
            f"Impossibile eliminare {teacher.name}: {blockers}. "
            "Rimuovili prima, oppure disattiva il docente.",
            level=messages.ERROR,
        )
    context = {"school": school, "teacher": teacher, "blockers": blockers}
    return _form_response(request, TPL + "delete_page.html", context)

import datetime

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from projects.permissions import (
    can_edit_school_data,
    can_view_school_data,
    permission_required,
)

from ..forms import CalendarEntryForm
from ..models import CalendarEntry
from ..selectors import current_school, day_label, month_view, resolve_month

TPL = "school/calendar/"
NO_SCHOOL = "Nessuna scuola configurata: esegui prima il setup."


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _is_history_restore(request):
    return request.headers.get("HX-History-Restore-Request") == "true"


def _month_context(request, school, first, flash=False):
    context = {
        "school": school,
        "can_edit": can_edit_school_data(request.user),
        "can_open_hub": can_edit_school_data(request.user),
        "flash": flash,
    }
    if school is not None:
        context.update(month_view(school, first))
    return context


def _section(request, school, first, flash=False, status=200):
    """The month section (flash messages optional): the HTMX response."""
    context = _month_context(request, school, first, flash)
    return render(request, TPL + "_section.html", context, status=status)


def _form_response(request, template, context, status=200):
    """A form/confirm screen: a partial for HTMX, the full page otherwise."""
    if _is_htmx(request):
        response = render(request, TPL + "_form_area.html", context, status=status)
        response["HX-Retarget"] = "#calendar-form-area"
        response["HX-Reswap"] = "innerHTML"
        return response
    return render(request, template, context, status=status)


def _done(request, school, entry, message):
    """Flash and show the month of `entry` (it may differ from the one open)."""
    messages.add_message(request, messages.SUCCESS, message)
    first = entry.date.replace(day=1)
    if _is_htmx(request):
        return _section(request, school, first, flash=True)
    return redirect(f"{reverse('calendar_month')}?mese={first:%Y-%m}")


def _no_school(request):
    if _is_htmx(request):
        return _section(request, None, None)
    context = _month_context(request, None, None)
    context["can_edit"] = False
    return render(request, TPL + "month.html", context)


@permission_required(can_view_school_data)
def calendar_month(request):
    school = current_school()
    if school is None:
        return _no_school(request)
    first = resolve_month(school, request.GET.get("mese"))
    if _is_htmx(request) and not _is_history_restore(request):
        return _section(request, school, first)
    return render(request, TPL + "month.html", _month_context(request, school, first))


def _prefill(request):
    try:
        return {"date": datetime.date.fromisoformat(request.GET.get("data", ""))}
    except ValueError:
        return {}


def _save_form(request, school, instance, creating):
    if request.method == "POST":
        form = CalendarEntryForm(request.POST, instance=instance, school=school)
        if form.is_valid():
            obj = form.save_or_report()
            if obj is not None:
                verb = "aggiunta" if creating else "aggiornata"
                return _done(
                    request,
                    school,
                    obj,
                    f"Riga del {day_label(obj.date)} {verb}.",
                )
    else:
        initial = _prefill(request) if creating else {}
        form = CalendarEntryForm(instance=instance, school=school, initial=initial)
    context = {
        "school": school,
        "form": form,
        "entry": instance,
        "creating": creating,
    }
    return _form_response(request, TPL + "form_page.html", context)


@permission_required(can_edit_school_data)
@require_http_methods(["GET", "POST"])
def calendar_create(request):
    school = current_school()
    if school is None:
        return _no_school(request)
    return _save_form(request, school, None, creating=True)


def _entry_of(school, pk):
    return get_object_or_404(
        CalendarEntry.objects.select_related("stage_class"), pk=pk, school=school
    )


@permission_required(can_edit_school_data)
@require_http_methods(["GET", "POST"])
def calendar_edit(request, pk):
    school = current_school()
    if school is None:
        return _no_school(request)
    return _save_form(request, school, _entry_of(school, pk), creating=False)


@permission_required(can_edit_school_data)
@require_http_methods(["GET", "POST"])
def calendar_delete(request, pk):
    school = current_school()
    if school is None:
        return _no_school(request)
    entry = _entry_of(school, pk)
    if request.method == "POST":
        entry.delete()
        return _done(
            request, school, entry, f"Riga del {day_label(entry.date)} eliminata."
        )
    context = {"school": school, "entry": entry}
    return _form_response(request, TPL + "delete_page.html", context)

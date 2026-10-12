from django.contrib import messages
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from ledger.models import LedgerEntry
from projects.permissions import (
    can_adjust_hours,
    can_edit_school_data,
    can_view_school_data,
    permission_required,
)

from ..assignment_hours import (
    AdjustmentReasonRequired,
    NegativeDelivered,
    adjust_delivered_hours,
    delivered_hours_map,
    set_assignment_total_hours,
)
from ..forms import AssignmentForm, AssignmentHoursForm, DeliveredAdjustForm
from ..selectors import (
    assignment_rows,
    assignments_of,
    current_school,
    pairings_relying_on,
)

TPL = "school/assignments/"


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _section(request, school, status=200):
    """The list section (flash messages included): the HTMX success response."""
    context = {
        "school": school,
        "assignments": assignment_rows(school) if school else [],
        "can_edit": can_edit_school_data(request.user),
        "can_adjust": can_adjust_hours(request.user),
        "flash": True,
    }
    return render(request, TPL + "_section.html", context, status=status)


def _form_response(request, template, context, status=200):
    """A form/confirm screen: a partial for HTMX, the full page otherwise."""
    if _is_htmx(request):
        response = render(request, TPL + "_form_area.html", context, status=status)
        response["HX-Retarget"] = "#assignment-form-area"
        response["HX-Reswap"] = "innerHTML"
        return response
    return render(request, template, context, status=status)


def _done(request, school, message, level=messages.SUCCESS):
    messages.add_message(request, level, message)
    if _is_htmx(request):
        return _section(request, school)
    return redirect("assignment_list")


def _no_school(request):
    context = {"school": None, "can_edit": True, "can_open_hub": True}
    if _is_htmx(request):
        return _section(request, None)
    return render(request, TPL + "list.html", context)


@permission_required(can_view_school_data)
def assignment_list(request):
    school = current_school()
    context = {
        "school": school,
        "assignments": assignment_rows(school) if school else [],
        "can_edit": can_edit_school_data(request.user),
        "can_adjust": can_adjust_hours(request.user),
        "can_open_hub": can_edit_school_data(request.user),
    }
    return render(request, TPL + "list.html", context)


@permission_required(can_edit_school_data)
@require_http_methods(["GET", "POST"])
def assignment_create(request):
    school = current_school()
    if school is None:
        return _no_school(request)
    if request.method == "POST":
        form = AssignmentForm(request.POST, school=school)
        if form.is_valid():
            obj = form.save_or_report()
            if obj is not None:
                return _done(request, school, f"Assegnazione {obj} aggiunta.")
    else:
        form = AssignmentForm(school=school)
    context = {"school": school, "form": form, "assignment": None, "creating": True}
    return _form_response(request, TPL + "form_page.html", context)


@permission_required(can_edit_school_data)
@require_http_methods(["GET", "POST"])
def assignment_edit(request, pk):
    school = current_school()
    if school is None:
        return _no_school(request)
    obj = get_object_or_404(assignments_of(school), pk=pk)
    if request.method == "POST":
        form = AssignmentHoursForm(request.POST)
        if form.is_valid():
            set_assignment_total_hours(obj, form.cleaned_data["ore_totali"])
            return _done(request, school, f"Assegnazione {obj} aggiornata.")
    else:
        form = AssignmentHoursForm(initial={"ore_totali": obj.ore_totali})
    context = {"school": school, "form": form, "assignment": obj, "creating": False}
    return _form_response(request, TPL + "form_page.html", context)


@permission_required(can_edit_school_data)
@require_http_methods(["GET", "POST"])
def assignment_delete(request, pk):
    school = current_school()
    if school is None:
        return _no_school(request)
    obj = get_object_or_404(assignments_of(school), pk=pk)
    blockers = list(pairings_relying_on(obj))
    if request.method == "POST":
        if not blockers:
            try:
                obj.delete()
            except ProtectedError:
                reason = "è usata da altri record"
            else:
                return _done(request, school, f"Assegnazione {obj} eliminata.")
        else:
            names = "; ".join(str(p) for p in blockers)
            noun = "dall'accoppiamento" if len(blockers) == 1 else "dagli accoppiamenti"
            reason = f"è usata {noun} {names}"
        return _done(
            request,
            school,
            f"Impossibile eliminare l'assegnazione {obj}: {reason}.",
            level=messages.ERROR,
        )
    context = {"school": school, "assignment": obj, "blockers": blockers}
    return _form_response(request, TPL + "delete_page.html", context)


@permission_required(can_adjust_hours)
@require_http_methods(["GET", "POST"])
def assignment_adjust(request, pk):
    school = current_school()
    if school is None:
        return _no_school(request)
    obj = get_object_or_404(assignments_of(school), pk=pk)
    current = delivered_hours_map(school).get(obj.pk, 0)
    if request.method == "POST":
        form = DeliveredAdjustForm(request.POST, current=current)
        if form.is_valid():
            try:
                result = adjust_delivered_hours(
                    obj,
                    form.cleaned_data["nuove_ore"],
                    request.user,
                    form.cleaned_data["motivo"],
                )
            except NegativeDelivered:
                form.add_error(
                    "nuove_ore", "Le ore erogate non possono essere negative."
                )
            except AdjustmentReasonRequired:
                form.add_error("motivo", "Indica il motivo della correzione.")
            else:
                if result["changed"]:
                    old, new = result["old"], result["new"]
                    msg = f"Ore erogate di {obj} corrette: {old} → {new}."
                else:
                    msg = f"Ore erogate di {obj} invariate."
                return _done(request, school, msg)
    else:
        form = DeliveredAdjustForm(initial={"nuove_ore": current}, current=current)
    context = {
        "school": school,
        "form": form,
        "assignment": obj,
        "adjusting": True,
        "current_delivered": current,
    }
    return _form_response(request, TPL + "form_page.html", context)


@permission_required(can_view_school_data)
@require_http_methods(["GET"])
def assignment_history(request, pk):
    school = current_school()
    if school is None:
        return _no_school(request)
    obj = get_object_or_404(assignments_of(school), pk=pk)
    entries = (
        LedgerEntry.objects.filter(assignment=obj, kind__in=["ADJUSTMENT", "REVERSAL"])
        .select_related("created_by")
        .order_by("-created_at", "-pk")
    )
    context = {
        "school": school,
        "assignment": obj,
        "history": entries,
        "history_panel": True,
    }
    return _form_response(request, TPL + "history_page.html", context)

import datetime
import re

from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from availability.services import school_weeks
from projects.permissions import (
    can_apply_quick_action,
    can_approve,
    can_assign_slot,
    can_choose_alternative,
    can_decide_conflicts,
    can_export_timetable,
    can_generate,
    can_leave_feedback,
    can_manage_soft_weights,
    can_move_lesson,
    can_repair,
    can_undo_approval,
    can_view_timetable,
    permission_required,
)
from school.models import Class
from school.selectors import current_school, today

from . import alternatives, instruction, proposals
from .assign import assign_lesson, assignable_pairs
from .board import (
    DAY_LABELS,
    NO_COMPLETE_MESSAGE,
    build_board,
    conflict_rows,
    explanations,
    is_best_effort,
    visible_conflicts,
)
from .decisions import approve_conflicts, reject_lesson
from .derogations import QUICK_ACTIONS
from .feedback import InvalidNote, add_feedback
from .forms import QuickActionForm, RepairForm
from .jobs import GenerationRefused, enqueue_generation, fail_stale_jobs
from .models import (
    ACTIVE_JOB_STATUSES,
    Day,
    JobKind,
    JobStatus,
    SolveJob,
    Timetable,
    TimetableFeedback,
    TimetableState,
)
from .moves import InvalidMove, MoveRejected, move_lesson
from .quick_actions import ERROR_NO_TIMETABLE, apply_quick_action
from .repair import ERROR_NO_TIMETABLE as REPAIR_NO_TIMETABLE
from .repair import (
    PARAMS_KEY,
    RepairRefused,
    confirm_repair,
    describe,
    dismiss_repair,
    enqueue_repair,
)
from .services import (
    NotApproved,
    ReasonRequired,
    TimetableLocked,
    approve_timetable,
    undo_approval,
)
from .solver import DEFAULT_SOFT_WEIGHTS

TPL = "scheduling/"
NO_TIMETABLE_TOKEN = "-1"


def _parse_week(school, raw):
    """The listed week whose Monday is `raw` (ISO date), else 404."""
    try:
        start = datetime.date.fromisoformat(raw)
    except (TypeError, ValueError):
        raise Http404("Settimana non valida.") from None
    for week in school_weeks(school):
        if week["start"] == start:
            return week
    raise Http404("Settimana non presente nell'anno formativo.")


def _default_week(weeks):
    day = today()
    for week in weeks:
        if week["start"] <= day < week["start"] + datetime.timedelta(days=7):
            return week
    return weeks[0] if weeks else None


def _school_or_404():
    school = current_school()
    if school is None:
        raise Http404("Nessuna scuola configurata.")
    return school


def _timetable(school, week):
    return Timetable.objects.filter(school=school, week_start=week["start"]).first()


def _version_token(timetable):
    return NO_TIMETABLE_TOKEN if timetable is None else str(timetable.version)


def _body_context(
    school,
    week,
    user,
    oob=False,
    move_error=None,
    clear_panel=False,
    undo_error=None,
    undo_reason="",
    approve_confirm=False,
    clear_result=False,
):
    timetable = _timetable(school, week)
    context = {
        "week": week,
        "timetable": timetable,
        "token": _version_token(timetable),
        "oob": oob,
        "can_generate": can_generate(user),
        "move_error": move_error,
        "can_move": False,
        "can_assign": False,
        "can_decide": False,
        "can_quick": False,
        "clear_panel": clear_panel,
        "approve_confirm": approve_confirm,
        "clear_result": clear_result,
        "can_approve": False,
    }
    if timetable is not None:
        approved = timetable.state == TimetableState.APPROVATO
        visible = visible_conflicts(timetable)
        context.update(
            can_move=can_move_lesson(user) and not approved,
            can_assign=can_assign_slot(user) and not approved,
            can_decide=can_decide_conflicts(user) and not approved,
            can_quick=can_apply_quick_action(user, timetable) and not approved,
            can_export=can_export_timetable(user, timetable),
            days=build_board(school, week["start"], timetable),
            conflict_list=conflict_rows(visible),
            best_effort=is_best_effort(visible),
            explanations=explanations(timetable),
            no_complete_message=NO_COMPLETE_MESSAGE,
            approved=approved,
            can_undo=can_undo_approval(user, timetable) and approved,
            can_approve=can_approve(user) and not approved,
            undo_error=undo_error,
            undo_reason=undo_reason,
        )
    return context


def _feedback_context(school, week, user, error=None, message=None, text=""):
    """The note panel: an ADMIN, on a week that has a timetable."""
    timetable = _timetable(school, week)
    visible = timetable is not None and can_leave_feedback(user, timetable)
    context = {
        "week": week,
        "fb_visible": visible,
        "fb_error": error,
        "fb_message": message,
        "fb_text": text,
        "fb_notes": [],
    }
    if visible:
        context["fb_notes"] = TimetableFeedback.objects.filter(
            school=school, week_start=week["start"]
        ).select_related("user")
    return context


def _deroga_context(
    school,
    week,
    user,
    form=None,
    initial=None,
    message=None,
    error=None,
    open_form=False,
    oob=False,
):
    """The "Deroga" panel: shown to an ADMIN on a draft week only."""
    timetable = _timetable(school, week)
    visible = (
        timetable is not None
        and timetable.state == TimetableState.BOZZA
        and can_apply_quick_action(user, timetable)
    )
    context = {
        "week": week,
        "deroga_visible": visible,
        "deroga_oob": oob,
        "deroga_message": message,
        "deroga_error": error,
        "deroga_open": open_form or form is not None or message is not None,
    }
    if visible:
        context["deroga_form"] = form or QuickActionForm(school=school, initial=initial)
        chosen = context["deroga_form"]["azione"].value()
        context["deroga_options"] = [
            {
                "value": value,
                "label": label,
                "fields": " ".join(QUICK_ACTIONS[value][1]) if value else "",
                "selected": value == chosen,
            }
            for value, label in context["deroga_form"].fields["azione"].choices
        ]
    return context


def _latest_job(school, week):
    return (
        SolveJob.objects.filter(
            school=school, week_start=week["start"], kind=JobKind.GENERATE
        )
        .order_by("-pk")
        .first()
    )


def _job_context(school, week, user, error=None, message=None):
    job = _latest_job(school, week)
    timetable = _timetable(school, week)
    approved = timetable is not None and timetable.state == TimetableState.APPROVATO
    running = job is not None and job.status in ACTIVE_JOB_STATUSES
    if error is None and job is not None and job.status == JobStatus.FAILED:
        error = job.error
    chooser = can_choose_alternative(user) and not approved
    notice = message
    if (
        notice is None
        and can_choose_alternative(user)
        and alternatives.is_alternatives_job(job)
        and job.status == JobStatus.DONE
        and not job.result.get("meaningful")
    ):
        notice = job.result.get("message")
    return {
        "alt_panel": alternatives.panel(job, timetable) if chooser else None,
        "alt_notice": notice,
        "can_alternatives": can_choose_alternative(user),
        "week": week,
        "job": job,
        "running": running,
        "error": error,
        "show_button": can_generate(user) and not running and not approved,
        "job_done": job is not None and job.status == JobStatus.DONE,
    }


@permission_required(can_view_timetable)
@require_GET
def timetable_page(request):
    school = current_school()
    if school is None:
        return render(request, TPL + "page.html", {"school": None})
    weeks = school_weeks(school)
    raw = request.GET.get("settimana")
    week = _parse_week(school, raw) if raw is not None else _default_week(weeks)
    if week is None:
        return render(request, TPL + "page.html", {"school": school, "weeks": []})
    context = {
        "school": school,
        "weeks": weeks,
        "week": week,
        "load_move_js": can_move_lesson(request.user),
    }
    context.update(_job_context(school, week, request.user))
    context.update(_body_context(school, week, request.user))
    context.update(_deroga_context(school, week, request.user))
    context.update(instruction.panel_context(school, week, request.user))
    context.update(_repair_context(school, week, request.user))
    context.update(_feedback_context(school, week, request.user))
    return render(request, TPL + "page.html", context)


@permission_required(can_view_timetable)
@require_GET
def timetable_grid(request, week):
    """The grid fragment, or 204 when the viewer already has this version."""
    school = _school_or_404()
    week = _parse_week(school, week)
    context = _body_context(school, week, request.user)
    if request.GET.get("versione") == context["token"]:
        return HttpResponse(status=204)
    return render(request, TPL + "_body.html", context)


def _job_response(request, school, week, status=200, error=None):
    context = _job_context(school, week, request.user, error=error)
    if context["job_done"] and not context["running"]:
        context.update(_body_context(school, week, request.user, oob=True))
        context.update(_deroga_context(school, week, request.user, oob=True))
        context.update(instruction.panel_context(school, week, request.user, oob=True))
    return render(request, TPL + "_job.html", context, status=status)


@permission_required(can_view_timetable)
@require_GET
def timetable_job(request, week):
    school = _school_or_404()
    week = _parse_week(school, week)
    fail_stale_jobs(school, week["start"])
    return _job_response(request, school, week)


@permission_required(can_generate)
@require_POST
def timetable_generate(request, week):
    school = _school_or_404()
    week = _parse_week(school, week)
    want_alternatives = request.POST.get("alternative") == "1"
    try:
        enqueue_generation(week["start"], request.user, alternatives=want_alternatives)
    except GenerationRefused as exc:
        return _job_response(request, school, week, status=409, error=str(exc))
    return _job_response(request, school, week)


MSG_APPROVED = "La settimana è approvata: le lezioni non si possono spostare."
MSG_BAD_INPUT = "Dati dello spostamento non validi."
_DIGITS = re.compile(r"[0-9]{1,9}")


def _int_field(post, name):
    raw = post.get(name, "")
    if not _DIGITS.fullmatch(raw):
        raise InvalidMove(f"{MSG_BAD_INPUT} Campo non valido: {name}.")
    return int(raw)


def _parse_move(post):
    slot_id = _int_field(post, "slot_id")
    version = _int_field(post, "version")
    hour = _int_field(post, "ora_inizio")
    if not 8 <= hour <= 13:
        raise InvalidMove("L'ora di inizio deve essere compresa tra 8 e 13.")
    day = post.get("giorno", "")
    if day not in Day.values:
        raise InvalidMove("Giorno non valido: sono ammessi solo i giorni feriali.")
    return slot_id, day, hour, version


def _edit_response(request, week, edit):
    """Run `edit(timetable)` on the week and answer with the full, current grid.

    The edit may raise MoveRejected (its status and message are shown above
    the grid; 400 for bad input, 409 for a refused or stale change). An
    approved week is refused before anything is parsed.
    """
    school = _school_or_404()
    week = _parse_week(school, week)
    timetable = _timetable(school, week)
    if timetable is None:
        raise Http404("Nessun orario per questa settimana.")
    status, error = 200, None
    try:
        if timetable.state == TimetableState.APPROVATO:
            raise TimetableLocked(timetable.pk)
        edit(timetable)
    except MoveRejected as exc:
        status, error = exc.status, exc.message
    except TimetableLocked:
        status, error = 409, MSG_APPROVED
    context = _body_context(
        school, week, request.user, move_error=error, clear_panel=True
    )
    return render(request, TPL + "_body.html", context, status=status)


@permission_required(can_move_lesson)
@require_POST
def timetable_move(request, week):
    """Move one lesson; always answers with the full, current grid."""

    def edit(timetable):
        slot_id, day, hour, version = _parse_move(request.POST)
        move_lesson(timetable.pk, slot_id, day, hour, version, request.user)

    return _edit_response(request, week, edit)


def _parse_cell(data):
    """(class id, day, hour) of a grid cell, or InvalidMove."""
    class_id = _int_field(data, "classe")
    hour = _int_field(data, "ora_inizio")
    if not 8 <= hour <= 13:
        raise InvalidMove("L'ora di inizio deve essere compresa tra 8 e 13.")
    day = data.get("giorno", "")
    if day not in Day.values:
        raise InvalidMove("Giorno non valido: sono ammessi solo i giorni feriali.")
    return class_id, day, hour


@permission_required(can_assign_slot)
@require_GET
def timetable_assignable(request, week, class_id):
    """The assignable teacher-subject pairs of a class, for one empty cell."""
    school = _school_or_404()
    week = _parse_week(school, week)
    timetable = _timetable(school, week)
    if timetable is None:
        raise Http404("Nessun orario per questa settimana.")
    school_class = Class.objects.filter(school=school, pk=class_id).first()
    if school_class is None:
        raise Http404("Classe non trovata.")
    context = {"week": week, "school_class": school_class, "error": None}
    status = 200
    try:
        if timetable.state == TimetableState.APPROVATO:
            raise TimetableLocked(timetable.pk)
        _, day, hour = _parse_cell({**request.GET.dict(), "classe": str(class_id)})
    except TimetableLocked:
        status, context["error"] = 409, MSG_APPROVED
    except InvalidMove as exc:
        status, context["error"] = exc.status, exc.message
    else:
        context.update(
            day=day,
            day_label=DAY_LABELS[day],
            hour=hour,
            token=_version_token(timetable),
            pairs=assignable_pairs(school, school_class, timetable),
        )
    return render(request, TPL + "_assign_panel.html", context, status=status)


@permission_required(can_assign_slot)
@require_POST
def timetable_assign(request, week):
    """Place a lesson of a (teacher, subject) pair in an empty cell."""

    def edit(timetable):
        class_id, day, hour = _parse_cell(request.POST)
        teacher_id = _int_field(request.POST, "docente")
        subject_id = _int_field(request.POST, "materia")
        version = _int_field(request.POST, "version")
        assign_lesson(
            timetable.pk,
            class_id,
            day,
            hour,
            teacher_id,
            subject_id,
            version,
            request.user,
        )

    return _edit_response(request, week, edit)


@permission_required(can_decide_conflicts)
@require_POST
def timetable_conflicts_approve(request, week):
    """Stop reporting the conflicts with the posted `chiavi`."""

    def edit(timetable):
        version = _int_field(request.POST, "version")
        approve_conflicts(
            timetable.pk, request.POST.getlist("chiavi"), version, request.user
        )

    return _edit_response(request, week, edit)


@permission_required(can_decide_conflicts)
@require_POST
def timetable_conflicts_reject(request, week):
    """Remove the lesson of a cell and leave the hour free (LIBERA)."""

    def edit(timetable):
        class_id, day, hour = _parse_cell(request.POST)
        version = _int_field(request.POST, "version")
        reject_lesson(timetable.pk, class_id, day, hour, version, request.user)

    return _edit_response(request, week, edit)


MSG_EMPTY_WEEK = "Nessuna lezione: approvare comunque?"
MSG_CONFLICTS = "La settimana ha {count} conflitti non risolti: approvare comunque?"


@permission_required(can_approve)
@require_POST
def timetable_approve(request, week):
    """Approve the week. An empty week or unresolved conflicts need `confirm`."""
    school = _school_or_404()
    week = _parse_week(school, week)
    timetable = _timetable(school, week)
    if timetable is None:
        raise Http404("Nessun orario per questa settimana.")
    error = None
    was_approved = timetable.state == TimetableState.APPROVATO
    if not was_approved and "confirm" not in request.POST:
        conflicts = visible_conflicts(timetable)
        if not timetable.slots.exists():
            error = MSG_EMPTY_WEEK
        elif conflicts:
            error = MSG_CONFLICTS.format(count=len(conflicts))
    if error is not None:
        context = _body_context(
            school, week, request.user, move_error=error, approve_confirm=True
        )
        return render(request, TPL + "_body.html", context, status=409)
    result = approve_timetable(timetable.pk, request.user)
    context = _body_context(school, week, request.user)
    context.update(_job_context(school, week, request.user))
    context["job_oob"] = True
    context.update(_deroga_context(school, week, request.user, oob=True))
    context.update(instruction.panel_context(school, week, request.user, oob=True))
    context.update(_repair_context(school, week, request.user, oob=True))
    context["approval_result"] = None if was_approved else result
    return render(request, TPL + "_approve_response.html", context)


MSG_NOT_APPROVED = "La settimana non è approvata: non c'è nulla da annullare."
MSG_UNDO_REASON = "Indica il motivo dell'annullamento (massimo 300 caratteri)."


@permission_required(can_undo_approval)
@require_POST
def timetable_undo_approval(request, week):
    """Return an approved week to BOZZA; the reason is mandatory."""
    school = _school_or_404()
    week = _parse_week(school, week)
    timetable = _timetable(school, week)
    if timetable is None:
        raise Http404("Nessun orario per questa settimana.")
    reason = request.POST.get("motivo", "")
    error, undo_error, status = None, None, 200
    try:
        undo_approval(timetable.pk, request.user, reason)
    except NotApproved:
        error, status = MSG_NOT_APPROVED, 409
    except ReasonRequired:
        undo_error, status = MSG_UNDO_REASON, 400
    context = _body_context(
        school,
        week,
        request.user,
        move_error=error,
        undo_error=undo_error,
        undo_reason=reason,
        clear_result=status == 200,
    )
    return render(request, TPL + "_body.html", context, status=status)


MSG_QUICK_STARTED = "Deroga «{label}» applicata a: {scope}. Rigenerazione avviata."


def _deroga_response(request, school, week, status=200, **kw):
    """The panel plus the job area (to start its polling) in one answer."""
    context = _deroga_context(school, week, request.user, **kw)
    context.update(_job_context(school, week, request.user))
    context["job_oob"] = True
    return render(request, TPL + "_deroga_response.html", context, status=status)


@permission_required(can_apply_quick_action)
def timetable_quick_action(request, week):
    """GET: the form, pre-filled from a conflict. POST: apply the derogation."""
    school = _school_or_404()
    week = _parse_week(school, week)
    timetable = _timetable(school, week)
    if request.method == "GET":
        initial = {
            "azione": request.GET.get("azione", ""),
            "classe": request.GET.get("classe", ""),
            "docente": request.GET.get("docente", ""),
            "giorno": request.GET.get("giorno", ""),
        }
        context = _deroga_context(
            school, week, request.user, initial=initial, open_form=True
        )
        status = 200
        if not context["deroga_visible"]:
            status = 409
            context["deroga_error"] = _refusal(timetable)
        return render(request, TPL + "_deroga.html", context, status=status)
    if request.method != "POST":
        return HttpResponse(status=405)
    refusal = _refusal(timetable)
    if refusal:
        return _deroga_response(request, school, week, status=409, error=refusal)
    form = QuickActionForm(request.POST, school=school)
    if not form.is_valid():
        return _deroga_response(request, school, week, status=400, form=form)
    params = form.params()
    try:
        _, scope = apply_quick_action(week["start"], request.user, params)
    except GenerationRefused as exc:
        return _deroga_response(
            request, school, week, status=409, error=str(exc), form=form
        )
    message = MSG_QUICK_STARTED.format(
        label=QUICK_ACTIONS[params["action_type"]][0], scope=scope
    )
    return _deroga_response(request, school, week, message=message)


def _refusal(timetable):
    if timetable is None:
        return ERROR_NO_TIMETABLE
    if timetable.state == TimetableState.APPROVATO:
        return MSG_QUICK_APPROVED
    return None


MSG_QUICK_APPROVED = "La settimana è approvata: l'orario non può essere rigenerato."


# ---- repair solving (sostituzione) -------------------------------------------

MSG_REPAIR_STARTED = "Sostituzione avviata: {what}."
MSG_REPAIR_APPLIED = "Sostituzione applicata: l'orario è stato aggiornato."
MSG_REPAIR_DISMISSED = "Sostituzione annullata: l'orario non è stato modificato."


def _repair_job(school, week):
    return (
        SolveJob.objects.filter(
            school=school, week_start=week["start"], kind=JobKind.REPAIR
        )
        .order_by("-pk")
        .first()
    )


def _repair_context(
    school,
    week,
    user,
    form=None,
    message=None,
    error=None,
    oob=False,
    open_form=False,
):
    """The "Sostituzione" panel: the form (draft week, ADMIN) and the last job."""
    timetable = _timetable(school, week)
    draft = timetable is not None and timetable.state == TimetableState.BOZZA
    visible = draft and can_repair(user)
    job = _repair_job(school, week) if can_repair(user) else None
    context = {
        "week": week,
        "repair_visible": visible,
        "repair_oob": oob,
        "repair_message": message,
        "repair_error": error,
        "repair_open": open_form or form is not None or message is not None,
        "repair_job": None,
        "repair_running": False,
        "repair_failed": None,
        "repair_proposal": None,
    }
    if visible:
        context["repair_form"] = form or RepairForm(school=school, timetable=timetable)
        chosen = context["repair_form"]["tipo"].value()
        context["repair_options"] = [
            {
                "value": value,
                "label": label,
                "fields": " ".join(RepairForm.NEEDS.get(value, ())),
                "selected": value == chosen,
            }
            for value, label in context["repair_form"].fields["tipo"].choices
        ]
    if job is not None and draft:
        context["repair_job"] = job
        context["repair_running"] = job.status in ACTIVE_JOB_STATUSES
        if job.status == JobStatus.FAILED:
            context["repair_failed"] = job.error
        elif job.status == JobStatus.DONE and not (
            job.result.get("applied") or job.result.get("dismissed")
        ):
            stale = job.result.get("base_version") != timetable.version
            context["repair_proposal"] = {
                **job.result,
                "job": job,
                "what": describe(job.params[PARAMS_KEY]),
                "stale": stale,
                "can_confirm": visible
                and not stale
                and not job.result.get("empty")
                and bool(job.result.get("changes")),
            }
    return context


def _repair_response(request, school, week, status=200, with_body=False, **kw):
    context = _repair_context(school, week, request.user, **kw)
    if with_body:
        context.update(_body_context(school, week, request.user, oob=True))
    return render(request, TPL + "_repair_response.html", context, status=status)


@permission_required(can_repair)
@require_POST
def timetable_repair(request, week):
    """Start a REPAIR job from the "Sostituzione" form."""
    school = _school_or_404()
    week = _parse_week(school, week)
    timetable = _timetable(school, week)
    refusal = _refusal_repair(timetable)
    if refusal:
        return _repair_response(request, school, week, status=409, error=refusal)
    form = RepairForm(request.POST, school=school, timetable=timetable)
    if not form.is_valid():
        return _repair_response(request, school, week, status=400, form=form)
    params = form.params()
    try:
        enqueue_repair(week["start"], request.user, params)
    except RepairRefused as exc:
        return _repair_response(
            request, school, week, status=exc.status, error=exc.message, form=form
        )
    message = MSG_REPAIR_STARTED.format(what=describe(params))
    return _repair_response(request, school, week, message=message)


def _refusal_repair(timetable):
    if timetable is None:
        return REPAIR_NO_TIMETABLE
    if timetable.state == TimetableState.APPROVATO:
        return MSG_REPAIR_APPROVED
    return None


MSG_REPAIR_APPROVED = (
    "La settimana è approvata: l'orario non può essere modificato da una sostituzione."
)


@permission_required(can_repair)
@require_GET
def timetable_repair_status(request, week):
    """Polled while a repair runs: the status area, with the diff when done."""
    school = _school_or_404()
    week = _parse_week(school, week)
    context = _repair_context(school, week, request.user)
    return render(request, TPL + "_repair_status.html", context)


@permission_required(can_repair)
@require_POST
def timetable_repair_confirm(request, week, job_id):
    """Apply the diff of a finished repair; answers with the panel and the grid."""
    school = _school_or_404()
    week = _parse_week(school, week)
    try:
        job_ref = SolveJob.objects.filter(
            pk=job_id, school=school, week_start=week["start"], kind=JobKind.REPAIR
        ).first()
        if job_ref is None:
            raise RepairRefused("Sostituzione non trovata.", status=404)
        confirm_repair(job_id, request.user)
    except RepairRefused as exc:
        return _repair_response(
            request, school, week, status=exc.status, error=exc.message, with_body=True
        )
    return _repair_response(
        request, school, week, message=MSG_REPAIR_APPLIED, with_body=True
    )


@permission_required(can_repair)
@require_POST
def timetable_repair_dismiss(request, week, job_id):
    """Annulla: drop the proposal; the timetable and its version stay as they are."""
    school = _school_or_404()
    week = _parse_week(school, week)
    try:
        if not SolveJob.objects.filter(
            pk=job_id, school=school, week_start=week["start"], kind=JobKind.REPAIR
        ).exists():
            raise RepairRefused("Sostituzione non trovata.", status=404)
        dismiss_repair(job_id, request.user)
    except RepairRefused as exc:
        return _repair_response(
            request, school, week, status=exc.status, error=exc.message
        )
    return _repair_response(request, school, week, message=MSG_REPAIR_DISMISSED)


# ---- alternatives and pairwise choice ---------------------------------------

MSG_ALT_PICKED = "{label} scelta: l'orario della settimana è stato aggiornato."


def _alt_refused(request, school, week, exc):
    context = _job_context(school, week, request.user, error=exc.message)
    return render(request, TPL + "_job.html", context, status=exc.status)


@permission_required(can_choose_alternative)
@require_POST
def timetable_alt_choose(request, week, job_id, index):
    """ "Scegli": write the alternative, store the pick, answer with job area + grid."""
    school = _school_or_404()
    week = _parse_week(school, week)
    job = SolveJob.objects.filter(
        pk=job_id, school=school, week_start=week["start"]
    ).first()
    if job is None:
        return _alt_refused(
            request,
            school,
            week,
            alternatives.AlternativeRefused(alternatives.MSG_NOT_FOUND, status=404),
        )
    try:
        alternatives.choose_alternative(job_id, index, request.user)
    except alternatives.AlternativeRefused as exc:
        context = _job_context(school, week, request.user, error=exc.message)
        context.update(_body_context(school, week, request.user, oob=True))
        return render(request, TPL + "_job.html", context, status=exc.status)
    label = f"Alternativa {alternatives.LETTERS[index]}"
    context = _job_context(
        school, week, request.user, message=MSG_ALT_PICKED.format(label=label)
    )
    context.update(_body_context(school, week, request.user, oob=True))
    return render(request, TPL + "_job.html", context)


@permission_required(can_choose_alternative)
@require_GET
def timetable_alt_view(request, week, job_id, index):
    """One stored alternative as a read-only grid (nothing is written)."""
    school = _school_or_404()
    week = _parse_week(school, week)
    try:
        job, alt = alternatives.get_alternative(job_id, index)
    except alternatives.AlternativeRefused as exc:
        raise Http404(exc.message) from None
    if job.school_id != school.pk or job.week_start != week["start"]:
        raise Http404(alternatives.MSG_NOT_FOUND)
    slots = alternatives.preview_slots(job, alt)
    days = build_board(
        school, week["start"], None, slots=slots, conflicts=alt["conflict_items"]
    )
    context = {
        "week": week,
        "days": days,
        "preview_label": alt["label"],
        "preview_description": alternatives.describe(alt["variation"]),
    }
    return render(request, TPL + "_alt_preview.html", context)


# ---- weight proposals -------------------------------------------------------

MSG_APPLIED = "Pesi aggiornati: {kinds}."
MSG_NOTHING_TO_APPLY = "Nessuna proposta da applicare (già applicata o non più valida)."
MSG_RESET = "Ripristinato il valore predefinito: {kinds}."
MSG_ALREADY_DEFAULT = "Già al valore predefinito: {kinds}."
MSG_PICK_KINDS = "Seleziona almeno un peso."


def _names(kinds):
    return ", ".join(proposals.KIND_NAMES[k] for k in kinds)


def _requested_kinds(request):
    """None for "all", else the list of kinds posted; ValueError if invalid."""
    if request.POST.get("all") == "1":
        return None
    kinds = request.POST.getlist("kind")
    if not kinds:
        raise ValueError(MSG_PICK_KINDS)
    proposals.check_kinds(kinds)
    return kinds


@permission_required(can_manage_soft_weights)
@require_GET
def weights_page(request):
    school = current_school()
    active = proposals.active_weights(school)
    context = {
        "proposals": proposals.compute_proposals(school),
        "weights": [
            {
                "kind": k,
                "name": proposals.KIND_NAMES[k],
                "default": DEFAULT_SOFT_WEIGHTS[k],
                "active": active[k],
            }
            for k in proposals.ADJUSTABLE_KINDS
        ],
        "history": [
            (row, proposals.KIND_NAMES.get(row.kind, row.kind))
            for row in proposals.history(school)
        ],
    }
    return render(request, TPL + "weights.html", context)


@permission_required(can_manage_soft_weights)
@require_POST
def weights_apply(request):
    try:
        kinds = _requested_kinds(request)
    except ValueError:
        return HttpResponseBadRequest("Peso non modificabile.")
    rows = proposals.apply_proposals(current_school(), request.user, kinds)
    if rows:
        messages.success(
            request, MSG_APPLIED.format(kinds=_names([r.kind for r in rows]))
        )
    else:
        messages.info(request, MSG_NOTHING_TO_APPLY)
    return redirect("soft_weights")


@permission_required(can_manage_soft_weights)
@require_POST
def weights_reset(request):
    try:
        kinds = _requested_kinds(request)
    except ValueError:
        return HttpResponseBadRequest("Peso non modificabile.")
    rows, skipped = proposals.reset_weights(current_school(), request.user, kinds)
    if rows:
        messages.success(
            request, MSG_RESET.format(kinds=_names([r.kind for r in rows]))
        )
    if skipped:
        messages.info(request, MSG_ALREADY_DEFAULT.format(kinds=_names(skipped)))
    return redirect("soft_weights")


@permission_required(can_leave_feedback)
@require_POST
def timetable_feedback(request, week):
    """Save a free-text note on the week; answers with the note panel."""
    school = _school_or_404()
    week = _parse_week(school, week)
    timetable = _timetable(school, week)
    if timetable is None:
        raise Http404("Nessun orario per questa settimana.")
    text = request.POST.get("nota", "")
    try:
        add_feedback(timetable.pk, request.user, text)
    except InvalidNote as exc:
        context = _feedback_context(
            school, week, request.user, error=exc.args[0], text=text
        )
        return render(request, TPL + "_feedback.html", context, status=400)
    context = _feedback_context(school, week, request.user, message="Nota salvata.")
    return render(request, TPL + "_feedback.html", context)

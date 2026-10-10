import datetime
import re

from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from availability.services import school_weeks
from projects.permissions import (
    can_apply_quick_action,
    can_assign_slot,
    can_decide_conflicts,
    can_generate,
    can_move_lesson,
    can_repair,
    can_view_timetable,
    permission_required,
)
from school.models import Class
from school.selectors import current_school, today

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
from .forms import QuickActionForm, RepairForm
from .jobs import GenerationRefused, enqueue_generation
from .models import (
    ACTIVE_JOB_STATUSES,
    Day,
    JobKind,
    JobStatus,
    SolveJob,
    Timetable,
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
from .services import TimetableLocked

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


def _body_context(school, week, user, oob=False, move_error=None, clear_panel=False):
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
    }
    if timetable is not None:
        approved = timetable.state == TimetableState.APPROVATO
        visible = visible_conflicts(timetable)
        context.update(
            can_move=can_move_lesson(user) and not approved,
            can_assign=can_assign_slot(user) and not approved,
            can_decide=can_decide_conflicts(user) and not approved,
            can_quick=can_apply_quick_action(user, timetable) and not approved,
            days=build_board(school, week["start"], timetable),
            conflict_list=conflict_rows(visible),
            best_effort=is_best_effort(visible),
            explanations=explanations(timetable),
            no_complete_message=NO_COMPLETE_MESSAGE,
            approved=approved,
        )
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


def _job_context(school, week, user, error=None):
    job = _latest_job(school, week)
    timetable = _timetable(school, week)
    approved = timetable is not None and timetable.state == TimetableState.APPROVATO
    running = job is not None and job.status in ACTIVE_JOB_STATUSES
    if error is None and job is not None and job.status == JobStatus.FAILED:
        error = job.error
    return {
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
    context.update(_repair_context(school, week, request.user))
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
    return render(request, TPL + "_job.html", context, status=status)


@permission_required(can_view_timetable)
@require_GET
def timetable_job(request, week):
    school = _school_or_404()
    return _job_response(request, school, _parse_week(school, week))


@permission_required(can_generate)
@require_POST
def timetable_generate(request, week):
    school = _school_or_404()
    week = _parse_week(school, week)
    try:
        enqueue_generation(week["start"], request.user)
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
        move_lesson(timetable.pk, slot_id, day, hour, version)

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
            timetable.pk, class_id, day, hour, teacher_id, subject_id, version
        )

    return _edit_response(request, week, edit)


@permission_required(can_decide_conflicts)
@require_POST
def timetable_conflicts_approve(request, week):
    """Stop reporting the conflicts with the posted `chiavi`."""

    def edit(timetable):
        version = _int_field(request.POST, "version")
        approve_conflicts(timetable.pk, request.POST.getlist("chiavi"), version)

    return _edit_response(request, week, edit)


@permission_required(can_decide_conflicts)
@require_POST
def timetable_conflicts_reject(request, week):
    """Remove the lesson of a cell and leave the hour free (LIBERA)."""

    def edit(timetable):
        class_id, day, hour = _parse_cell(request.POST)
        version = _int_field(request.POST, "version")
        reject_lesson(timetable.pk, class_id, day, hour, version)

    return _edit_response(request, week, edit)


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

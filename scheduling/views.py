import datetime

from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from availability.services import school_weeks
from projects.permissions import can_generate, can_view_timetable, permission_required
from school.selectors import current_school, today

from .board import (
    NO_COMPLETE_MESSAGE,
    build_board,
    conflict_descriptions,
    is_best_effort,
)
from .jobs import GenerationRefused, enqueue_generation
from .models import (
    ACTIVE_JOB_STATUSES,
    JobKind,
    JobStatus,
    SolveJob,
    Timetable,
    TimetableState,
)

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


def _body_context(school, week, user, oob=False):
    timetable = _timetable(school, week)
    context = {
        "week": week,
        "timetable": timetable,
        "token": _version_token(timetable),
        "oob": oob,
        "can_generate": can_generate(user),
    }
    if timetable is not None:
        context.update(
            days=build_board(school, week["start"], timetable),
            conflict_list=conflict_descriptions(timetable.conflicts),
            best_effort=is_best_effort(timetable.conflicts),
            no_complete_message=NO_COMPLETE_MESSAGE,
            approved=timetable.state == TimetableState.APPROVATO,
        )
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
    context = {"school": school, "weeks": weeks, "week": week}
    context.update(_job_context(school, week, request.user))
    context.update(_body_context(school, week, request.user))
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

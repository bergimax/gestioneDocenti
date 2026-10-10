"""Starting a generation: the only entry point the views will call."""

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction

from availability.grid import check_monday
from projects.permissions import can_generate
from school.models import Assignment
from school.selectors import current_school

from .models import ACTIVE_JOB_STATUSES, JobKind, SolveJob, Timetable, TimetableState
from .tasks import ALTERNATIVES_KEY, ERROR_APPROVED, ERROR_NO_ASSIGNMENTS, run_solve_job

ERROR_ACTIVE_JOB = (
    "Una generazione di questa settimana è già in corso: attendi che finisca."
)


class GenerationRefused(Exception):
    """The generation cannot start; `str(exc)` is the Italian reason."""


def _active_job(school, week_start):
    return SolveJob.objects.filter(
        school=school,
        week_start=week_start,
        kind=JobKind.GENERATE,
        status__in=ACTIVE_JOB_STATUSES,
    ).first()


def enqueue_generation(week_start, user, alternatives=False):
    """Create a PENDING job for the week and enqueue it; returns at once.

    Returns the already pending or running job of that week instead of a second
    one. With `alternatives` the job solves up to three weight variations and
    writes nothing until one is picked (scheduling.alternatives). Raises
    PermissionDenied (user lacks can_generate), ValueError (not a
    Monday) or GenerationRefused (approved week, no assignments).
    """
    if not can_generate(user):
        raise PermissionDenied
    return enqueue_job(
        week_start, user, {ALTERNATIVES_KEY: True} if alternatives else None
    )


def enqueue_job(week_start, user, params=None, refuse_active=False):
    """The shared enqueue; the caller has already checked the permission.

    `params` is stored on the job (the one regeneration a quick action is for).
    With `refuse_active` a pending or running job of the week raises
    GenerationRefused instead of being returned.
    """
    check_monday(week_start)
    school = current_school()
    if Timetable.objects.filter(
        school=school, week_start=week_start, state=TimetableState.APPROVATO
    ).exists():
        raise GenerationRefused(ERROR_APPROVED)
    if not Assignment.objects.filter(school=school, teacher__active=True).exists():
        raise GenerationRefused(ERROR_NO_ASSIGNMENTS)
    try:
        with transaction.atomic():
            existing = _active_job(school, week_start)
            if existing is not None:
                if refuse_active:
                    raise GenerationRefused(ERROR_ACTIVE_JOB)
                return existing
            job = SolveJob.objects.create(
                school=school,
                week_start=week_start,
                kind=JobKind.GENERATE,
                requested_by=user,
                params=params or {},
            )
    except IntegrityError:  # a concurrent request created it first
        if refuse_active:
            raise GenerationRefused(ERROR_ACTIVE_JOB) from None
        return _active_job(school, week_start)
    try:
        run_solve_job.enqueue(job.pk)
    except Exception:
        job.delete()
        raise
    return job

"""Starting a generation: the only entry point the views will call."""

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction

from availability.grid import check_monday
from projects.permissions import can_generate
from school.models import Assignment
from school.selectors import current_school

from .models import ACTIVE_JOB_STATUSES, JobKind, SolveJob, Timetable, TimetableState
from .tasks import ERROR_APPROVED, ERROR_NO_ASSIGNMENTS, run_solve_job


class GenerationRefused(Exception):
    """The generation cannot start; `str(exc)` is the Italian reason."""


def _active_job(school, week_start):
    return SolveJob.objects.filter(
        school=school,
        week_start=week_start,
        kind=JobKind.GENERATE,
        status__in=ACTIVE_JOB_STATUSES,
    ).first()


def enqueue_generation(week_start, user):
    """Create a PENDING job for the week and enqueue it; returns at once.

    Returns the already pending or running job of that week instead of a second
    one. Raises PermissionDenied (user lacks can_generate), ValueError (not a
    Monday) or GenerationRefused (approved week, no assignments).
    """
    if not can_generate(user):
        raise PermissionDenied
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
                return existing
            job = SolveJob.objects.create(
                school=school,
                week_start=week_start,
                kind=JobKind.GENERATE,
                requested_by=user,
            )
    except IntegrityError:  # a concurrent request created it first
        return _active_job(school, week_start)
    try:
        run_solve_job.enqueue(job.pk)
    except Exception:
        job.delete()
        raise
    return job

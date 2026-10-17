"""Starting a generation: the only entry point the views will call."""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models.functions import Coalesce
from django.utils import timezone

from availability.grid import check_monday
from projects.permissions import can_generate
from school.models import Assignment
from school.selectors import current_school

from .models import (
    ACTIVE_JOB_STATUSES,
    InvalidJobTransition,
    JobKind,
    JobStatus,
    SolveJob,
    Timetable,
    TimetableState,
)
from .tasks import ALTERNATIVES_KEY, ERROR_APPROVED, ERROR_NO_ASSIGNMENTS, run_solve_job

ERROR_ACTIVE_JOB = (
    "Una generazione di questa settimana è già in corso: attendi che finisca."
)

ERROR_STALE_JOB = (
    "La generazione si è interrotta (il processo di calcolo è stato fermato). Riprova."
)


class GenerationRefused(Exception):
    """The generation cannot start; `str(exc)` is the Italian reason."""


def fail_stale_jobs(school=None, week_start=None, now=None):
    """Mark RUNNING jobs older than timeout + margin FAILED; returns how many.

    A killed worker leaves its job RUNNING for ever, which blocks the week.
    The reference time is `started_at` (else `created_at`); only strictly older
    jobs qualify. PENDING jobs are never touched.
    """
    now = now or timezone.now()
    limit = timedelta(
        seconds=settings.SOLVER_TIMEOUT_SECONDS + settings.SOLVER_STALE_MARGIN_SECONDS
    )
    jobs = SolveJob.objects.filter(status=JobStatus.RUNNING).annotate(
        since=Coalesce("started_at", "created_at")
    )
    jobs = jobs.filter(since__lt=now - limit)
    if school is not None:
        jobs = jobs.filter(school=school)
    if week_start is not None:
        jobs = jobs.filter(week_start=week_start)
    failed = 0
    for job in jobs:
        try:
            job.transition_to(JobStatus.FAILED, error=ERROR_STALE_JOB)
        except InvalidJobTransition:  # it finished or failed meanwhile
            continue
        failed += 1
    return failed


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
    fail_stale_jobs(school, week_start)
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

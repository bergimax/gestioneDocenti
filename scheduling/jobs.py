"""Starting a generation: the only entry point the views will call."""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

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
from .tasks import (
    ALTERNATIVES_KEY,
    ERROR_APPROVED,
    ERROR_NO_ASSIGNMENTS,
    run_repair_job,
    run_solve_job,
)

ERROR_ACTIVE_JOB = (
    "Una generazione di questa settimana è già in corso: attendi che finisca."
)

ERROR_STALE_JOB = (
    "La generazione si è interrotta (il processo di calcolo è stato fermato). Riprova."
)

ERROR_LOST_JOB = "La generazione non è partita (la richiesta è andata persa). Riprova."


class GenerationRefused(Exception):
    """The generation cannot start; `str(exc)` is the Italian reason."""


def _has_live_task(job_ids, now, limit):
    """The pks among `job_ids` that still have a task row able to run them.

    Live: READY, or RUNNING and claimed no longer ago than `limit` (a worker
    that is about to move the job to RUNNING). Matched by task path and by the
    job pk as first positional argument.
    """
    paths = [run_solve_job.module_path, run_repair_job.module_path]
    rows = DBTaskResult.objects.filter(task_path__in=paths).filter(
        Q(status="READY") | Q(status="RUNNING", started_at__gte=now - limit)
    )
    live = set()
    for args_kwargs in rows.values_list("args_kwargs", flat=True):
        args = (args_kwargs or {}).get("args") or []
        if args and args[0] in job_ids:
            live.add(args[0])
    return live


def fail_stale_jobs(school=None, week_start=None, now=None):
    """Fail RUNNING jobs older than timeout + margin and orphaned PENDING ones.

    A killed worker leaves its job RUNNING for ever, which blocks the week.
    The reference time is `started_at` (else `created_at`); only strictly older
    jobs qualify. A PENDING job older than the same limit is failed when no live
    task row (READY, or RUNNING claimed within the limit) exists for it: its
    request was lost. A PENDING job with a READY row is a worker that is down
    or busy and is never touched. Returns how many jobs were failed.
    """
    now = now or timezone.now()
    limit = timedelta(
        seconds=settings.SOLVER_TIMEOUT_SECONDS + settings.SOLVER_STALE_MARGIN_SECONDS
    )
    jobs = SolveJob.objects.all()
    if school is not None:
        jobs = jobs.filter(school=school)
    if week_start is not None:
        jobs = jobs.filter(week_start=week_start)
    running = jobs.filter(status=JobStatus.RUNNING).annotate(
        since=Coalesce("started_at", "created_at")
    )
    failed = 0
    for job in running.filter(since__lt=now - limit):
        failed += _fail(job, ERROR_STALE_JOB)
    pending = list(jobs.filter(status=JobStatus.PENDING, created_at__lt=now - limit))
    if pending:
        live = _has_live_task({job.pk for job in pending}, now, limit)
        for job in pending:
            if job.pk not in live:
                failed += _fail(job, ERROR_LOST_JOB)
    return failed


def _fail(job, error):
    try:
        job.transition_to(JobStatus.FAILED, error=error)
    except InvalidJobTransition:  # it finished or failed meanwhile
        return 0
    return 1


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

"""The solve task: runs in the worker process, never in a web request."""

import dataclasses
import logging
import threading

from django.conf import settings
from django.db import transaction
from django_tasks import task

from school.models import Assignment, ClassPairing

from . import audit
from .context import build_week_context
from .derogations import derogation_from_params
from .models import (
    ConflictDecision,
    Day,
    InvalidJobTransition,
    JobStatus,
    Slot,
    SolveJob,
    Timetable,
)
from .proposals import active_weights
from .services import TimetableLocked, apply_change
from .solver import SolveStatus, solve

logger = logging.getLogger(__name__)

FIRST_CLOCK_HOUR = 8
DAYS = list(Day.values)
ALTERNATIVES_KEY = "alternatives"  # job param: solve weight variations

# One solve at a time per worker process (1 GB memory target). The database
# worker is already sequential; the lock keeps that true if tasks ever run on
# threads.
_SOLVE_LOCK = threading.Lock()

ERROR_UNEXPECTED = "Errore imprevisto durante la generazione dell'orario."
ERROR_APPROVED = "La settimana è già approvata: l'orario non può essere rigenerato."
ERROR_NO_ASSIGNMENTS = "Nessuna assegnazione per la settimana."


def timeout_message(seconds):
    return (
        f"Nessuna soluzione trovata entro il tempo massimo di {seconds} secondi "
        "(timeout). Riprova."
    )


def infeasible_message():
    return "Nessun orario possibile per questa settimana."


def solve_exclusive(ctx, pinned=None, weights=None):
    """Run the solver, one at a time in this process."""
    extra = {"weights": weights} if weights is not None else {}
    with _SOLVE_LOCK:
        if pinned:
            return solve(ctx, pinned=pinned, **extra)
        return solve(ctx, **extra)


def _build_slots(timetable, lessons, school_id):
    assignments = Assignment.objects.in_bulk({le.assignment_id for le in lessons})
    pairs = {}  # (teacher, class, subject) -> the other class of the pairing
    for p in ClassPairing.objects.filter(school_id=school_id):
        pairs[p.teacher_id, p.class_a_id, p.subject_id] = p.class_b_id
        pairs[p.teacher_id, p.class_b_id, p.subject_id] = p.class_a_id
    slots = []
    for le in lessons:
        a = assignments[le.assignment_id]
        partner = pairs.get((a.teacher_id, a.school_class_id, a.subject_id))
        slots.append(
            Slot(
                timetable=timetable,
                school_class_id=a.school_class_id,
                teacher_id=a.teacher_id,
                subject_id=a.subject_id,
                day=DAYS[le.day],
                start_hour=FIRST_CLOCK_HOUR + le.hour,
                end_hour=FIRST_CLOCK_HOUR + le.hour + 1,
                paired=partner is not None,
                paired_class_id=partner,
            )
        )
    return slots


def write_timetable(
    locked, school_id, lessons, quality_score, quality_level, soft_conflicts, conflicts
):
    """Replace the slots, decisions, score and conflicts of a locked timetable."""
    locked.slots.all().delete()
    ConflictDecision.objects.filter(timetable=locked).delete()
    Slot.objects.bulk_create(_build_slots(locked, lessons, school_id))
    locked.quality_score = quality_score
    locked.quality_level = quality_level
    locked.soft_conflicts = soft_conflicts
    locked.conflicts = conflicts
    locked.save(
        update_fields=["quality_score", "quality_level", "soft_conflicts", "conflicts"]
    )


def _write_result(job, result):
    """Replace slots, score and conflicts and bump the version, atomically."""
    with transaction.atomic():
        timetable, _ = Timetable.objects.get_or_create(
            school_id=job.school_id, week_start=job.week_start
        )

        def change(locked):
            write_timetable(
                locked,
                job.school_id,
                result.lessons,
                result.quality_score,
                result.quality_level,
                result.n_soft_conflicts,
                result.conflict_items,
            )

        written = apply_change(timetable.pk, change)
        audit.record_audit(
            audit.GENERATE,
            job.requested_by,
            job.week_start,
            {
                "job": job.pk,
                "slot": len(result.lessons),
                "conflitti": len(result.conflict_items),
                "punteggio": result.quality_score,
                "livello": result.quality_level,
            },
        )
        job.transition_to(
            JobStatus.DONE,
            result={
                "timetable_id": written.pk,
                "version": written.version,
                "quality_score": result.quality_score,
                "quality_level": result.quality_level,
                "n_soft_conflicts": result.n_soft_conflicts,
                "n_slots": len(result.lessons),
                "n_conflicts": len(result.conflict_items),
                "message": result.message,
                "explanations": result.explanations,
                "explanation_unavailable": result.explanation_unavailable,
                "suggested_deroghe": result.suggested_deroghe,
            },
        )


def finish_failed(job, error):
    """Move the job to FAILED; a job already failed by recovery stays as is."""
    job.refresh_from_db()  # undo the in-memory DONE of a rolled-back write
    try:
        job.transition_to(JobStatus.FAILED, error=error)
    except InvalidJobTransition:
        logger.warning("Job %s is no longer running: left as %s", job.pk, job.status)


def run_job(job_id):
    """Run one GENERATE job to DONE or FAILED. Never raises."""
    job = SolveJob.objects.filter(pk=job_id).first()
    if job is None or job.status != JobStatus.PENDING:
        logger.warning("Solve job %s is missing or not pending: skipped", job_id)
        return
    if job.params.get(ALTERNATIVES_KEY):
        from .alternatives import run_alternatives  # it imports this module

        run_alternatives(job)
        return
    job.transition_to(JobStatus.RUNNING)
    try:
        derogation = derogation_from_params(job.params)
        extra = {"derogations": [derogation]} if derogation else {}
        ctx = build_week_context(job.week_start, job.school, **extra)
        ctx = dataclasses.replace(
            ctx,
            timeout_seconds=settings.SOLVER_TIMEOUT_SECONDS,
            explain_budget_seconds=settings.SOLVER_EXPLAIN_BUDGET_SECONDS,
        )
        result = solve_exclusive(ctx, weights=active_weights(job.school))
        if result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE):
            _write_result(job, result)
            return
        if result.status == SolveStatus.INFEASIBLE:
            error = infeasible_message()
        else:
            error = timeout_message(settings.SOLVER_TIMEOUT_SECONDS)
    except TimetableLocked:
        error = ERROR_APPROVED
    except Exception as exc:
        logger.exception("Solve job %s failed", job_id)
        error = f"{ERROR_UNEXPECTED} ({exc})"
    finish_failed(job, error)


@task
def run_solve_job(job_id):
    run_job(job_id)


@task
def run_repair_job(job_id):
    from .repair import run_repair  # repair imports this module

    run_repair(job_id)

"""Confirmed repairs stay in force for their week (#72).

A confirmed "sostituzione" (architecture 7.4) is read back, never stored anew:
the REPAIR `SolveJob` rows hold the perturbation and the append-only audit log
holds the order and the boundary. A repair is in force when its
REPAIR_CONFIRMED audit row is newer than the newest GENERATE or
ALTERNATIVE_CHOSEN row of the same school and week: regenerating the timetable
from scratch ends it. Every later re-evaluation applies them, in confirmation
order. This module must not import `repair` or `moves` at load time (they
import it).
"""

from school.models import Teacher

from . import audit
from .board import DAY_LABELS, FIRST_HOUR
from .context import build_week_context
from .models import AuditLog, Day, JobKind, JobStatus, SolveJob
from .solver import (
    SLOT_REMOVED,
    TEACHER_ABSENT,
    Perturbation,
    apply_perturbation,
)

DAYS = list(Day.values)

MSG_SLOT_REMOVED = (
    "L'ora è stata eliminata per una sostituzione confermata: non si può usare."
)


def perturbation_from_params(params):
    """The solver `Perturbation` of the `repair` entry of a job's params."""
    return Perturbation(
        kind=params["kind"],
        teacher_id=params.get("teacher_id"),
        class_id=params.get("class_id"),
        days=frozenset(DAYS.index(d) for d in params.get("days") or []),
        hour=None if params.get("hour") is None else params["hour"] - FIRST_HOUR,
    )


def repairs_in_force(school, week_start):
    """The perturbations of the week's confirmed repairs, in confirmation order.

    Three queries however many repairs there are. A job that is gone or whose
    params no longer resolve is skipped.
    """
    rows = list(
        AuditLog.objects.filter(
            school=school,
            week_start=week_start,
            action__in=[
                audit.GENERATE,
                audit.ALTERNATIVE_CHOSEN,
                audit.REPAIR_CONFIRMED,
            ],
        )
        .order_by("pk")
        .values_list("action", "details")
    )
    confirmed = []  # job ids, oldest first, since the last regeneration
    for action, details in rows:
        if action == audit.REPAIR_CONFIRMED:
            confirmed.append((details or {}).get("job"))
        else:
            confirmed = []
    jobs = {
        job.pk: job
        for job in SolveJob.objects.filter(
            school=school,
            week_start=week_start,
            kind=JobKind.REPAIR,
            status=JobStatus.DONE,
        )
    }
    perts = []
    for job_id in confirmed:
        job = jobs.get(job_id)
        if job is None or not job.result.get("applied") or job.result.get("dismissed"):
            continue
        try:
            perts.append(perturbation_from_params(job.params["repair"]))
        except (KeyError, TypeError, ValueError):
            continue
    return perts


def week_context(week_start, school, extra=()):
    """`(ctx, perts)`: the week's context with the repairs in force and `extra`.

    `extra` are further perturbations (the repair being solved or confirmed).
    `perts` lists everything applied, for `check_cell` / `check_teacher_day`.
    """
    perts = [*repairs_in_force(school, week_start), *extra]
    ctx = build_week_context(week_start, school)
    for p in perts:
        ctx = apply_perturbation(ctx, p)
    return ctx, perts


def check_cell(perts, class_ids, day, hour):
    """Raise MoveRejected if a lesson of `class_ids` may not sit on `day`/`hour`.

    `hour` is the clock hour (8-13). Only removed slots are checked here; a
    closed day is refused through the class capacity of the context.
    """
    from .moves import MoveRejected

    day_index, hour_index = DAYS.index(day), hour - FIRST_HOUR
    for p in perts:
        if (
            p.kind == SLOT_REMOVED
            and p.class_id in class_ids
            and day_index in p.days
            and p.hour == hour_index
        ):
            raise MoveRejected(MSG_SLOT_REMOVED)


def check_teacher_day(perts, teacher_id, day):
    """Raise MoveRejected if the teacher is absent on `day`."""
    from .moves import MoveRejected

    day_index = DAYS.index(day)
    for p in perts:
        if p.kind == TEACHER_ABSENT and p.teacher_id == teacher_id:
            if day_index in p.days:
                name = (
                    Teacher.objects.filter(pk=teacher_id)
                    .values_list("name", flat=True)
                    .first()
                )
                raise MoveRejected(
                    f"Il docente {name} è assente {DAY_LABELS[day].lower()} "
                    "per una sostituzione confermata."
                )

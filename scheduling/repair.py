"""Repair solving (architecture 7.4): a one-off perturbation of a draft timetable.

The perturbation (teacher absent, slot removed, class closed) lives only in the
`params` of the REPAIR `SolveJob` that was started for it and in the job's
`result`; no availability, calendar or teacher record is written. The job
computes a diff against the current timetable; nothing is applied until an
ADMIN confirms it (`confirm_repair`).
"""

import dataclasses
import logging

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction

from availability.grid import check_monday
from projects.permissions import can_repair
from school.models import Class, Subject, Teacher
from school.selectors import current_school

from .board import DAY_LABELS
from .context import build_week_context
from .models import (
    ACTIVE_JOB_STATUSES,
    Day,
    JobKind,
    JobStatus,
    Slot,
    SolveJob,
    Timetable,
    TimetableState,
)
from .moves import MoveRejected, reevaluate
from .services import TimetableLocked, apply_change
from .solver import (
    CLASS_CLOSED,
    SLOT_REMOVED,
    TEACHER_ABSENT,
    Lesson,
    Perturbation,
    SolveStatus,
    affected_lessons,
    apply_perturbation,
    diff_lessons,
)
from .tasks import (
    ERROR_APPROVED,
    ERROR_UNEXPECTED,
    _build_slots,
    infeasible_message,
    run_repair_job,
    solve_exclusive,
    timeout_message,
)

logger = logging.getLogger(__name__)

PARAMS_KEY = "repair"
FIRST_CLOCK_HOUR = 8
DAYS = list(Day.values)

ERROR_NO_TIMETABLE = "Non c'è ancora un orario da riparare: genera prima l'orario."
ERROR_ACTIVE_JOB = (
    "Una generazione o una sostituzione di questa settimana è già in corso: "
    "attendi che finisca."
)
MSG_NOTHING_TO_CHANGE = "Nessuna lezione da modificare"
MSG_STALE = (
    "L'orario è cambiato dopo il calcolo della sostituzione: "
    "ripeti la sostituzione per ottenere una proposta aggiornata."
)
MSG_ALREADY_APPLIED = "Questa sostituzione è già stata applicata."
MSG_DISMISSED = "Questa sostituzione è stata annullata: ripetila se serve."
MSG_NOT_FOUND = "Sostituzione non trovata."
MSG_NOT_READY = "La sostituzione non è ancora pronta."
MSG_NOT_EVALUATED = "Impossibile rivalutare l'orario dopo la sostituzione."

KIND_LABELS = {
    TEACHER_ABSENT: "Docente assente",
    SLOT_REMOVED: "Ora eliminata",
    CLASS_CLOSED: "Classe chiusa",
}


class RepairRefused(Exception):
    """The repair cannot start or be applied; `status` is the HTTP status."""

    status = 409

    def __init__(self, message, status=409):
        super().__init__(message)
        self.message = message
        self.status = status


def perturbation_from_params(params):
    """The solver `Perturbation` of the `repair` entry of a job's params."""
    return Perturbation(
        kind=params["kind"],
        teacher_id=params.get("teacher_id"),
        class_id=params.get("class_id"),
        days=frozenset(DAYS.index(d) for d in params.get("days") or []),
        hour=None if params.get("hour") is None else params["hour"] - FIRST_CLOCK_HOUR,
    )


def describe(params):
    """The perturbation in words, e.g. "Docente assente: ROSSI, lunedì"."""
    days = ", ".join(DAY_LABELS[d].lower() for d in params.get("days") or [])
    teacher = Teacher.objects.filter(pk=params.get("teacher_id")).first()
    school_class = Class.objects.filter(pk=params.get("class_id")).first()
    kind = params["kind"]
    if kind == TEACHER_ABSENT:
        return f"Docente assente: {teacher.name if teacher else '?'}, {days}"
    name = school_class.name if school_class else "?"
    if kind == SLOT_REMOVED:
        hour = params.get("hour")
        return f"Ora eliminata: classe {name}, {days} {hour}-{hour + 1}"
    return f"Classe chiusa: {name}, {days}"


def enqueue_repair(week_start, user, params):
    """Create a PENDING REPAIR job for the week and enqueue it; returns the job.

    `params` is the validated dict of `RepairForm.params()`. Raises
    PermissionDenied (user lacks can_repair), ValueError (not a Monday) or
    RepairRefused (no timetable, approved week, a job already active).
    """
    if not can_repair(user):
        raise PermissionDenied
    check_monday(week_start)
    school = current_school()
    timetable = Timetable.objects.filter(school=school, week_start=week_start).first()
    if timetable is None:
        raise RepairRefused(ERROR_NO_TIMETABLE)
    if timetable.state == TimetableState.APPROVATO:
        raise RepairRefused(ERROR_APPROVED)
    try:
        with transaction.atomic():
            if SolveJob.objects.filter(
                school=school, week_start=week_start, status__in=ACTIVE_JOB_STATUSES
            ).exists():
                raise RepairRefused(ERROR_ACTIVE_JOB)
            job = SolveJob.objects.create(
                school=school,
                week_start=week_start,
                kind=JobKind.REPAIR,
                requested_by=user,
                params={PARAMS_KEY: params},
            )
    except IntegrityError:  # a concurrent request created it first
        raise RepairRefused(ERROR_ACTIVE_JOB) from None
    try:
        run_repair_job.enqueue(job.pk)
    except Exception:
        job.delete()
        raise
    return job


def current_lessons(slots, ctx):
    """The solver `Lesson`s of the stored slots (rows of a paired lesson included).

    A slot whose teacher/class/subject is no longer an active assignment is left
    out: the solver cannot reason about it.
    """
    by_key = {(a.teacher_id, a.class_id, a.subject_id): a.id for a in ctx.assignments}
    lessons = {}
    for s in slots:
        aid = by_key.get((s.teacher_id, s.school_class_id, s.subject_id))
        if aid is None:
            continue
        day, hour = DAYS.index(s.day), s.start_hour - FIRST_CLOCK_HOUR
        lessons[aid, day, hour] = Lesson(aid, day, hour)
    return sorted(
        lessons.values(), key=lambda le: (le.day, le.hour, str(le.assignment_id))
    )


def _cell(cell):
    if cell is None:
        return None
    day, hour = cell
    return {
        "day": DAYS[day],
        "label": DAY_LABELS[DAYS[day]],
        "hour": FIRST_CLOCK_HOUR + hour,
    }


def _change_rows(changes, ctx, school_id):
    by_id = {a.id: a for a in ctx.assignments}
    subjects = Subject.objects.in_bulk(
        {by_id[c.assignment_id].subject_id for c in changes}
    )
    rows = []
    for c in changes:
        a = by_id[c.assignment_id]
        rows.append(
            {
                "assignment_id": a.id,
                "class_id": a.class_id,
                "teacher_id": a.teacher_id,
                "subject_id": a.subject_id,
                "class": ctx.class_names.get(a.class_id, str(a.class_id)),
                "teacher": ctx.teacher_names.get(a.teacher_id, str(a.teacher_id)),
                "subject": subjects[a.subject_id].name,
                "old": _cell(c.old),
                "new": _cell(c.new),
            }
        )
    return rows


def _summary(rows):
    moved = sum(1 for r in rows if r["old"] and r["new"])
    removed = sum(1 for r in rows if r["old"] and not r["new"])
    added = sum(1 for r in rows if r["new"] and not r["old"])
    return {
        "n_changes": len(rows),
        "n_moved": moved,
        "n_removed": removed,
        "n_added": added,
    }


def _read_week(job):
    """(timetable id, version, slots) read together under the timetable lock."""
    with transaction.atomic():
        timetable = (
            Timetable.objects.select_for_update()
            .filter(school_id=job.school_id, week_start=job.week_start)
            .first()
        )
        if timetable is None:
            raise RepairRefused(ERROR_NO_TIMETABLE)
        if timetable.state == TimetableState.APPROVATO:
            raise RepairRefused(ERROR_APPROVED)
        return (
            timetable.pk,
            timetable.version,
            list(Slot.objects.filter(timetable=timetable)),
        )


def _base_result(timetable_id, version):
    return {
        "timetable_id": timetable_id,
        "base_version": version,
        "changes": [],
        "n_changes": 0,
        "n_moved": 0,
        "n_removed": 0,
        "n_added": 0,
        "empty": True,
        "complete": True,
        "message": MSG_NOTHING_TO_CHANGE,
        "conflicts": [],
        "explanations": [],
        "explanation_unavailable": False,
        "suggested_deroghe": [],
        "applied": False,
        "dismissed": False,
    }


def _solve_repair(job):
    """The result dict of the job, or a failure message string."""
    timetable_id, version, slots = _read_week(job)
    pert = perturbation_from_params(job.params[PARAMS_KEY])
    ctx = build_week_context(job.week_start, job.school)
    current = current_lessons(slots, ctx)
    result = _base_result(timetable_id, version)
    if not affected_lessons(ctx, pert, current):
        return result
    ctx = dataclasses.replace(
        apply_perturbation(ctx, pert),
        current_lessons=current,
        timeout_seconds=settings.SOLVER_TIMEOUT_SECONDS,
        explain_budget_seconds=settings.SOLVER_EXPLAIN_BUDGET_SECONDS,
    )
    solved = solve_exclusive(ctx)
    if solved.status == SolveStatus.INFEASIBLE:
        return infeasible_message()
    if solved.status not in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE):
        return timeout_message(settings.SOLVER_TIMEOUT_SECONDS)
    rows = _change_rows(diff_lessons(current, solved.lessons), ctx, job.school_id)
    complete = not solved.message
    result.update(
        _summary(rows),
        changes=rows,
        empty=not rows,
        complete=complete,
        message=(MSG_NOTHING_TO_CHANGE if not rows else solved.message),
        conflicts=[c["description"] for c in solved.conflict_items],
        explanations=solved.explanations,
        explanation_unavailable=solved.explanation_unavailable,
        suggested_deroghe=solved.suggested_deroghe,
        quality_score=solved.quality_score,
        quality_level=solved.quality_level,
    )
    return result


def run_repair(job_id):
    """Run one REPAIR job to DONE or FAILED. Never raises."""
    job = SolveJob.objects.filter(pk=job_id, kind=JobKind.REPAIR).first()
    if job is None or job.status != JobStatus.PENDING:
        logger.warning("Repair job %s is missing or not pending: skipped", job_id)
        return
    job.transition_to(JobStatus.RUNNING)
    try:
        outcome = _solve_repair(job)
    except RepairRefused as exc:
        outcome = exc.message
    except Exception as exc:
        logger.exception("Repair job %s failed", job_id)
        outcome = f"{ERROR_UNEXPECTED} ({exc})"
    if isinstance(outcome, str):
        job.transition_to(JobStatus.FAILED, error=outcome)
    else:
        job.transition_to(JobStatus.DONE, result=outcome)


def _updated_result(job, **changes):
    job.result = {**job.result, **changes}
    job.save(update_fields=["result"])


def dismiss_repair(job_id, user):
    """Annulla: forget the proposal. The timetable is not touched."""
    if not can_repair(user):
        raise PermissionDenied
    with transaction.atomic():
        job = _finished_job(job_id, lock=True)
        if job.result.get("applied"):
            raise RepairRefused(MSG_ALREADY_APPLIED)
        _updated_result(job, dismissed=True)
    return job


def _finished_job(job_id, lock=False):
    qs = SolveJob.objects.filter(pk=job_id, kind=JobKind.REPAIR)
    job = (qs.select_for_update() if lock else qs).first()
    if job is None:
        raise RepairRefused(MSG_NOT_FOUND, status=404)
    if job.status != JobStatus.DONE:
        raise RepairRefused(MSG_NOT_READY)
    return job


def confirm_repair(job_id, user):
    """Apply the diff of a DONE job: one transaction, one version bump.

    Returns the refreshed Timetable. Raises PermissionDenied or RepairRefused
    (job unknown, already applied or dismissed, empty diff, approved week, the
    timetable changed since the job read it). A refusal writes nothing.
    """
    if not can_repair(user):
        raise PermissionDenied
    job = _finished_job(job_id)
    if job.school_id != current_school().pk:
        raise RepairRefused(MSG_NOT_FOUND, status=404)
    timetable_id = job.result.get("timetable_id")
    if not Timetable.objects.filter(pk=timetable_id).exists():
        raise RepairRefused(MSG_STALE)
    pert = perturbation_from_params(job.params[PARAMS_KEY])

    def change(locked):
        fresh = _finished_job(job_id, lock=True)
        result = fresh.result
        if result.get("applied"):
            raise RepairRefused(MSG_ALREADY_APPLIED)
        if result.get("dismissed"):
            raise RepairRefused(MSG_DISMISSED)
        if result.get("empty") or not result.get("changes"):
            raise RepairRefused(MSG_NOTHING_TO_CHANGE)
        if locked.version != result.get("base_version"):
            raise RepairRefused(MSG_STALE)
        rows = result["changes"]
        for r in rows:
            if r["old"]:
                deleted, _ = Slot.objects.filter(
                    timetable=locked,
                    school_class_id=r["class_id"],
                    teacher_id=r["teacher_id"],
                    subject_id=r["subject_id"],
                    day=r["old"]["day"],
                    start_hour=r["old"]["hour"],
                ).delete()
                if not deleted:
                    raise RepairRefused(MSG_STALE)
        new = [
            Lesson(
                r["assignment_id"],
                DAYS.index(r["new"]["day"]),
                r["new"]["hour"] - FIRST_CLOCK_HOUR,
            )
            for r in rows
            if r["new"]
        ]
        Slot.objects.bulk_create(_build_slots(locked, new, locked.school_id))
        ctx = apply_perturbation(
            build_week_context(locked.week_start, locked.school), pert
        )
        try:
            reevaluate(locked, ctx, list(Slot.objects.filter(timetable=locked)))
        except MoveRejected:
            raise RepairRefused(MSG_NOT_EVALUATED) from None
        _updated_result(fresh, applied=True)

    try:
        return apply_change(timetable_id, change)
    except TimetableLocked:
        raise RepairRefused(ERROR_APPROVED) from None

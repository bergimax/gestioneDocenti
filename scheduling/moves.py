"""Moving one lesson to another day/hour (specs 10, "Spostare una lezione").

The server decides everything: the move is validated, the whole week is
re-evaluated with every lesson pinned (forced mode, so what the move breaks
becomes a conflict instead of an error) and the result is stored in a single
``apply_change`` transaction that bumps ``Timetable.version`` once.
"""

import dataclasses

from django.conf import settings

from school.models import Assignment

from . import audit
from .board import FIRST_HOUR, HOURS
from .models import Day, Slot, Timetable
from .perturbations import check_cell, check_teacher_day, week_context
from .services import apply_change
from .solver import Lesson, SolveStatus
from .tasks import solve_exclusive

DAYS = list(Day.values)

MSG_STALE = (
    "L'orario è stato modificato nel frattempo da un altro utente: "
    "ecco la versione aggiornata, riprova lo spostamento."
)
MSG_OCCUPIED = "L'ora di destinazione è già occupata da un'altra lezione della classe."
MSG_STAGE = "La classe è in stage in quel giorno: impossibile spostare la lezione."
MSG_NO_HOURS = "La classe non ha ore di lezione in quel giorno."
MSG_NO_ASSIGNMENT = "La lezione non corrisponde più a un'assegnazione valida."
MSG_NOT_EVALUATED = "Impossibile rivalutare l'orario dopo lo spostamento."
MSG_SLOT = "Lezione non trovata in questo orario."


class MoveRejected(Exception):
    """The move is refused and nothing changed. `status` is the HTTP status."""

    status = 409

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class InvalidMove(MoveRejected):
    status = 400


def _counterparts(slot):
    """The other row of a paired lesson (the partner class's own slot), if any."""
    if not slot.paired_class_id:
        return []
    return list(
        Slot.objects.filter(
            timetable_id=slot.timetable_id,
            school_class_id=slot.paired_class_id,
            paired_class_id=slot.school_class_id,
            teacher_id=slot.teacher_id,
            subject_id=slot.subject_id,
            day=slot.day,
            start_hour=slot.start_hour,
        ).exclude(pk=slot.pk)
    )


def _check_target(ctx, slots, group, class_ids, day, hour):
    day_index = DAYS.index(day)
    for class_id in class_ids:
        if day_index in ctx.stage_days.get(class_id, ()):
            raise MoveRejected(MSG_STAGE)
        if ctx.class_capacity.get(class_id, {}).get(day_index, HOURS) == 0:
            raise MoveRejected(MSG_NO_HOURS)
    moved = {s.pk for s in group}
    for other in slots:
        if other.pk in moved or other.day != day or other.start_hour != hour:
            continue
        if {other.school_class_id, other.paired_class_id} & class_ids:
            raise MoveRejected(MSG_OCCUPIED)


def _pins(school_id, slots, moving):
    """Every lesson of the week as a pinned Lesson; `moving` must be one."""
    by_key = {
        (a.teacher_id, a.school_class_id, a.subject_id): a.pk
        for a in Assignment.objects.filter(school_id=school_id, teacher__active=True)
    }
    pins = {}
    for s in slots:
        assignment_id = by_key.get((s.teacher_id, s.school_class_id, s.subject_id))
        if assignment_id is None:
            if moving is not None and s.pk == moving.pk:
                raise MoveRejected(MSG_NO_ASSIGNMENT)
            continue  # no longer an assignment: nothing to evaluate it against
        pins[assignment_id, s.day, s.start_hour] = Lesson(
            assignment_id, DAYS.index(s.day), s.start_hour - FIRST_HOUR
        )
    return list(pins.values())


def reevaluate(locked, ctx, slots, moving=None):
    """Re-evaluate the whole week with exactly `slots` pinned and store it.

    Forced mode: what the lessons break becomes a conflict. With every teacher
    capped at the lessons pinned for them the solver cannot add any (filling an
    empty hour is a separate, explicit action). `moving` is the edited lesson;
    it must still be an assignment. Stores score, level and conflicts on
    `locked` (the caller's transaction bumps the version).
    """
    pinned = _pins(locked.school_id, slots, moving)
    ctx = dataclasses.replace(
        ctx,
        availability_relaxed=True,
        weekly_caps={a.teacher_id: 0 for a in ctx.assignments},
        timeout_seconds=settings.SOLVER_TIMEOUT_SECONDS,
    )
    result = solve_exclusive(ctx, pinned=pinned)
    if result.status not in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE):
        raise MoveRejected(MSG_NOT_EVALUATED)
    locked.quality_score = result.quality_score
    locked.quality_level = result.quality_level
    locked.soft_conflicts = result.n_soft_conflicts
    locked.conflicts = result.conflict_items
    locked.save(
        update_fields=["quality_score", "quality_level", "soft_conflicts", "conflicts"]
    )


def move_lesson(timetable_id, slot_id, day, hour, version, user=None):
    """Move one lesson (and its partner row when paired) to `day`/`hour`.

    Returns ``(timetable, moved)``; `moved` is False for a move onto the
    lesson's own cell (a no-op: nothing written, version untouched). Raises
    ``InvalidMove`` (400) when the slot is not in this timetable and
    ``MoveRejected`` (409) for a stale version, a refused target, an approved
    week (``TimetableLocked`` from ``apply_change``) or a failed evaluation.
    A successful move writes one audit entry for `user`.
    Any error rolls back the whole write.
    """
    timetable = Timetable.objects.get(pk=timetable_id)
    slot = Slot.objects.filter(pk=slot_id, timetable=timetable).first()
    if slot is None:
        raise InvalidMove(MSG_SLOT)
    if timetable.version != version:
        raise MoveRejected(MSG_STALE)
    if slot.day == day and slot.start_hour == hour:
        return timetable, False

    def change(locked):
        if locked.version != version:
            raise MoveRejected(MSG_STALE)
        slots = list(Slot.objects.filter(timetable=locked))
        by_pk = {s.pk: s for s in slots}
        if slot_id not in by_pk:
            raise MoveRejected(MSG_STALE)
        moving = by_pk[slot_id]
        group = [moving] + [by_pk[s.pk] for s in _counterparts(moving)]
        class_ids = {moving.school_class_id, moving.paired_class_id} - {None}
        ctx, perts = week_context(locked.week_start, locked.school)
        _check_target(ctx, slots, group, class_ids, day, hour)
        check_cell(perts, class_ids, day, hour)
        check_teacher_day(perts, moving.teacher_id, day)
        for s in group:
            s.day, s.start_hour, s.end_hour = day, hour, hour + 1
        reevaluate(locked, ctx, slots, moving)
        Slot.objects.bulk_update(group, ["day", "start_hour", "end_hour"])
        audit.record_audit(
            audit.MODIFY_SLOT,
            user,
            locked.week_start,
            {"azione": "sposta", "slot": slot_id, "giorno": day, "ora": hour},
        )

    return apply_change(timetable_id, change), True

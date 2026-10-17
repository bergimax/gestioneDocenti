"""Replacing the teacher of one lesson (specs 10, "Spostare una lezione").

The lesson keeps its day, hour, class and subject. Like a move, the week is
re-evaluated with every lesson pinned (forced mode), so what the new teacher
breaks becomes a conflict, not an error. Everything happens in one
``apply_change`` transaction that bumps ``Timetable.version`` once.
"""

from school.models import Assignment, ClassPairing

from . import audit
from .assign import MSG_PARTNER_ASSIGNMENT, assignable_pairs
from .models import Slot, Timetable
from .moves import (
    MSG_SLOT,
    MSG_STALE,
    InvalidMove,
    MoveRejected,
    _counterparts,
    reevaluate,
)
from .perturbations import check_teacher_day, week_context
from .services import apply_change

MSG_NOT_CANDIDATE = (
    "Il docente scelto non ha un'assegnazione per la classe e la materia della lezione."
)
MSG_NO_PAIRING = (
    "Il docente scelto non ha l'abbinamento delle due classi per questa materia."
)


def _partner_ok(school_id, slot, teacher_id):
    """(has the partner-class assignment, has the pairing) of a paired lesson."""
    has_assignment = Assignment.objects.filter(
        school_id=school_id,
        school_class_id=slot.paired_class_id,
        teacher_id=teacher_id,
        subject_id=slot.subject_id,
    ).exists()
    low, high = sorted((slot.school_class_id, slot.paired_class_id))
    has_pairing = ClassPairing.objects.filter(
        school_id=school_id,
        class_a_id=low,
        class_b_id=high,
        subject_id=slot.subject_id,
        teacher_id=teacher_id,
    ).exists()
    return has_assignment, has_pairing


def candidates(timetable, slot):
    """Teachers who may replace the one of `slot`, with remaining hours.

    Active teachers other than the current one assigned to the lesson's class
    and subject; for a paired lesson also assigned in the partner class and
    with the pairing. Sorted by name.
    """
    school = timetable.school
    rows = [
        r
        for r in assignable_pairs(school, slot.school_class, timetable)
        if r["subject_id"] == slot.subject_id and r["teacher_id"] != slot.teacher_id
    ]
    if slot.paired_class_id:
        rows = [r for r in rows if all(_partner_ok(school.pk, slot, r["teacher_id"]))]
    rows.sort(key=lambda r: (r["teacher"].lower(), r["teacher_id"]))
    return rows


def change_teacher(timetable_id, slot_id, teacher_id, version, user=None):
    """Give the lesson `slot_id` (and its partner row) to teacher `teacher_id`.

    Returns ``(timetable, changed)``; `changed` is False when the teacher is
    already the lesson's (nothing written, version untouched). Raises
    ``InvalidMove`` (400) for a slot not in this timetable or a teacher who is
    unknown, inactive or not assigned to the class and subject, and
    ``MoveRejected`` (409) for a stale version, a paired lesson whose new
    teacher lacks the partner assignment or pairing, an approved week
    (``TimetableLocked``) or a failed evaluation. Any error rolls back.
    """
    timetable = Timetable.objects.select_related("school").get(pk=timetable_id)
    slot = Slot.objects.filter(pk=slot_id, timetable=timetable).first()
    if slot is None:
        raise InvalidMove(MSG_SLOT)
    if (
        teacher_id != slot.teacher_id
        and not Assignment.objects.filter(
            school=timetable.school,
            school_class_id=slot.school_class_id,
            subject_id=slot.subject_id,
            teacher_id=teacher_id,
            teacher__active=True,
        ).exists()
    ):
        raise InvalidMove(MSG_NOT_CANDIDATE)
    if timetable.version != version:
        raise MoveRejected(MSG_STALE)
    if teacher_id == slot.teacher_id:
        return timetable, False

    def change(locked):
        if locked.version != version:
            raise MoveRejected(MSG_STALE)
        slots = list(Slot.objects.filter(timetable=locked))
        by_pk = {s.pk: s for s in slots}
        if slot_id not in by_pk:
            raise MoveRejected(MSG_STALE)
        moving = by_pk[slot_id]
        old_teacher = moving.teacher_id
        if moving.paired_class_id:
            has_assignment, has_pairing = _partner_ok(
                locked.school_id, moving, teacher_id
            )
            if not has_assignment:
                raise MoveRejected(MSG_PARTNER_ASSIGNMENT)
            if not has_pairing:
                raise MoveRejected(MSG_NO_PAIRING)
        group = [moving] + [by_pk[s.pk] for s in _counterparts(moving)]
        ctx, perts = week_context(locked.week_start, locked.school)
        check_teacher_day(perts, teacher_id, moving.day)
        for s in group:
            s.teacher_id = teacher_id
        reevaluate(locked, ctx, slots, moving)
        Slot.objects.bulk_update(group, ["teacher"])
        audit.record_audit(
            audit.MODIFY_SLOT,
            user,
            locked.week_start,
            {
                "azione": "cambia_docente",
                "slot": slot_id,
                "da": old_teacher,
                "a": teacher_id,
            },
        )

    return apply_change(timetable_id, change), True

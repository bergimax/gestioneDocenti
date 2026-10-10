"""Filling an empty hour (specs 10, "Assegnare un'ora libera").

The lesson is placed even when it breaks rules: the week is re-evaluated with
every lesson pinned (forced mode, as for a move) and the violations become
conflicts. Only an hour the class cannot hold is refused. All writes happen in
one ``apply_change`` transaction that bumps ``Timetable.version`` once.
"""

from school import assignment_hours
from school.models import Assignment, ClassPairing

from .board import FIRST_HOUR, HOURS
from .context import build_week_context
from .decisions import free_key
from .models import ConflictDecision, Day, DecisionAction, Slot, Timetable
from .moves import MSG_STALE, InvalidMove, MoveRejected, reevaluate
from .services import apply_change

DAYS = list(Day.values)

MSG_BAD_PAIR = "Il docente e la materia scelti non sono un'assegnazione della classe."
MSG_OCCUPIED = "L'ora scelta è già occupata da una lezione della classe."
MSG_STAGE = "La classe è in stage in quel giorno: impossibile assegnare la lezione."
MSG_NO_HOURS = "La classe non ha ore di lezione in quel giorno."
MSG_FULL = (
    "La classe ha già tutte le ore di lezione di quel giorno: "
    "impossibile assegnare un'altra lezione."
)
MSG_PARTNER_ASSIGNMENT = (
    "La classe abbinata non ha l'assegnazione corrispondente: lezione non assegnabile."
)


def assignable_pairs(school, school_class, timetable):
    """The class's assignments with teacher, subject and remaining hours.

    Remaining hours are the total minus the delivered ones and the lessons
    already placed for the class in `timetable`. Sorted by subject; each
    assignment appears once.
    """
    placed = {}
    for t, s in Slot.objects.filter(
        timetable=timetable, school_class=school_class
    ).values_list("teacher_id", "subject_id"):
        placed[t, s] = placed.get((t, s), 0) + 1
    delivered = assignment_hours.delivered_hours_map(school)
    rows = [
        {
            "teacher_id": a.teacher_id,
            "teacher": a.teacher.name,
            "subject_id": a.subject_id,
            "subject": a.subject.name,
            "remaining": a.ore_totali
            - delivered.get(a.pk, 0)
            - placed.get((a.teacher_id, a.subject_id), 0),
        }
        for a in Assignment.objects.filter(
            school=school, school_class=school_class, teacher__active=True
        ).select_related("teacher", "subject")
    ]
    rows.sort(
        key=lambda r: (r["subject"].lower(), r["teacher"].lower(), r["teacher_id"])
    )
    return rows


def _partner_class_id(school_id, teacher_id, subject_id, class_id):
    pairing = ClassPairing.objects.filter(
        school_id=school_id, teacher_id=teacher_id, subject_id=subject_id
    ).filter(class_a_id=class_id) | ClassPairing.objects.filter(
        school_id=school_id,
        teacher_id=teacher_id,
        subject_id=subject_id,
        class_b_id=class_id,
    )
    pairing = pairing.first()
    if pairing is None:
        return None
    return pairing.class_b_id if pairing.class_a_id == class_id else pairing.class_a_id


def _on_day(slots, class_id, day):
    """Hours of `class_id` taken on `day` (a paired lesson shows in both)."""
    return {
        s.start_hour
        for s in slots
        if s.day == day and class_id in (s.school_class_id, s.paired_class_id)
    }


def _check_cell(ctx, slots, class_id, day, hour):
    day_index = DAYS.index(day)
    if day_index in ctx.stage_days.get(class_id, ()):
        raise MoveRejected(MSG_STAGE)
    capacity = ctx.class_capacity.get(class_id, {}).get(day_index, HOURS)
    if capacity == 0:
        raise MoveRejected(MSG_NO_HOURS)
    taken = _on_day(slots, class_id, day)
    if hour in taken:
        raise MoveRejected(MSG_OCCUPIED)
    if len(taken) + 1 > capacity:
        raise MoveRejected(MSG_FULL)


def assign_lesson(timetable_id, class_id, day, hour, teacher_id, subject_id, version):
    """Place a (teacher, subject) lesson of class `class_id` at `day`/`hour`.

    Returns the refreshed timetable. Raises ``InvalidMove`` (400) for a class,
    teacher, subject or pair that is not valid, ``MoveRejected`` (409) for a
    stale version, an occupied or full cell, a stage or closed day, an approved
    week or a failed evaluation. Any error rolls back the whole write.
    """
    timetable = Timetable.objects.select_related("school").get(pk=timetable_id)
    school = timetable.school
    assignment = (
        Assignment.objects.filter(
            school=school,
            school_class_id=class_id,
            teacher_id=teacher_id,
            subject_id=subject_id,
            teacher__active=True,
        )
        .select_related("school_class")
        .first()
    )
    if assignment is None:
        raise InvalidMove(MSG_BAD_PAIR)
    if timetable.version != version:
        raise MoveRejected(MSG_STALE)

    def change(locked):
        if locked.version != version:
            raise MoveRejected(MSG_STALE)
        slots = list(Slot.objects.filter(timetable=locked))
        partner = _partner_class_id(school.pk, teacher_id, subject_id, class_id)
        if (
            partner is not None
            and not Assignment.objects.filter(
                school=school,
                school_class_id=partner,
                teacher_id=teacher_id,
                subject_id=subject_id,
            ).exists()
        ):
            raise MoveRejected(MSG_PARTNER_ASSIGNMENT)
        ctx = build_week_context(locked.week_start, school)
        class_ids = [class_id] + ([partner] if partner is not None else [])
        for cid in class_ids:
            _check_cell(ctx, slots, cid, day, hour)
        created = []
        for cid, other in zip(class_ids, class_ids[::-1], strict=False):
            created.append(
                Slot.objects.create(
                    timetable=locked,
                    school_class_id=cid,
                    teacher_id=teacher_id,
                    subject_id=subject_id,
                    day=day,
                    start_hour=hour,
                    end_hour=hour + 1,
                    paired=partner is not None,
                    paired_class_id=other if partner is not None else None,
                )
            )
        # The hour is no longer deliberately free.
        ConflictDecision.objects.filter(
            timetable=locked,
            action=DecisionAction.LIBERA,
            chiave__in=[free_key(cid, day, hour - FIRST_HOUR) for cid in class_ids],
        ).delete()
        reevaluate(locked, ctx, slots + created, created[0])

    return apply_change(timetable_id, change)

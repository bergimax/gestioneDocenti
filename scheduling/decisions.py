"""Admin decisions on conflicts (specs 10, "Conflitti").

Approve: the conflict, identified by its stable `chiave`, is no longer
reported. Reject: the lesson is removed, the hour stays free and a LIBERA
decision records it. Stored scores and conflicts are never recomputed to hide
a conflict: reading filters the list by the decisions.
"""

from . import audit
from .board import FIRST_HOUR, decided_keys
from .context import build_week_context
from .models import ConflictDecision, Day, DecisionAction, Slot, Timetable
from .moves import MSG_STALE, InvalidMove, MoveRejected, reevaluate
from .services import apply_change
from .solver import CLASSE_UNFILLED
from .solver.conflicts import chiave

DAYS = list(Day.values)

MSG_EMPTY = "L'ora scelta è già libera: non c'è nessuna lezione da rifiutare."
MSG_BAD_CLASS = "Classe non valida."


def free_key(class_id, day, hour_index):
    """Key of the "hour not covered" conflict of a class cell.

    `day` is the weekday name, `hour_index` 0-5. A LIBERA decision stores it,
    so the hour left free on purpose is not reported as a hole.
    """
    return chiave(CLASSE_UNFILLED, class_id, None, DAYS.index(day), hour_index)


def approve_conflicts(timetable_id, chiavi, version, user=None):
    """Store one APPROVATO decision per known, not yet decided key.

    Returns ``(timetable, changed)``. Unknown and repeated keys are ignored;
    when nothing is new nothing is written and the version is not bumped.
    """
    timetable = Timetable.objects.get(pk=timetable_id)
    if timetable.version != version:
        raise MoveRejected(MSG_STALE)

    def new_keys(locked):
        known = {c.get("chiave") for c in locked.conflicts or [] if isinstance(c, dict)}
        wanted = [k for k in dict.fromkeys(chiavi) if k in known]
        return [k for k in wanted if k not in decided_keys(locked)]

    if not new_keys(timetable):
        return timetable, False

    def change(locked):
        if locked.version != version:
            raise MoveRejected(MSG_STALE)
        keys = new_keys(locked)
        ConflictDecision.objects.bulk_create(
            [
                ConflictDecision(
                    timetable=locked, chiave=k, action=DecisionAction.APPROVATO
                )
                for k in keys
            ]
        )
        audit.record_audit(
            audit.CONFLICT_DECISION,
            user,
            locked.week_start,
            {"azione": "approva", "chiavi": keys},
        )

    return apply_change(timetable_id, change), True


def reject_lesson(timetable_id, class_id, day, hour, version, user=None):
    """Remove the lesson of a class cell (and its paired row) and keep it free."""
    timetable = Timetable.objects.select_related("school").get(pk=timetable_id)
    if not timetable.school.classes.filter(pk=class_id).exists():
        raise InvalidMove(MSG_BAD_CLASS)
    if timetable.version != version:
        raise MoveRejected(MSG_STALE)

    def change(locked):
        if locked.version != version:
            raise MoveRejected(MSG_STALE)
        slots = list(Slot.objects.filter(timetable=locked))
        found = [
            s
            for s in slots
            if s.day == day
            and s.start_hour == hour
            and class_id in (s.school_class_id, s.paired_class_id)
        ]
        if not found:
            raise MoveRejected(MSG_EMPTY)
        # A paired lesson has a row in each class: both match the cell.
        gone = {s.pk: s for s in found}
        class_ids = set()
        for s in gone.values():
            class_ids |= {s.school_class_id, s.paired_class_id} - {None}
        Slot.objects.filter(pk__in=gone).delete()
        for cid in class_ids:
            ConflictDecision.objects.update_or_create(
                timetable=locked,
                chiave=free_key(cid, day, hour - FIRST_HOUR),
                defaults={"action": DecisionAction.LIBERA},
            )
        remaining = [s for s in slots if s.pk not in gone]
        ctx = build_week_context(locked.week_start, locked.school)
        reevaluate(locked, ctx, remaining)
        audit.record_audit(
            audit.CONFLICT_DECISION,
            user,
            locked.week_start,
            {"azione": "rifiuta", "classe": class_id, "giorno": day, "ora": hour},
        )

    return apply_change(timetable_id, change)

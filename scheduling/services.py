"""Writes to a timetable. All board mutations go through apply_change."""

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from ledger.models import EntryKind, LedgerEntry
from school.models import Assignment

from . import audit
from .models import Timetable, TimetableState


class TimetableLocked(Exception):
    """The timetable is APPROVATO: no changes allowed."""


class ForbiddenTimetableChange(Exception):
    """A change function touched state or approved_at."""


class NotApproved(Exception):
    """The week is not APPROVATO: there is nothing to undo."""


class ReasonRequired(Exception):
    """A reason is mandatory and was empty."""


def apply_change(timetable_id, change_fn):
    """Run `change_fn(locked_timetable)` and bump the version exactly once.

    Everything happens in one transaction on a row locked with
    SELECT FOR UPDATE; any exception rolls back edits and bump together.
    Returns the refreshed Timetable. The bump also happens when the change
    edits nothing.
    """
    with transaction.atomic():
        locked = Timetable.objects.select_for_update().get(pk=timetable_id)
        if locked.state == TimetableState.APPROVATO:
            raise TimetableLocked(timetable_id)
        state, approved_at = locked.state, locked.approved_at
        change_fn(locked)
        current = Timetable.objects.values("state", "approved_at").get(pk=timetable_id)
        if current["state"] != state or current["approved_at"] != approved_at:
            raise ForbiddenTimetableChange(timetable_id)
        Timetable.objects.filter(pk=timetable_id).update(version=F("version") + 1)
        return Timetable.objects.get(pk=timetable_id)


def approve_timetable(timetable_id, user):
    """Approve the week: the only code that sets state APPROVATO (specs 8).

    One transaction on a locked row: one DELIVERED ledger entry of 1 hour per
    lesson for the assignment of its class (and of its paired class), state,
    approved_at, the version bump and the audit call. Approving an approved
    week writes nothing. Returns the numbers of the audit params.
    """
    with transaction.atomic():
        locked = Timetable.objects.select_for_update().get(pk=timetable_id)
        if locked.state == TimetableState.APPROVATO:
            return {
                "ore_scalate": 0,
                "assegnazioni_aggiornate": 0,
                "senza_monte_ore": 0,
            }
        assignments = {
            (a.teacher_id, a.school_class_id, a.subject_id): a.pk
            for a in Assignment.objects.filter(school_id=locked.school_id)
        }
        entries, touched, missing = [], set(), 0
        for slot in locked.slots.order_by("pk"):
            for class_id in (slot.school_class_id, slot.paired_class_id):
                if class_id is None:
                    continue
                pk = assignments.get((slot.teacher_id, class_id, slot.subject_id))
                if pk is None:
                    missing += 1
                    continue
                touched.add(pk)
                entries.append(
                    LedgerEntry(
                        assignment_id=pk,
                        timetable=locked,
                        week=locked.week_start,
                        hours=1,
                        kind=EntryKind.DELIVERED,
                    )
                )
        LedgerEntry.objects.bulk_create(entries)
        Timetable.objects.filter(pk=timetable_id).update(
            state=TimetableState.APPROVATO,
            approved_at=timezone.now(),
            version=F("version") + 1,
        )
        result = {
            "ore_scalate": len(entries),
            "assegnazioni_aggiornate": len(touched),
            "senza_monte_ore": missing,
        }
        audit.record_audit(audit.SCHEDULE_APPROVED, user, locked.week_start, result)
        return result


def undo_approval(timetable_id, user, reason):
    """Return an approved week to BOZZA with compensating ledger entries.

    One transaction on the locked row. For each assignment, one REVERSAL
    entry of minus the net DELIVERED + REVERSAL hours of this timetable
    (zero nets are skipped); entries are never edited or deleted and
    ADJUSTMENT rows are untouched. Raises NotApproved on a draft week and
    ReasonRequired on a blank reason, writing nothing. Returns the number of
    reversed hours and assignments.
    """
    reason = (reason or "").strip()
    if not reason or len(reason) > 300:
        raise ReasonRequired
    with transaction.atomic():
        locked = Timetable.objects.select_for_update().get(pk=timetable_id)
        if locked.state != TimetableState.APPROVATO:
            raise NotApproved(timetable_id)
        nets = (
            LedgerEntry.objects.filter(
                timetable=locked, kind__in=[EntryKind.DELIVERED, EntryKind.REVERSAL]
            )
            .values("assignment_id")
            .annotate(total=Sum("hours"))
            .order_by("assignment_id")
        )
        entries = [
            LedgerEntry(
                assignment_id=row["assignment_id"],
                timetable=locked,
                week=locked.week_start,
                hours=-row["total"],
                kind=EntryKind.REVERSAL,
                reason=reason,
                created_by=user,
            )
            for row in nets
            if row["total"] != 0
        ]
        LedgerEntry.objects.bulk_create(entries)
        Timetable.objects.filter(pk=timetable_id).update(
            state=TimetableState.BOZZA,
            approved_at=None,
            version=F("version") + 1,
        )
        result = {
            "ore_annullate": -sum(e.hours for e in entries),
            "assegnazioni_aggiornate": len(entries),
            "motivo": reason,
        }
        audit.record_audit(audit.APPROVAL_UNDONE, user, locked.week_start, result)
        return result

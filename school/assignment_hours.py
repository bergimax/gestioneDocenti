"""Hours of an assignment: the only code that reads delivered hours and the
only code that writes `ore_totali` after creation (issue #12).

Remaining hours are `ore_totali - delivered` (specs section 8) and may be
negative. `delivered_hours_map` is one aggregate over the ledger (#36); #37
changes `set_assignment_total_hours`.
"""

from django.db import transaction
from django.db.models import Sum


def delivered_hours_map(school):
    """Delivered hours per assignment id of `school`; a missing id means 0.

    One aggregate over the ledger (sum of `hours` per assignment).
    """
    from ledger.models import LedgerEntry

    rows = (
        LedgerEntry.objects.filter(assignment__school=school)
        .values("assignment_id")
        .annotate(total=Sum("hours"))
        .order_by()
    )
    return {row["assignment_id"]: row["total"] for row in rows}


def set_assignment_total_hours(assignment, hours):
    """Set the total hours of `assignment` and return it."""
    with transaction.atomic():
        assignment.ore_totali = hours
        assignment.save(update_fields=["ore_totali"])
    return assignment


class NegativeDelivered(Exception):
    """The new delivered value is below zero."""


class AdjustmentReasonRequired(Exception):
    """A changed value needs a reason."""


def adjust_delivered_hours(assignment, new_delivered, user, reason):
    """Set the delivered hours of `assignment` with an ADJUSTMENT entry (#37).

    The only writer of manual corrections. Locks the Assignment row, computes
    the delta against the current ledger sum and writes one entry when it is
    not zero. Returns `{"changed", "old", "new", "hours"}`.
    """
    from ledger.models import EntryKind, LedgerEntry
    from scheduling import audit

    from .models import Assignment

    reason = (reason or "").strip()
    with transaction.atomic():
        Assignment.objects.select_for_update().get(pk=assignment.pk)
        current = (
            LedgerEntry.objects.filter(assignment_id=assignment.pk).aggregate(
                total=Sum("hours")
            )["total"]
            or 0
        )
        delta = new_delivered - current
        result = {
            "changed": delta != 0,
            "old": current,
            "new": new_delivered,
            "hours": delta,
        }
        if delta == 0:
            return result
        if new_delivered < 0:
            raise NegativeDelivered(new_delivered)
        if not reason:
            raise AdjustmentReasonRequired
        LedgerEntry.objects.create(
            assignment_id=assignment.pk,
            hours=delta,
            kind=EntryKind.ADJUSTMENT,
            reason=reason,
            created_by=user,
        )
        audit.record_audit(
            audit.HOURS_ADJUSTED,
            user,
            None,
            {
                "assegnazione": assignment.pk,
                "ore_erogate_prima": current,
                "ore_erogate_dopo": new_delivered,
                "ore": delta,
                "motivo": reason,
            },
        )
        return result

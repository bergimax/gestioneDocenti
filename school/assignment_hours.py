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

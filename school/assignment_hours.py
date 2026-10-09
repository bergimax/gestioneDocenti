"""Hours of an assignment: the only code that reads delivered hours and the
only code that writes `ore_totali` after creation (issue #12).

Remaining hours are `ore_totali - delivered` (specs section 8) and may be
negative. #36 replaces the body of `delivered_hours_map` with one aggregate
over the ledger; #37 changes `set_assignment_total_hours`.
"""

from django.db import transaction


def delivered_hours_map(school):
    """Delivered hours per assignment id of `school`; a missing id means 0.

    No ledger exists yet, so nothing has been delivered and no query is run.
    """
    return {}


def set_assignment_total_hours(assignment, hours):
    """Set the total hours of `assignment` and return it."""
    with transaction.atomic():
        assignment.ore_totali = hours
        assignment.save(update_fields=["ore_totali"])
    return assignment

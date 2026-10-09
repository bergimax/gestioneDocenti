"""Queries and writes around availability grids; logic lives in grid.py."""

from django.db import transaction

from school.models import Teacher

from . import grid as grid_logic
from .models import Availability


def resolve_teacher_grid(teacher, week_start):
    """Read-only resolution for one teacher and week (see grid.resolve_grid)."""
    grid_logic.check_monday(week_start)
    stored = dict(
        Availability.objects.filter(
            teacher=teacher, week_start__lte=week_start
        ).values_list("week_start", "slots")
    )
    return grid_logic.resolve_grid(week_start, stored, teacher.first_time)


def week_status(school, week_start):
    """Completion of the week for the active teachers of `school`."""
    grid_logic.check_monday(week_start)
    teachers = list(
        Teacher.objects.filter(school=school, active=True).order_by("name", "pk")
    )
    filled_ids = set(
        Availability.objects.filter(
            school=school, week_start=week_start, teacher__in=teachers
        ).values_list("teacher_id", flat=True)
    )
    missing = [t for t in teachers if t.pk not in filled_ids]
    total = len(teachers)
    filled = total - len(missing)
    complete = total > 0 and not missing
    return {
        "total_teachers": total,
        "teachers_with_availability": filled,
        "teachers_missing": missing,
        "completion_percentage": grid_logic.completion_percentage(total, filled),
        "week_complete": complete,
        "ready_to_generate": complete,
    }


def school_weeks(school):
    return grid_logic.list_weeks(school.year_start, school.year_end)


@transaction.atomic
def save_availability(teacher, week_start, grid):
    """Create or update the record of `teacher` for `week_start`.

    Raises ValueError (non-Monday) or ValidationError (invalid grid) and
    writes nothing in that case.
    """
    grid_logic.check_monday(week_start)
    obj = (
        Availability.objects.select_for_update()
        .filter(teacher=teacher, week_start=week_start)
        .first()
    )
    if obj is None:
        obj = Availability(
            school_id=teacher.school_id, teacher=teacher, week_start=week_start
        )
    obj.slots = grid
    obj.full_clean(validate_unique=False)
    obj.save()
    return obj

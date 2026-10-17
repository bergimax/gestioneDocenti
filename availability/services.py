"""Queries and writes around availability grids; logic lives in grid.py."""

from django.db import transaction

from scheduling.models import Timetable, TimetableState
from school.models import Teacher

from . import grid as grid_logic
from .models import Availability


def week_is_approved(school, week_start, lock=False):
    """True when the timetable of `school` for `week_start` is APPROVATO.

    With `lock` the Timetable row is locked (select_for_update, as
    approve_timetable does) so the caller must be inside a transaction.
    """
    qs = Timetable.objects.filter(school=school, week_start=week_start)
    if lock:
        qs = qs.select_for_update()
    return any(t.state == TimetableState.APPROVATO for t in qs)


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


def copy_previous_week(school, week_start):
    """Copy last week's stored records to teachers with none in `week_start`.

    Only active teachers are considered; teachers already filled in the
    selected week are left untouched and counted as skipped. Returns
    {"previous_week", "previous_records", "copied", "skipped"}.
    """
    grid_logic.check_monday(week_start)
    previous = grid_logic.previous_week(school_weeks(school), week_start)
    result = {
        "previous_week": previous,
        "previous_records": 0,
        "copied": 0,
        "skipped": 0,
    }
    if previous is None:
        return result
    with transaction.atomic():
        sources = list(
            Availability.objects.filter(
                school=school, week_start=previous, teacher__active=True
            ).select_related("teacher")
        )
        result["previous_records"] = len(sources)
        filled = set(
            Availability.objects.filter(
                school=school, week_start=week_start
            ).values_list("teacher_id", flat=True)
        )
        for source in sources:
            if source.teacher_id in filled:
                result["skipped"] += 1
                continue
            save_availability(source.teacher, week_start, source.slots)
            result["copied"] += 1
    return result


def school_rows(school, week_start):
    """Active teachers of `school` with the grid shown for `week_start`.

    One query for the stored records; resolution as in resolve_teacher_grid.
    Each item: {"teacher", "resolution", "grid", "stored"}.
    """
    grid_logic.check_monday(week_start)
    teachers = list(
        Teacher.objects.filter(school=school, active=True).order_by("name", "pk")
    )
    stored = {}
    for tid, week, slots in Availability.objects.filter(
        school=school, week_start__lte=week_start, teacher__in=teachers
    ).values_list("teacher_id", "week_start", "slots"):
        stored.setdefault(tid, {})[week] = slots
    rows = []
    for teacher in teachers:
        resolution = grid_logic.resolve_grid(
            week_start, stored.get(teacher.pk, {}), teacher.first_time
        )
        rows.append({"teacher": teacher, "resolution": resolution})
    return rows

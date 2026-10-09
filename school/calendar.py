"""Day resolution (specs section 7). Pure: no Django, no database."""

MAX_LESSON_HOURS = 6


def _norm_group(group):
    return group or None


def resolve_hours(rows, day, class_id, class_group):
    """Lesson hours of a class on `day`, capped at 6."""
    class_group = _norm_group(class_group)
    group_stage = group_row = school_row = None
    for row in rows:
        if row.date != day:
            continue
        if row.stage_class_id is not None:
            if row.stage_class_id == class_id:
                return 0
            continue
        group = _norm_group(row.group)
        if group is None:
            school_row = row
        elif group == class_group:
            if row.is_group_stage:
                group_stage = row
            else:
                group_row = row
    if group_stage is not None:
        return 0
    row = group_row if group_row is not None else school_row
    if row is None:
        return MAX_LESSON_HOURS
    if row.is_closure:
        return 0
    return min(row.max_hours, MAX_LESSON_HOURS)


def resolve_start_hour(rows, day, class_group):
    """Earliest allowed start hour (8-13) for the class's group, or None."""
    class_group = _norm_group(class_group)
    group_limit = school_limit = None
    for row in rows:
        if row.date != day or row.stage_class_id is not None:
            continue
        group = _norm_group(row.group)
        if group is None:
            school_limit = row.start_hour_min
        elif group == class_group:
            group_limit = row.start_hour_min
    return group_limit if group_limit is not None else school_limit

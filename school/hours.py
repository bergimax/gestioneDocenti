"""Weekly hours target (specs section 8). Pure: no Django, no database."""


def weekly_hours_target(
    remaining,
    week_start,
    year_end,
    teacher_type,
    reduce_contract_hours=False,
    max_hours=None,
    stage_weeks=0,
):
    """Hours to schedule in the week, or None when nothing remains.

    `stage_weeks` future weeks entirely in stage for the class are not counted
    in the weeks left (specs 8).
    """
    if remaining <= 0:
        return None
    weeks_left = max(1, (year_end - week_start).days // 7 - stage_weeks)
    target = max(1, remaining // weeks_left)
    if reduce_contract_hours and teacher_type == "CONTRATTO":
        if max_hours is not None:
            target = max(1, min(target, max_hours))
        else:
            target = max(1, target // 2)
    return target

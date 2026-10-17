"""Context builder: the only bridge from Django state to the solver (specs 9.1).

Read-only. Builds a ``ScheduleContext`` for one Monday from the database; it
never writes (not even the inherited availability).
"""

import datetime

from availability import grid as grid_logic
from availability.models import Availability
from scheduling.derogations import REDUCE_CONTRACT_HOURS
from scheduling.solver.context import (
    DAYS,
    EXCLUDE_DAY,
    FIRST_HOUR,
    HOURS,
    Pairing,
    ScheduleContext,
)
from scheduling.solver.context import (
    Assignment as SolverAssignment,
)
from school import assignment_hours
from school import calendar as day_rules
from school.hours import weekly_hours_target
from school.models import (
    Assignment,
    CalendarEntry,
    Class,
    ClassPairing,
    Teacher,
    TeacherType,
)
from school.selectors import current_school

ALL_CELLS = frozenset((d, h) for d in range(DAYS) for h in range(HOURS))


def _cells_of(grid):
    """The (day, hour index) cells a valid grid marks available."""
    cells = set()
    for day_index, day in enumerate(grid_logic.DAYS):
        for slot in grid[day]:
            if not slot["disponibile"]:
                continue
            start = int(slot["ora_inizio"][:2])
            end = int(slot["ora_fine"][:2])
            if slot["ora_fine"][3:] != "00":
                end += 1
            for hour in range(max(start, FIRST_HOUR), min(end, FIRST_HOUR + HOURS)):
                cells.add((day_index, hour - FIRST_HOUR))
    return frozenset(cells)


def _availability(teachers, stored, week_start):
    """teacher id -> available cells (always a dict, {} when no teachers)."""
    result = {}
    for teacher in teachers:
        if teacher.type != TeacherType.CONTRATTO:
            result[teacher.pk] = ALL_CELLS
            continue
        resolution = grid_logic.resolve_grid(
            week_start, stored.get(teacher.pk, {}), teacher.first_time
        )
        if resolution.errors or (
            resolution.source == grid_logic.DEFAULT and not teacher.first_time
        ):
            result[teacher.pk] = frozenset()
        else:
            result[teacher.pk] = _cells_of(resolution.grid)
    return result


def _is_stage(rows, day, school_class):
    group = school_class.group or None
    return any(
        row.date == day
        and (
            row.stage_class_id == school_class.pk
            or (
                row.stage_class_id is None
                and row.is_group_stage
                and (row.group or None) == group
            )
        )
        for row in rows
    )


def _reduces(derogations, assignment):
    """The `reduce_contract_hours` derogations covering the assignment."""
    return [
        d
        for d in derogations
        if d.action_type == REDUCE_CONTRACT_HOURS
        and d.class_id in (None, assignment.school_class_id)
        and d.teacher_id in (None, assignment.teacher_id)
    ]


def _excluded_days(derogations):
    """H7 input from the `exclude_day` derogations (merged, one entry per teacher)."""
    excluded = {}
    for d in derogations:
        if (
            d.action_type == EXCLUDE_DAY
            and d.teacher_id is not None
            and d.day is not None
        ):
            excluded[d.teacher_id] = excluded.get(d.teacher_id, frozenset()) | {d.day}
    return excluded


def build_week_context(week_start, school=None, derogations=()):
    """The solver input for the week starting on Monday ``week_start``.

    `derogations` (solver `Derogation`s of one quick action) are passed to the
    solver as they are; `reduce_contract_hours` also lowers the targets here,
    where they are computed (specs 8).
    """
    grid_logic.check_monday(week_start)
    if school is None:
        school = current_school()
    days = [week_start + datetime.timedelta(days=i) for i in range(DAYS)]

    teachers = list(Teacher.objects.filter(school=school))
    classes = list(Class.objects.filter(school=school))
    rows = list(CalendarEntry.objects.filter(school=school, date__in=days))
    stored = {}
    for tid, week, slots in Availability.objects.filter(
        school=school, week_start__lte=week_start
    ).values_list("teacher_id", "week_start", "slots"):
        stored.setdefault(tid, {})[week] = slots

    class_capacity, class_min_start, stage_days = {}, {}, {}
    for school_class in classes:
        group = school_class.group or None
        class_capacity[school_class.pk] = {
            i: day_rules.resolve_hours(rows, day, school_class.pk, group)
            for i, day in enumerate(days)
        }
        starts = [
            day_rules.resolve_start_hour(rows, day, group)
            for day in days
            if class_capacity[school_class.pk][days.index(day)] > 0
        ]
        starts = [s for s in starts if s is not None]
        if starts:
            class_min_start[school_class.pk] = max(starts)
        stage = frozenset(
            i for i, day in enumerate(days) if _is_stage(rows, day, school_class)
        )
        if stage:
            stage_days[school_class.pk] = stage

    delivered = assignment_hours.delivered_hours_map(school)
    assignments, targets, sources = [], {}, {}
    weeks_left = max(1, (school.year_end - week_start).days // 7)
    for a in (
        Assignment.objects.filter(school=school, teacher__active=True)
        .select_related("subject", "teacher")
        .order_by("pk")
    ):
        remaining = a.ore_totali - delivered.get(a.pk, 0)
        assignments.append(
            SolverAssignment(
                id=a.pk,
                teacher_id=a.teacher_id,
                class_id=a.school_class_id,
                subject_id=a.subject_id,
                residual_hours=max(0, remaining),
                subject_type=a.subject.kind,
            )
        )
        if not any(class_capacity[a.school_class_id].values()):
            continue
        reducing = _reduces(derogations, a)
        target = weekly_hours_target(
            remaining,
            week_start,
            school.year_end,
            a.teacher.type,
            reduce_contract_hours=bool(reducing),
            max_hours=reducing[0].max_hours if reducing else None,
        )
        if target is not None:
            targets[a.pk] = target
            sources[a.pk] = f"residuo {remaining} ore su {weeks_left} settimane"
            if reducing and a.teacher.type == TeacherType.CONTRATTO:
                sources[a.pk] += ", ridotto per deroga"

    pairings = [
        Pairing(p.class_a_id, p.class_b_id, p.subject_id, p.teacher_id)
        for p in ClassPairing.objects.filter(school=school).order_by("pk")
    ]
    return ScheduleContext(
        assignments=assignments,
        excluded_days=_excluded_days(derogations),
        teacher_types={t.pk: t.type for t in teachers},
        class_groups={c.pk: (c.group or None) for c in classes},
        class_capacity=class_capacity,
        class_min_start=class_min_start,
        stage_days=stage_days,
        pairings=pairings,
        availability=_availability(teachers, stored, week_start),
        targets=targets,
        target_sources=sources,
        derogations=list(derogations),
        class_names={c.pk: c.name for c in classes},
        teacher_names={t.pk: t.name for t in teachers},
    )

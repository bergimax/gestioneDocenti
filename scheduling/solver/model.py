"""CP-SAT model with the always-respected hard constraints (specs section 9.2)."""

from __future__ import annotations

from collections import defaultdict

from ortools.sat.python import cp_model

from .context import (
    ASSUNTO,
    DAYS,
    FIRST_HOUR,
    HOURS,
    Lesson,
    ScheduleContext,
    SolveResult,
    SolveStatus,
)


def _sum(terms):
    return cp_model.LinearExpr.sum(list(terms))


_STATUS = {
    cp_model.OPTIMAL: SolveStatus.OPTIMAL,
    cp_model.FEASIBLE: SolveStatus.FEASIBLE,
    cp_model.INFEASIBLE: SolveStatus.INFEASIBLE,
}


def _capacity(ctx: ScheduleContext, class_id, day: int) -> int:
    if day in ctx.stage_days.get(class_id, ()):
        return 0  # H5
    cap = ctx.class_capacity.get(class_id, {}).get(day, HOURS)
    return max(0, min(HOURS, cap))


def solve(ctx: ScheduleContext) -> SolveResult:
    classes = (
        {a.class_id for a in ctx.assignments}
        | set(ctx.class_capacity)
        | set(ctx.class_groups)
        | set(ctx.class_min_start)
        | set(ctx.stage_days)
    )
    for p in ctx.pairings:
        classes |= {p.class_a, p.class_b}
    if not classes:
        return SolveResult(SolveStatus.OPTIMAL, [])

    by_key = {(a.teacher_id, a.class_id, a.subject_id): a for a in ctx.assignments}
    by_class = defaultdict(list)
    by_teacher = defaultdict(list)
    for a in ctx.assignments:
        by_class[a.class_id].append(a)
        by_teacher[a.teacher_id].append(a)

    m = cp_model.CpModel()
    x = {
        (a.id, d, h): m.new_bool_var(f"x_{a.id}_{d}_{h}")
        for a in ctx.assignments
        for d in range(DAYS)
        for h in range(HOURS)
    }

    # H3: the paired lesson is one lesson shared by two assignments.
    shadow = set()  # the second class's assignment: not counted for the teacher
    for p in ctx.pairings:
        a = by_key.get((p.teacher_id, p.class_a, p.subject_id))
        b = by_key.get((p.teacher_id, p.class_b, p.subject_id))
        if a is None or b is None:
            raise ValueError(f"pairing without matching assignments: {p}")
        shadow.add(b.id)
        for d in range(DAYS):
            for h in range(HOURS):
                m.add(x[a.id, d, h] == x[b.id, d, h])

    for class_id in classes:
        group = by_class[class_id]
        min_hour = max(
            0,
            min(HOURS - 1, ctx.class_min_start.get(class_id, FIRST_HOUR) - FIRST_HOUR),
        )
        for d in range(DAYS):
            cap = _capacity(ctx, class_id, d)
            occupied = [_sum(x[a.id, d, h] for a in group) for h in range(HOURS)]
            for h in range(HOURS):
                m.add(occupied[h] <= 1)  # H1b
            # H2 + H2b + H2c: one contiguous block of exactly `cap` lessons,
            # starting no earlier than the class's minimum start hour.
            if cap == 0:  # H5 / closure
                for h in range(HOURS):
                    m.add(occupied[h] == 0)
                continue
            starts = list(range(min_hour, HOURS - cap + 1))
            if not starts:
                m.add_bool_or([])  # unsatisfiable: the block cannot fit
                continue
            pick = {s: m.new_bool_var(f"blk_{class_id}_{d}_{s}") for s in starts}
            m.add_exactly_one(pick.values())
            for h in range(HOURS):
                m.add(
                    occupied[h] == _sum(v for s, v in pick.items() if s <= h < s + cap)
                )

    # H1: a teacher is in one place per hour (a pair counts once).
    for group in by_teacher.values():
        counted = [a for a in group if a.id not in shadow]
        for d in range(DAYS):
            for h in range(HOURS):
                m.add(_sum(x[a.id, d, h] for a in counted) <= 1)

    # H6: ASSUNTO hours per assignment never exceed the residual.
    for a in ctx.assignments:
        if ctx.teacher_types.get(a.teacher_id, ASSUNTO) == ASSUNTO:
            m.add(
                _sum(x[a.id, d, h] for d in range(DAYS) for h in range(HOURS))
                <= max(0, a.residual_hours)
            )

    # H7: excluded teacher-days.
    for teacher_id, days in ctx.excluded_days.items():
        for a in by_teacher.get(teacher_id, []):
            for d in days:
                if 0 <= d < DAYS:
                    for h in range(HOURS):
                        m.add(x[a.id, d, h] == 0)

    # H8: weekly cap per teacher.
    for teacher_id, cap in ctx.weekly_caps.items():
        counted = [a for a in by_teacher.get(teacher_id, []) if a.id not in shadow]
        m.add(
            _sum(
                x[a.id, d, h]
                for a in counted
                for d in range(DAYS)
                for h in range(HOURS)
            )
            <= max(0, cap)
        )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = ctx.timeout_seconds
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = 0
    status = _STATUS.get(solver.solve(m), SolveStatus.UNKNOWN)
    if status not in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE):
        return SolveResult(status, [])
    lessons = [
        Lesson(a.id, d, h)
        for a in ctx.assignments
        for d in range(DAYS)
        for h in range(HOURS)
        if solver.value(x[a.id, d, h])
    ]
    lessons.sort(key=lambda le: (le.day, le.hour, str(le.assignment_id)))
    return SolveResult(status, lessons)

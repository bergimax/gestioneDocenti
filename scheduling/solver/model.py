"""CP-SAT model with the always-respected hard constraints (specs section 9.2)."""

from __future__ import annotations

from collections import defaultdict

from ortools.sat.python import cp_model

from .context import (
    ASSUNTO,
    AUTHORIZE_FRIDAY_LATE_START,
    AUTHORIZE_SHORT_PRATICA_BLOCK,
    AUTHORIZE_SINGLE_CLASSE_DAY,
    CONTRATTO,
    DAYS,
    FIRST_HOUR,
    HOURS,
    MAX_HOURS_PER_CLASSE_DAY,
    OVERRIDE_AVAILABILITY,
    PRATICA,
    RELAX_WEIGHT,
    Conflict,
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


_DAY_NAMES = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì"]
FRIDAY = 4


def _derogated(ctx: ScheduleContext, action, class_id, teachers, day) -> bool:
    """True if a derogation of this type covers the (class, teachers, day)."""
    return any(
        d.action_type == action
        and (d.class_id is None or d.class_id == class_id)
        and (d.day is None or d.day == day)
        and (d.teacher_id is None or d.teacher_id in teachers)
        for d in ctx.derogations
    )


def _hour_label(h: int) -> str:
    return f"{FIRST_HOUR + h:02d}:00"


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

    penalties = []  # (var, record) - one override variable per violation
    pick_by_class_day = {}

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
            pick_by_class_day[class_id, d] = pick
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

    _add_quasi_hard(
        ctx, m, x, classes, by_class, by_teacher, shadow, pick_by_class_day, penalties
    )
    if penalties:
        m.minimize(_sum(RELAX_WEIGHT * var for var, _ in penalties))

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
    conflicts = [
        _conflict(rec, lessons, ctx) for var, rec in penalties if solver.value(var)
    ]
    penalty = round(solver.objective_value) if penalties else 0
    return SolveResult(status, lessons, conflicts, penalty)


def _add_quasi_hard(
    ctx, m, x, classes, by_class, by_teacher, shadow, pick_by_class_day, penalties
):
    """H4, H9, H10, H11: each violation is an override var weighted RELAX_WEIGHT."""
    assignment = {a.id: a for a in ctx.assignments}
    partner = {}  # paired assignments share their H4 derogation
    for p in ctx.pairings:
        a = next(
            (
                a
                for a in ctx.assignments
                if (a.teacher_id, a.class_id, a.subject_id)
                == (p.teacher_id, p.class_a, p.subject_id)
            ),
            None,
        )
        b = next(
            (
                b
                for b in ctx.assignments
                if (b.teacher_id, b.class_id, b.subject_id)
                == (p.teacher_id, p.class_b, p.subject_id)
            ),
            None,
        )
        partner[a.id], partner[b.id] = b.id, a.id

    # H4: contractor availability. Rigid unless availability_relaxed.
    for a in ctx.assignments if ctx.availability is not None else []:
        if ctx.teacher_types.get(a.teacher_id, ASSUNTO) != CONTRATTO:
            continue
        free = ctx.availability.get(a.teacher_id, frozenset())
        ids = [a.id] + ([partner[a.id]] if a.id in partner else [])
        for d in range(DAYS):
            if any(
                _derogated(
                    ctx,
                    OVERRIDE_AVAILABILITY,
                    assignment[i].class_id,
                    {a.teacher_id},
                    d,
                )
                for i in ids
            ):
                continue
            for h in range(HOURS):
                if (d, h) in free:
                    continue
                if not ctx.availability_relaxed:
                    m.add(x[a.id, d, h] == 0)
                elif a.id not in shadow:
                    penalties.append(
                        (x[a.id, d, h], ("H4", a.class_id, a.teacher_id, d, h))
                    )

    # H9: on Friday the lesson block of a class starts at 08:00.
    for class_id in classes:
        if ctx.class_min_start.get(class_id, FIRST_HOUR) > FIRST_HOUR:
            continue
        pick = pick_by_class_day.get((class_id, FRIDAY))
        if not pick or 0 not in pick or len(pick) == 1:
            continue
        teachers = {a.teacher_id for a in by_class[class_id]}
        if _derogated(ctx, AUTHORIZE_FRIDAY_LATE_START, class_id, teachers, FRIDAY):
            continue
        v = m.new_bool_var(f"h9_{class_id}")
        m.add(v + pick[0] == 1)
        penalties.append((v, ("H9", class_id, None, FRIDAY, None)))

    # H10: a PRATICA subject forms one block of >= min(3, capacity) hours, or none.
    for class_id in classes:
        subjects = defaultdict(list)
        for a in by_class[class_id]:
            if a.subject_type == PRATICA:
                subjects[a.subject_id].append(a)
        for subject_id, group in subjects.items():
            teachers = {a.teacher_id for a in group}
            for d in range(DAYS):
                cap = _capacity(ctx, class_id, d)
                if cap == 0 or _derogated(
                    ctx, AUTHORIZE_SHORT_PRATICA_BLOCK, class_id, teachers, d
                ):
                    continue
                minblock = min(3, cap)
                p = [_sum(x[a.id, d, h] for a in group) for h in range(HOURS)]
                starts = []
                for h in range(HOURS):
                    s = m.new_bool_var(f"h10s_{class_id}_{subject_id}_{d}_{h}")
                    prev = p[h - 1] if h else 0
                    m.add(s >= p[h] - prev)
                    m.add(s <= p[h])
                    m.add(s <= 1 - prev)
                    starts.append(s)
                total = _sum(p)
                used = m.new_bool_var(f"h10u_{class_id}_{subject_id}_{d}")
                for h in range(HOURS):
                    m.add(used >= p[h])
                m.add(used <= total)
                v = m.new_bool_var(f"h10_{class_id}_{subject_id}_{d}")
                m.add(_sum(starts) <= 1).only_enforce_if(v.negated())
                m.add(total >= minblock).only_enforce_if([v.negated(), used])
                penalties.append(
                    (
                        v,
                        (
                            "H10",
                            class_id,
                            min(teachers, key=str),
                            d,
                            None,
                            subject_id,
                            minblock,
                        ),
                    )
                )

    # H11: at most MAX_HOURS_PER_CLASSE_DAY hours with the same class per day,
    # for teachers with two or more classes (a paired lesson is one hour per class).
    for teacher_id, group in by_teacher.items():
        if len({a.class_id for a in group}) < 2:
            continue
        for class_id in {a.class_id for a in group}:
            mine = [a for a in group if a.class_id == class_id]
            for d in range(DAYS):
                if _capacity(ctx, class_id, d) <= MAX_HOURS_PER_CLASSE_DAY:
                    continue
                if _derogated(
                    ctx, AUTHORIZE_SINGLE_CLASSE_DAY, class_id, {teacher_id}, d
                ):
                    continue
                v = m.new_bool_var(f"h11_{teacher_id}_{class_id}_{d}")
                m.add(
                    _sum(x[a.id, d, h] for a in mine for h in range(HOURS))
                    <= MAX_HOURS_PER_CLASSE_DAY
                ).only_enforce_if(v.negated())
                penalties.append((v, ("H11", class_id, teacher_id, d, None)))


def _conflict(rec, lessons, ctx) -> Conflict:
    kind, class_id, teacher_id, day, hour = rec[:5]
    where = f"{_DAY_NAMES[day]}"
    if kind == "H4":
        desc = (
            f"Il docente {teacher_id} ha una lezione con la classe {class_id} "
            f"{where} alle {_hour_label(hour)} ma non è disponibile"
        )
        action = OVERRIDE_AVAILABILITY
    elif kind == "H9":
        desc = (
            f"La classe {class_id} non inizia alle 08:00 il venerdì "
            f"(il blocco di lezioni dovrebbe iniziare alle 8:00)"
        )
        action = AUTHORIZE_FRIDAY_LATE_START
    elif kind == "H10":
        subject_id, minblock = rec[5], rec[6]
        desc = (
            f"Le ore di pratica di {subject_id} per la classe {class_id} {where} "
            f"non formano un unico blocco di almeno {minblock} ore"
        )
        action = AUTHORIZE_SHORT_PRATICA_BLOCK
    else:
        assignments = {a.id: a for a in ctx.assignments}
        n = sum(
            1
            for le in lessons
            if le.day == day
            and assignments[le.assignment_id].teacher_id == teacher_id
            and assignments[le.assignment_id].class_id == class_id
        )
        desc = (
            f"Il docente {teacher_id} ha {n} ore con la classe {class_id} {where}, "
            f"oltre il massimo di {MAX_HOURS_PER_CLASSE_DAY}"
        )
        action = AUTHORIZE_SINGLE_CLASSE_DAY
    return Conflict(kind, class_id, teacher_id, day, hour, desc, action)

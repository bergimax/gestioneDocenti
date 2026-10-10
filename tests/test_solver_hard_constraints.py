import itertools
from collections import defaultdict

from scheduling.solver import (
    ASSUNTO,
    CONTRATTO,
    Assignment,
    Pairing,
    ScheduleContext,
    SolveStatus,
    solve,
)


def day0_only(classes, cap, **extra):
    """Capacity `cap` on Monday, 0 on every other day, for each class."""
    return {c: {0: cap, 1: 0, 2: 0, 3: 0, 4: 0} for c in classes} | extra


def A(id, teacher, klass, residual, subject="S"):
    return Assignment(id, teacher, klass, subject, residual)


def ctx(assignments, capacity, teacher_types=None, **kw):
    types = {a.teacher_id: CONTRATTO for a in assignments}
    types.update(teacher_types or {})
    return ScheduleContext(
        assignments=assignments,
        teacher_types=types,
        class_capacity=capacity,
        timeout_seconds=20,
        **kw,
    )


def by_assignment(result):
    out = defaultdict(list)
    for le in result.lessons:
        out[le.assignment_id].append((le.day, le.hour))
    return out


def test_h1_teacher_not_in_two_classes_same_hour():
    a, b = A(1, "T", "A", 5), A(2, "T", "B", 5)
    # Only hour 13:00 is available to both classes: one teacher cannot be in both.
    tight = ctx([a, b], day0_only("AB", 1), class_min_start={"A": 13, "B": 13})
    assert solve(tight).status == SolveStatus.INFEASIBLE
    # With two possible hours it works and the hours differ.
    ok = ctx([a, b], day0_only("AB", 1), class_min_start={"A": 12, "B": 12})
    res = solve(ok)
    assert res.status == SolveStatus.OPTIMAL
    hours = [le.hour for le in res.lessons]
    assert len(hours) == 2 and len(set(hours)) == 2


def test_h1b_class_has_one_lesson_per_hour():
    assignments = [A(1, "T1", "A", 6), A(2, "T2", "A", 6)]
    res = solve(ctx(assignments, day0_only("A", 4)))
    assert res.status == SolveStatus.OPTIMAL
    slots = [(le.day, le.hour) for le in res.lessons]
    assert len(slots) == 4 and len(set(slots)) == 4


def test_h2_exact_capacity():
    full = {"A": {d: 4 for d in range(5)}}
    res = solve(ctx([A(1, "T", "A", 99)], full))
    assert res.status == SolveStatus.OPTIMAL
    per_day = defaultdict(int)
    for le in res.lessons:
        per_day[le.day] += 1
    assert dict(per_day) == {d: 4 for d in range(5)}


def test_h2_infeasible_when_assignments_cannot_supply_capacity():
    one = ctx([A(1, "T", "A", 3)], day0_only("A", 4), {"T": ASSUNTO})
    res = solve(one)
    assert res.status == SolveStatus.INFEASIBLE
    assert res.lessons == []


def test_h2b_block_is_contiguous():
    res = solve(ctx([A(1, "T", "A", 99)], day0_only("A", 4)))
    hours = sorted(le.hour for le in res.lessons)
    assert hours[-1] - hours[0] == 3 and len(hours) == 4


def test_h2b_gap_is_never_accepted():
    # T1 is free only at 08:00 (class B occupies 09-13), T2 is free at 13:00
    # or 08:00 only; class A (2 lessons, one per teacher) could only be
    # satisfied with a gap between its lessons.
    assignments = [
        A(1, "T1", "A", 1),
        A(2, "T2", "A", 1),
        A(3, "T1", "B", 9),
        A(4, "T2", "C", 9),
    ]
    capacity = day0_only("A", 2) | day0_only("B", 5) | day0_only("C", 5)
    res = solve(ctx(assignments, capacity, class_min_start={"B": 9}))
    assert res.status == SolveStatus.INFEASIBLE


def test_h2c_no_lessons_before_minimum_start():
    res = solve(ctx([A(1, "T", "A", 9)], day0_only("A", 3), class_min_start={"A": 10}))
    assert res.status == SolveStatus.OPTIMAL
    assert min(le.hour for le in res.lessons) >= 2  # 10:00


def test_h2c_block_that_cannot_fit_after_minimum_start_is_infeasible():
    res = solve(ctx([A(1, "T", "A", 9)], day0_only("A", 4), class_min_start={"A": 11}))
    assert res.status == SolveStatus.INFEASIBLE


def test_h3_paired_lesson_same_slot_and_residual_once():
    assignments = [A(1, "T", "A", 1), A(2, "T", "B", 1)]
    pair = Pairing("A", "B", "S", "T")
    # Both classes only have 13:00: only possible if the pair is one lesson.
    res = solve(
        ctx(
            assignments,
            day0_only("AB", 1),
            {"T": ASSUNTO},
            pairings=[pair],
            class_min_start={"A": 13, "B": 13},
            weekly_caps={"T": 1},
        )
    )
    assert res.status == SolveStatus.OPTIMAL
    lessons = by_assignment(res)
    assert lessons[1] == lessons[2] == [(0, 5)]


def test_h5_stage_and_closed_days_have_no_lessons():
    capacity = {"A": {0: 3, 1: 3, 2: 0, 3: 3, 4: 3}}
    res = solve(
        ctx(
            [A(1, "T", "A", 99)],
            capacity,
            stage_days={"A": frozenset({1})},
        )
    )
    assert res.status == SolveStatus.OPTIMAL
    days = {le.day for le in res.lessons}
    assert days == {0, 3, 4}


def test_h5_whole_stage_week_is_empty_and_feasible():
    stage = frozenset(range(5))
    res = solve(
        ctx(
            [A(1, "T", "A", 5), A(2, "U", "B", 5)],
            {},
            stage_days={"A": stage, "B": stage},
        )
    )
    assert res.status == SolveStatus.OPTIMAL
    assert res.lessons == []


def test_h6_assunto_capped_by_residual_contratto_is_not():
    capacity = day0_only("A", 4)
    assunto = solve(ctx([A(1, "T", "A", 3)], capacity, {"T": ASSUNTO}))
    assert assunto.status == SolveStatus.INFEASIBLE
    contratto = solve(ctx([A(1, "T", "A", 3)], capacity, {"T": CONTRATTO}))
    assert contratto.status == SolveStatus.OPTIMAL
    assert len(contratto.lessons) == 4


def test_h6_assunto_gets_at_most_residual_when_another_teacher_fills_up():
    assignments = [A(1, "T1", "A", 2), A(2, "T2", "A", 9)]
    res = solve(ctx(assignments, day0_only("A", 4), {"T1": ASSUNTO}))
    assert res.status == SolveStatus.OPTIMAL
    assert len(by_assignment(res)[1]) <= 2


def test_h7_excluded_teacher_day_has_no_lessons():
    only_t = ctx(
        [A(1, "T", "A", 9)],
        day0_only("A", 1),
        excluded_days={"T": frozenset({0})},
    )
    assert solve(only_t).status == SolveStatus.INFEASIBLE
    two = ctx(
        [A(1, "T1", "A", 9), A(2, "T2", "A", 9)],
        day0_only("A", 3),
        excluded_days={"T1": frozenset({0})},
    )
    res = solve(two)
    assert res.status == SolveStatus.OPTIMAL
    assert 1 not in by_assignment(res) and len(by_assignment(res)[2]) == 3


def test_h8_weekly_cap_is_respected():
    assignments = [A(1, "T", "A", 9), A(2, "T", "B", 9)]
    capacity = day0_only("AB", 1) | {"A": {0: 2, 1: 0, 2: 0, 3: 0, 4: 0}}
    over = ctx(assignments, capacity, weekly_caps={"T": 2})
    assert solve(over).status == SolveStatus.INFEASIBLE  # needs 3 hours
    fits = ctx(assignments, capacity, weekly_caps={"T": 3})
    res = solve(fits)
    assert res.status == SolveStatus.OPTIMAL and len(res.lessons) == 3


def test_empty_assignment_list_is_empty_and_feasible():
    res = solve(ScheduleContext())
    assert res.status == SolveStatus.OPTIMAL and res.lessons == []


def test_zero_residual_assignment_gives_no_lessons():
    assignments = [A(1, "T1", "A", 0), A(2, "T2", "A", 9)]
    res = solve(ctx(assignments, day0_only("A", 2), {"T1": ASSUNTO}))
    assert res.status == SolveStatus.OPTIMAL
    assert 1 not in by_assignment(res)
    only_zero = solve(ctx([A(1, "T1", "A", 0)], {"A": {d: 0 for d in range(5)}}))
    assert only_zero.status == SolveStatus.OPTIMAL and only_zero.lessons == []


def test_capacity_above_six_is_clamped():
    res = solve(ctx([A(1, "T", "A", 99)], day0_only("A", 9)))
    assert res.status == SolveStatus.OPTIMAL
    assert len(res.lessons) == 6


def test_class_without_assignments_but_with_capacity_is_infeasible():
    res = solve(ctx([A(1, "T", "A", 9)], day0_only("A", 1) | day0_only("B", 2)))
    assert res.status == SolveStatus.INFEASIBLE


def big_context(timeout):
    classes = [f"C{i}" for i in range(24)]
    teachers = [f"T{i}" for i in range(12)]
    assignments = [
        A(i, t, c, 40, subject=str(j))
        for i, (c, (j, t)) in enumerate(
            itertools.product(classes, list(enumerate(teachers))[:6])
        )
    ]
    return ScheduleContext(
        assignments=assignments,
        teacher_types={t: CONTRATTO for t in teachers},
        timeout_seconds=timeout,
    )


def test_timeout_never_raises():
    res = solve(big_context(0.0001))
    assert res.status in (SolveStatus.UNKNOWN, SolveStatus.FEASIBLE)


def test_same_context_gives_identical_lessons():
    assignments = [A(i, f"T{i % 5}", f"C{i % 4}", 30) for i in range(20)]
    c = ctx(assignments, {})
    first, second = solve(c), solve(c)
    assert first.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    assert first.lessons == second.lessons

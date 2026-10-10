"""Quasi-hard constraints H4, H9, H10, H11 of the solver (specs 9.3)."""

import pytest

from scheduling.solver import (
    ASSUNTO,
    AUTHORIZE_FRIDAY_LATE_START,
    AUTHORIZE_SHORT_PRATICA_BLOCK,
    AUTHORIZE_SINGLE_CLASSE_DAY,
    CONTRATTO,
    OVERRIDE_AVAILABILITY,
    PRATICA,
    RELAX_WEIGHT,
    Assignment,
    Derogation,
    Pairing,
    ScheduleContext,
    SolveStatus,
    solve,
)

MON, THU, FRI = 0, 3, 4


def day_cap(day, cap):
    return {d: (cap if d == day else 0) for d in range(5)}


def A(id, teacher, klass, residual=99, subject="S", subject_type=""):
    return Assignment(id, teacher, klass, subject, residual, subject_type)


def cells(day, hours):
    return frozenset((day, h) for h in hours)


def make(assignments, capacity, types=None, **kw):
    teacher_types = {a.teacher_id: CONTRATTO for a in assignments}
    teacher_types.update(types or {})
    return ScheduleContext(
        assignments=assignments,
        teacher_types=teacher_types,
        class_capacity=capacity,
        timeout_seconds=20,
        **kw,
    )


def kinds(res, kind):
    return [c for c in res.conflicts if c.kind == kind]


def assert_consistent(res):
    """No silent violation: the objective equals 1000 per reported conflict."""
    assert res.override_penalty == RELAX_WEIGHT * len(res.conflicts)
    assert all(c.penalty == RELAX_WEIGHT for c in res.conflicts)


# ---------------------------------------------------------------- H4


def test_h4_holds_when_possible():
    ctx = make(
        [A(1, "T", "A")], {"A": day_cap(MON, 2)}, availability={"T": cells(MON, [4, 5])}
    )
    res = solve(ctx)
    assert res.status == SolveStatus.OPTIMAL
    assert sorted((le.day, le.hour) for le in res.lessons) == [(MON, 4), (MON, 5)]
    assert kinds(res, "H4") == []


def test_h4_rigid_when_only_free_cell_is_unavailable():
    # Full 6-hour day, contractor free everywhere but 11:00: INFEASIBLE, not a
    # violation, even though the model has an override available.
    free = cells(MON, [0, 1, 2, 4, 5])
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 6)}, availability={"T": free})
    res = solve(ctx)
    assert res.status == SolveStatus.INFEASIBLE
    assert res.conflicts == []


def test_h4_reported_when_forced_with_relaxed_flag():
    free = cells(MON, [0, 1, 2, 4, 5])
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 6)},
        availability={"T": free},
        availability_relaxed=True,
    )
    res = solve(ctx)
    assert res.status == SolveStatus.OPTIMAL
    assert len(res.lessons) == 6
    (c,) = res.conflicts
    assert (c.kind, c.class_id, c.teacher_id, c.day, c.hour) == ("H4", "A", "T", MON, 3)
    assert c.penalty == RELAX_WEIGHT == res.override_penalty
    assert c.action_type == OVERRIDE_AVAILABILITY


def test_h4_relaxed_violates_the_minimum_number_of_cells():
    # Two lessons, the contractor is free at 08:00 only: one block of two must
    # cover one unavailable cell, never two.
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 2)},
        availability={"T": cells(MON, [0])},
        availability_relaxed=True,
    )
    res = solve(ctx)
    assert len(kinds(res, "H4")) == 1
    assert res.override_penalty == RELAX_WEIGHT
    assert sorted(le.hour for le in res.lessons) == [0, 1]


def test_h4_relaxed_flag_without_need_reports_nothing():
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 2)},
        availability={"T": cells(MON, range(6))},
        availability_relaxed=True,
    )
    res = solve(ctx)
    assert res.conflicts == [] and res.override_penalty == 0


def test_h4_assunto_ignores_an_unavailable_grid():
    ctx = make(
        [A(1, "T", "A", residual=6)],
        {"A": day_cap(MON, 6)},
        {"T": ASSUNTO},
        availability={"T": frozenset()},
    )
    res = solve(ctx)
    assert res.status == SolveStatus.OPTIMAL
    assert len(res.lessons) == 6 and res.conflicts == []


def test_h4_contractor_without_grid_is_unavailable_all_week():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 2)}, availability={})
    assert solve(ctx).status == SolveStatus.INFEASIBLE


def test_h4_override_availability_frees_a_contractor_without_grid():
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 2)},
        availability={},
        availability_relaxed=True,
        derogations=[Derogation(OVERRIDE_AVAILABILITY, teacher_id="T")],
    )
    res = solve(ctx)
    assert res.status == SolveStatus.OPTIMAL
    assert len(res.lessons) == 2
    assert res.conflicts == [] and res.override_penalty == 0


def test_h4_override_availability_works_in_rigid_mode_too():
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 2)},
        availability={},
        derogations=[Derogation(OVERRIDE_AVAILABILITY, teacher_id="T")],
    )
    assert solve(ctx).status == SolveStatus.OPTIMAL


@pytest.mark.parametrize(
    "other",
    [
        Derogation(OVERRIDE_AVAILABILITY, teacher_id="X"),
        Derogation(OVERRIDE_AVAILABILITY, class_id="B"),
        Derogation(OVERRIDE_AVAILABILITY, day=THU),
        Derogation(AUTHORIZE_FRIDAY_LATE_START, teacher_id="T"),
    ],
)
def test_h4_derogation_for_another_scope_still_reports(other):
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 2)},
        availability={},
        availability_relaxed=True,
        derogations=[other],
    )
    res = solve(ctx)
    assert len(kinds(res, "H4")) == 2
    assert res.override_penalty == 2 * RELAX_WEIGHT


def test_h4_derogation_scoped_to_class_and_day_applies():
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 2)},
        availability={},
        availability_relaxed=True,
        derogations=[Derogation(OVERRIDE_AVAILABILITY, class_id="A", day=MON)],
    )
    assert solve(ctx).conflicts == []


def test_h4_paired_lesson_is_reported_once():
    ctx = make(
        [A(1, "T", "A"), A(2, "T", "B")],
        {"A": day_cap(MON, 1), "B": day_cap(MON, 1)},
        pairings=[Pairing("A", "B", "S", "T")],
        availability={"T": frozenset()},
        availability_relaxed=True,
    )
    res = solve(ctx)
    assert len(kinds(res, "H4")) == 1
    assert res.override_penalty == RELAX_WEIGHT


# ---------------------------------------------------------------- H9


def friday_ctx(**kw):
    # Contractor free from 09:00: a Friday block of 3 cannot start at 08:00.
    return make(
        [A(1, "T", "A")],
        {"A": day_cap(FRI, 3)},
        availability={"T": kw.pop("free", cells(FRI, [1, 2, 3, 4, 5]))},
        **kw,
    )


def test_h9_holds_when_possible():
    ctx = friday_ctx(free=cells(FRI, range(6)))
    res = solve(ctx)
    assert res.status == SolveStatus.OPTIMAL
    assert kinds(res, "H9") == []
    assert sorted(le.hour for le in res.lessons) == [0, 1, 2]


def test_h9_reported_when_forced():
    res = solve(friday_ctx())
    assert res.status == SolveStatus.OPTIMAL
    (c,) = res.conflicts
    assert (c.kind, c.class_id, c.day) == ("H9", "A", FRI)
    assert c.action_type == AUTHORIZE_FRIDAY_LATE_START
    assert c.description
    assert min(le.hour for le in res.lessons) >= 1
    assert res.override_penalty == RELAX_WEIGHT


def test_h9_not_flagged_with_minimum_start_hour():
    res = solve(friday_ctx(class_min_start={"A": 9}))
    assert res.status == SolveStatus.OPTIMAL
    assert res.conflicts == [] and res.override_penalty == 0


def test_h9_not_flagged_when_class_has_no_lessons_on_friday():
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 2)},
        availability={"T": cells(MON, range(6))},
    )
    res = solve(ctx)
    assert res.status == SolveStatus.OPTIMAL
    assert res.conflicts == []


def test_h9_only_applies_on_friday():
    # Same forced late start on Thursday is not an H9 matter.
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(THU, 3)},
        availability={"T": cells(THU, [1, 2, 3, 4, 5])},
    )
    res = solve(ctx)
    assert res.status == SolveStatus.OPTIMAL and res.conflicts == []


def test_h9_derogation_removes_conflict_and_penalty():
    for d in (
        Derogation(AUTHORIZE_FRIDAY_LATE_START),
        Derogation(AUTHORIZE_FRIDAY_LATE_START, class_id="A", day=FRI),
        Derogation(AUTHORIZE_FRIDAY_LATE_START, teacher_id="T"),
    ):
        res = solve(friday_ctx(derogations=[d]))
        assert res.status == SolveStatus.OPTIMAL
        assert res.conflicts == [] and res.override_penalty == 0


@pytest.mark.parametrize(
    "other",
    [
        Derogation(AUTHORIZE_FRIDAY_LATE_START, class_id="B"),
        Derogation(AUTHORIZE_FRIDAY_LATE_START, teacher_id="X"),
        Derogation(AUTHORIZE_FRIDAY_LATE_START, day=THU),
        Derogation(AUTHORIZE_SHORT_PRATICA_BLOCK),
    ],
)
def test_h9_derogation_for_another_scope_still_reports(other):
    res = solve(friday_ctx(derogations=[other]))
    assert len(kinds(res, "H9")) == 1
    assert res.override_penalty == RELAX_WEIGHT


# ---------------------------------------------------------------- H10


def solve_pratica(p_hours, t_hours, t_free, capacity=6, **kw):
    """Class A: PRATICA teacher P and theory teacher TT, a contractor free only
    at `t_free`, which pins where the practice hours fall."""
    ctx = make(
        [
            A(1, "P", "A", p_hours, "LAB", PRATICA),
            A(2, "TT", "A", t_hours, "TEO"),
        ],
        {"A": day_cap(MON, capacity)},
        {"P": ASSUNTO, "TT": CONTRATTO},
        availability={"TT": cells(MON, t_free)},
        **kw,
    )
    return solve(ctx)


def test_h10_holds_when_possible():
    # Theory 08-09, practice 10-13 in one block of four.
    res = solve_pratica(4, 2, [0, 1])
    assert res.status == SolveStatus.OPTIMAL
    assert kinds(res, "H10") == []
    assert res.override_penalty == 0


def test_h10_short_block_reported_when_forced():
    # 2 practice hours on a 6-hour day: the minimum block is 3.
    res = solve_pratica(2, 4, [0, 1, 2, 3])
    assert res.status == SolveStatus.OPTIMAL
    (c,) = res.conflicts
    assert (c.kind, c.class_id, c.teacher_id, c.day) == ("H10", "A", "P", MON)
    assert c.action_type == AUTHORIZE_SHORT_PRATICA_BLOCK
    assert c.description
    assert res.override_penalty == RELAX_WEIGHT


def test_h10_split_blocks_are_reported():
    # Theory is pinned at 10-11, so practice splits 2 + 2.
    res = solve_pratica(4, 2, [2, 3])
    assert res.status == SolveStatus.OPTIMAL
    hours = sorted(le.hour for le in res.lessons if le.assignment_id == 1)
    assert hours == [0, 1, 4, 5]
    (c,) = res.conflicts
    assert c.kind == "H10" and c.class_id == "A" and c.day == MON


def test_h10_minimum_block_is_the_capacity_when_smaller_than_three():
    ctx = make(
        [A(1, "P", "A", 2, "LAB", PRATICA)],
        {"A": day_cap(MON, 2)},
        {"P": ASSUNTO},
    )
    res = solve(ctx)
    assert res.status == SolveStatus.OPTIMAL
    assert res.conflicts == []


def test_h10_day_without_the_subject_is_never_flagged():
    # Practice has a residual of 0: no lessons that week, nothing to flag.
    ctx = make(
        [A(1, "P", "A", 0, "LAB", PRATICA), A(2, "TT", "A", 6, "TEO")],
        {"A": day_cap(MON, 6)},
        {"P": ASSUNTO, "TT": ASSUNTO},
    )
    res = solve(ctx)
    assert res.status == SolveStatus.OPTIMAL
    assert res.conflicts == []


def test_h10_only_applies_to_pratica_subjects():
    res = solve(
        make(
            [A(1, "P", "A", 2, "TEO"), A(2, "TT", "A", 4, "TEO2")],
            {"A": day_cap(MON, 6)},
            {"P": ASSUNTO, "TT": ASSUNTO},
        )
    )
    assert res.status == SolveStatus.OPTIMAL and res.conflicts == []


def test_h10_derogation_removes_conflict_and_penalty():
    for d in (
        Derogation(AUTHORIZE_SHORT_PRATICA_BLOCK),
        Derogation(AUTHORIZE_SHORT_PRATICA_BLOCK, class_id="A", day=MON),
        Derogation(AUTHORIZE_SHORT_PRATICA_BLOCK, teacher_id="P"),
    ):
        res = solve_pratica(2, 4, [0, 1, 2, 3], derogations=[d])
        assert res.status == SolveStatus.OPTIMAL
        assert res.conflicts == [] and res.override_penalty == 0


@pytest.mark.parametrize(
    "other",
    [
        Derogation(AUTHORIZE_SHORT_PRATICA_BLOCK, class_id="B"),
        Derogation(AUTHORIZE_SHORT_PRATICA_BLOCK, teacher_id="TT"),
        Derogation(AUTHORIZE_SHORT_PRATICA_BLOCK, day=THU),
        Derogation(AUTHORIZE_SINGLE_CLASSE_DAY),
    ],
)
def test_h10_derogation_for_another_scope_still_reports(other):
    res = solve_pratica(2, 4, [0, 1, 2, 3], derogations=[other])
    assert len(kinds(res, "H10")) == 1
    assert res.override_penalty == RELAX_WEIGHT


# ---------------------------------------------------------------- H11


def two_class_ctx(hours_a=5, **kw):
    # T is the only teacher of class A (so 5 hours with A) and also teaches B.
    return make(
        [A(1, "T", "A", hours_a), A(2, "T", "B", 1)],
        {"A": day_cap(MON, hours_a), "B": day_cap(MON, 1)},
        {"T": ASSUNTO},
        **kw,
    )


def test_h11_single_class_teacher_is_never_limited():
    ctx = make([A(1, "T", "A", 6)], {"A": day_cap(MON, 6)}, {"T": ASSUNTO})
    res = solve(ctx)
    assert res.status == SolveStatus.OPTIMAL and res.conflicts == []


def test_h11_holds_when_possible():
    # A second teacher can take the fifth hour of class A.
    ctx = make(
        [A(1, "T", "A"), A(2, "U", "A", 1), A(3, "T", "B", 1)],
        {"A": day_cap(MON, 5), "B": day_cap(MON, 1)},
        {"U": ASSUNTO},
    )
    res = solve(ctx)
    assert res.status == SolveStatus.OPTIMAL
    assert kinds(res, "H11") == []
    assert sum(1 for le in res.lessons if le.assignment_id == 1) <= 4


def test_h11_reported_when_forced():
    res = solve(two_class_ctx())
    assert res.status == SolveStatus.OPTIMAL
    (c,) = res.conflicts
    assert (c.kind, c.class_id, c.teacher_id, c.day) == ("H11", "A", "T", MON)
    assert c.action_type == AUTHORIZE_SINGLE_CLASSE_DAY
    assert "5" in c.description
    assert res.override_penalty == RELAX_WEIGHT


def test_h11_four_hours_is_allowed():
    res = solve(two_class_ctx(hours_a=4))
    assert res.status == SolveStatus.OPTIMAL and res.conflicts == []


def test_h11_paired_lesson_counts_once():
    # T gives 4 paired hours to A and B at once: 4 with each class, not 8.
    ctx = make(
        [A(1, "T", "A", 4), A(2, "T", "B", 4)],
        {"A": day_cap(MON, 4), "B": day_cap(MON, 4)},
        {"T": ASSUNTO},
        pairings=[Pairing("A", "B", "S", "T")],
    )
    res = solve(ctx)
    assert res.status == SolveStatus.OPTIMAL and res.conflicts == []


def test_h11_paired_hours_still_count_towards_the_limit():
    ctx = make(
        [A(1, "T", "A", 5), A(2, "T", "B", 5)],
        {"A": day_cap(MON, 5), "B": day_cap(MON, 5)},
        {"T": ASSUNTO},
        pairings=[Pairing("A", "B", "S", "T")],
    )
    res = solve(ctx)
    assert sorted(c.class_id for c in kinds(res, "H11")) == ["A", "B"]


def test_h11_derogation_removes_conflict_and_penalty():
    for d in (
        Derogation(AUTHORIZE_SINGLE_CLASSE_DAY),
        Derogation(AUTHORIZE_SINGLE_CLASSE_DAY, teacher_id="T", class_id="A", day=MON),
    ):
        res = solve(two_class_ctx(derogations=[d]))
        assert res.status == SolveStatus.OPTIMAL
        assert res.conflicts == [] and res.override_penalty == 0


@pytest.mark.parametrize(
    "other",
    [
        Derogation(AUTHORIZE_SINGLE_CLASSE_DAY, teacher_id="X"),
        Derogation(AUTHORIZE_SINGLE_CLASSE_DAY, class_id="B"),
        Derogation(AUTHORIZE_SINGLE_CLASSE_DAY, day=THU),
        Derogation(AUTHORIZE_FRIDAY_LATE_START),
    ],
)
def test_h11_derogation_for_another_scope_still_reports(other):
    res = solve(two_class_ctx(derogations=[other]))
    assert len(kinds(res, "H11")) == 1
    assert res.override_penalty == RELAX_WEIGHT


# ------------------------------------------------------------ cross-cutting


def test_no_violation_is_silent_and_penalties_match_conflicts():
    # One context forcing an H4, an H9, an H10 and an H11 violation at once.
    assignments = [
        A(1, "C", "A", 99),  # contractor, no free cell at hour 3 on Monday
        A(2, "P", "B", 2, "LAB", PRATICA),
        A(3, "TT", "B", 4, "TEO"),
        A(4, "T", "D", 5),
        A(5, "T", "E", 1),
        A(6, "F", "F", 99),  # Friday class forced to start late
    ]
    capacity = {
        "A": day_cap(MON, 6),
        "B": day_cap(THU, 6),
        "D": day_cap(MON, 5),
        "E": day_cap(MON, 1),
        "F": day_cap(FRI, 3),
    }
    types = {"P": ASSUNTO, "TT": ASSUNTO, "T": ASSUNTO}
    res = solve(
        make(
            assignments,
            capacity,
            types,
            availability={
                "C": cells(MON, [0, 1, 2, 4, 5]),
                "F": cells(FRI, [1, 2, 3, 4, 5]),
            },
            availability_relaxed=True,
        )
    )
    assert res.status == SolveStatus.OPTIMAL
    assert sorted(c.kind for c in res.conflicts) == ["H10", "H11", "H4", "H9"]
    assert res.override_penalty == 4 * RELAX_WEIGHT
    assert_consistent(res)
    for c in res.conflicts:
        assert c.class_id is not None and c.day in range(5)
        assert c.description and c.action_type


def test_violations_are_minimised():
    # Two lessons could both be placed on unavailable cells, but one is enough:
    # 08:00 is free, so the solver reports exactly one H4.
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 2)},
        availability={"T": cells(MON, [5])},
        availability_relaxed=True,
    )
    res = solve(ctx)
    assert len(res.conflicts) == 1
    assert_consistent(res)

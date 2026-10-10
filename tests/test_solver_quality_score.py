"""Quality score, level and violated-penalty reporting (specs 9.6)."""

import pytest

from scheduling.solver import (
    CLASSE_LATE_START,
    CONTRATTO,
    DEFAULT_SOFT_WEIGHTS,
    RELAX_WEIGHT,
    SINGLE_CLASSE_DAY,
    TEORIA_CONSECUTIVE,
    Assignment,
    ScheduleContext,
    SolveStatus,
    quality,
    solve,
)
from scheduling.solver import model as solver_model

MON, FRI = 0, 4


def day_cap(day, cap):
    return {d: (cap if d == day else 0) for d in range(5)}


def A(id, teacher, klass, residual=99):
    return Assignment(id, teacher, klass, "S", residual)


def make(assignments, capacity, **kw):
    return ScheduleContext(
        assignments=assignments, class_capacity=capacity, timeout_seconds=20, **kw
    )


# ------------------------------------------------------- level and boundaries


def test_all_penalties_zero_is_100_and_a():
    assert quality(True, 40, 0) == (100.0, "A")


def test_exactly_95_is_b_not_a():
    assert quality(True, 20, 1) == (95.0, "B")


def test_just_above_95_is_a():
    score, level = quality(True, 40, 1)
    assert score == 97.5 and level == "A"


def test_exactly_80_is_c_not_b():
    assert quality(True, 5, 1) == (80.0, "C")


def test_just_above_80_is_b():
    score, level = quality(True, 25, 4)
    assert score == 84.0 and level == "B"


def test_everything_violated_is_0_and_c():
    assert quality(True, 3, 3) == (0.0, "C")


def test_no_solution_is_0_and_c():
    assert quality(False, 10, 0) == (0.0, "C")


def test_no_penalty_term_at_all_is_100_and_a():
    # Documented choice: nothing to violate, so no division by zero.
    assert quality(True, 0, 0) == (100.0, "A")


# --------------------------------------------------------------- through solve


def test_infeasible_has_score_0_level_c_and_no_conflicts():
    # Full-day class with a teacher who can only give 2 hours (H6 + H2).
    ctx = make([A(1, "T", "A", 2)], {"A": day_cap(MON, 6)})
    res = solve(ctx, fallback=False)  # the rigid model alone
    assert res.status == SolveStatus.INFEASIBLE
    assert (res.quality_score, res.quality_level) == (0.0, "C")
    assert res.n_soft_conflicts == 0
    assert res.violations == [] and res.conflicts == []


def test_unknown_status_has_score_0_level_c_and_no_conflicts(monkeypatch):
    monkeypatch.setattr(solver_model, "_STATUS", {})  # every status -> UNKNOWN
    res = solve(make([A(1, "T", "A")], {"A": day_cap(MON, 2)}))
    assert res.status == SolveStatus.UNKNOWN
    assert (res.quality_score, res.quality_level) == (0.0, "C")
    assert res.n_soft_conflicts == 0 and res.violations == []


def test_clean_schedule_is_100_and_a():
    ctx = make([A(1, "T1", "A"), A(2, "T2", "A")], {"A": day_cap(MON, 4)})
    res = solve(ctx)
    assert res.n_penalties > 0 and res.violations == []
    assert (res.quality_score, res.quality_level) == (100.0, "A")


def test_case_without_penalty_terms_is_100_and_a():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 6)})
    off = {TEORIA_CONSECUTIVE: 0, SINGLE_CLASSE_DAY: 0}
    res = solve(ctx, off)
    assert res.n_penalties == 0
    assert (res.quality_score, res.quality_level) == (100.0, "A")


def test_empty_context_is_100_and_a():
    res = solve(ScheduleContext())
    assert (res.quality_score, res.quality_level) == (100.0, "A")
    assert res.n_soft_conflicts == 0


def test_score_follows_the_violated_share_of_the_penalties():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 2)}, class_min_start={"A": 10})
    res = solve(ctx)
    assert res.n_soft_conflicts == 1
    expected = (res.n_penalties - 1) * 100 / res.n_penalties
    assert res.quality_score == pytest.approx(expected)
    assert res.quality_level == quality(True, res.n_penalties, 1)[1]


def test_overrides_count_as_penalties_with_their_relax_weight():
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(FRI, 3)},
        teacher_types={"T": CONTRATTO},
        availability={"T": frozenset((FRI, h) for h in range(1, 6))},
    )
    res = solve(ctx)
    (h9,) = [v for v in res.violations if v.kind == "H9"]
    assert (h9.class_id, h9.teacher_id, h9.day, h9.weight) == (
        "A",
        None,
        FRI,
        RELAX_WEIGHT,
    )
    assert h9.weight == RELAX_WEIGHT
    assert res.n_soft_conflicts == len(res.violations) == 2  # H9 and one S2
    assert res.quality_score < 100


def test_n_soft_conflicts_equals_the_returned_violated_penalties():
    ctx = make(
        [A(1, "T", "A"), A(2, "T", "B")],
        {"A": day_cap(MON, 3), "B": day_cap(MON, 3)},
        class_min_start={"B": 10},
        targets={1: 1},
    )
    res = solve(ctx)
    assert res.n_soft_conflicts == len(res.violations) > 0
    for v in res.violations:
        assert v.kind and v.weight > 0 and v.value >= 1
        assert v.day in (MON, None)  # S3 is weekly: no day
        assert v.class_id in ("A", "B", None)
    kinds = {v.kind for v in res.violations}
    assert CLASSE_LATE_START in kinds
    # Each class is listed with the weight used.
    late = [v for v in res.violations if v.kind == CLASSE_LATE_START]
    assert all(v.weight == DEFAULT_SOFT_WEIGHTS[CLASSE_LATE_START] for v in late)
    assert res.n_penalties >= res.n_soft_conflicts

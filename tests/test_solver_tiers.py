"""Penalty tiers of the solver stay separated (specs 9.4, issue #69).

Worst-case sum argument, for one school of 40 classes, 5 days, <= 30 hours per
class and week, <= 100 teachers:

- preferences: the scaled maximum per violation is 1_000 * 1.5 = 1_500; with
  <= 2_000 preference violations a week they sum to <= 3.0e6, below the
  cheapest exact-target hour (0.5 * 10_000_000 = 5.0e6);
- exact target: <= 200 hours of deviation * ~1.5e7 (largest scaled weight) =
  3.0e9, below RELAX_WEIGHT (1e10);
- quasi-hard: <= 50 override violations * 1e10 = 5.0e11, below
  FIXED_PENALTY_WEIGHT (1e12);
- range: <= 1_200 unfilled hours (40 * 30) * 1e12 = 1.2e15 < 2**53, the limit
  of the exact double conversion of CP-SAT.
"""

from scheduling.solver import (
    DEFAULT_SOFT_WEIGHTS,
    EXACT_TARGET_WEIGHT,
    FIXED_PENALTY_WEIGHT,
    MAX_SOFT_WEIGHT,
    ORE_TARGET_DEVIATION,
    RELAX_WEIGHT,
    SINGLE_CLASSE_DAY,
    TEORIA_CONSECUTIVE,
    Assignment,
    ScheduleContext,
    SolveStatus,
    solve,
)

MON, FRI = 0, 4
MAX_WEIGHTS = {k: MAX_SOFT_WEIGHT for k in DEFAULT_SOFT_WEIGHTS}
MAX_SCARCITY = 1.5


def A(id, teacher, klass, residual=99):
    return Assignment(id, teacher, klass, "S", residual, "")


def caps(per_day):
    return {d: per_day for d in range(5)}


def make(assignments, capacity, **kw):
    return ScheduleContext(
        assignments=assignments, class_capacity=capacity, timeout_seconds=20, **kw
    )


def hours(res, assignment_id):
    return [le for le in res.lessons if le.assignment_id == assignment_id]


def count(res, kind):
    return sum(v.value for v in res.violations if v.kind == kind)


# ---------------------------------------------------------------- constants


def test_tier_constants_are_separated():
    s3_max_base = max(DEFAULT_SOFT_WEIGHTS.values(), default=0)
    assert (
        FIXED_PENALTY_WEIGHT
        > RELAX_WEIGHT
        > EXACT_TARGET_WEIGHT + s3_max_base
        > MAX_SOFT_WEIGHT
    )


def test_worst_case_sum_inequalities():
    assert 2000 * MAX_SCARCITY * MAX_SOFT_WEIGHT < 0.5 * EXACT_TARGET_WEIGHT
    base = DEFAULT_SOFT_WEIGHTS[ORE_TARGET_DEVIATION]
    assert 200 * MAX_SCARCITY * (EXACT_TARGET_WEIGHT + base) < RELAX_WEIGHT
    assert 50 * RELAX_WEIGHT < FIXED_PENALTY_WEIGHT
    assert 1200 * FIXED_PENALTY_WEIGHT < 2**53


# -------------------------------------------------------- solver tier ordering


def test_exact_target_beats_ten_preference_violations():
    # T (also in B, where it teaches nothing) must teach all 3 hours of A on
    # each of 5 days to reach a target of 15: that breaks S5 and S2 every day
    # (10 preference violations at MAX_SOFT_WEIGHT). Handing a single hour to U
    # would avoid two of them, at one hour of deviation.
    ctx = make(
        [A(1, "T", "A"), A(2, "U", "A"), A(3, "T", "B", 0)],
        {"A": caps(3), "B": caps(0)},
        targets={1: 15},
    )
    res = solve(ctx, MAX_WEIGHTS)
    assert res.status == SolveStatus.OPTIMAL
    assert len(hours(res, 1)) == 15
    assert [v for v in res.violations if v.kind == ORE_TARGET_DEVIATION] == []
    assert count(res, SINGLE_CLASSE_DAY) == 5
    assert count(res, TEORIA_CONSECUTIVE) == 5
    total = sum(v.weight * v.value for v in res.violations)
    assert total == 10 * MAX_SOFT_WEIGHT
    assert total < EXACT_TARGET_WEIGHT


def test_target_deviation_is_accepted_to_avoid_a_quasi_hard_violation():
    # T (also in B) would reach a target of 12 only with 6 hours of A on both
    # days, a 5th and 6th hour breaking H11 each day. Four hours of deviation
    # cost less than the two overrides.
    ctx = make(
        [A(1, "T", "A"), A(2, "U", "A"), A(3, "T", "B", 0)],
        {"A": {**caps(0), MON: 6, 1: 6}, "B": caps(0)},
        targets={1: 12},
    )
    res = solve(ctx)
    assert res.status == SolveStatus.OPTIMAL
    assert res.conflicts == []
    assert res.override_penalty == 0
    assert len(hours(res, 1)) == 8
    (dev,) = [v for v in res.violations if v.kind == ORE_TARGET_DEVIATION]
    assert dev.value == 4


def test_quasi_hard_override_is_accepted_to_cover_an_unfilled_hour():
    # A needs 6 hours on Monday; T (also in B) can give 5 at most: the rigid
    # model is INFEASIBLE. In best-effort mode the 5th hour of T breaks H11
    # (RELAX_WEIGHT) but covers an hour (FIXED_PENALTY_WEIGHT).
    ctx = make(
        [A(1, "T", "A", 5), A(2, "T", "B", 0)],
        {"A": {**caps(0), MON: 6}, "B": caps(0)},
    )
    assert solve(ctx, fallback=False).status == SolveStatus.INFEASIBLE
    res = solve(ctx)
    assert res.status == SolveStatus.OPTIMAL
    assert len(hours(res, 1)) == 5
    assert [c.kind for c in res.conflicts] == ["H11"]
    assert res.override_penalty == RELAX_WEIGHT
    unfilled = [v for v in res.violations if v.kind == "classe_unfilled"]
    assert [v.weight for v in unfilled] == [FIXED_PENALTY_WEIGHT]

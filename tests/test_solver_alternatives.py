"""Alternatives by weight variation (#34): pure solver-side functions."""

import time
from collections import defaultdict
from unittest import mock

import pytest

from scheduling.solver import (
    CLASSE_LATE_START,
    CONTRACTOR_GAP,
    CONTRATTO,
    DEFAULT_SOFT_WEIGHTS,
    MAX_SOFT_WEIGHT,
    ORE_TARGET_DEVIATION,
    SINGLE_CLASSE_DAY,
    TEORIA_CONSECUTIVE,
    Assignment,
    Lesson,
    ScheduleContext,
    SolveResult,
    SolveStatus,
    generate_alternatives,
    resolve_weights,
    weight_variations,
)
from scheduling.solver import alternatives as alt_module
from scheduling.solver.alternatives import (
    LOWER_FACTOR,
    MAX_ALTERNATIVES,
    MIN_SOLVE_SECONDS,
    RAISE_FACTOR,
    lesson_set,
    varied,
    violation_counts,
)
from scheduling.solver.context import Penalty


def test_default_variations_are_the_base_then_two_single_weight_changes():
    base, lower, higher = weight_variations(None)
    assert base.weights == DEFAULT_SOFT_WEIGHTS and base.changed is None
    # Heaviest default weight halved, second heaviest raised by half.
    assert lower.changed == CONTRACTOR_GAP and lower.factor == LOWER_FACTOR
    assert lower.new == round(DEFAULT_SOFT_WEIGHTS[CONTRACTOR_GAP] * LOWER_FACTOR)
    assert higher.changed == CLASSE_LATE_START and higher.factor == RAISE_FACTOR
    assert higher.new == round(DEFAULT_SOFT_WEIGHTS[CLASSE_LATE_START] * RAISE_FACTOR)
    assert [v.index for v in (base, lower, higher)] == [0, 1, 2]


def test_a_variation_changes_exactly_one_weight():
    base = resolve_weights(None)
    for v in weight_variations(None)[1:]:
        changed = {k for k in base if v.weights[k] != base[k]}
        assert changed == {v.changed}
        assert v.old == base[v.changed] and v.new == v.weights[v.changed]


def test_variations_are_deterministic():
    weights = {CLASSE_LATE_START: 321, CONTRACTOR_GAP: 123}
    assert weight_variations(weights) == weight_variations(weights)
    assert weight_variations(None) == weight_variations(dict(DEFAULT_SOFT_WEIGHTS))


def test_base_weights_are_the_ones_given_over_the_defaults():
    base = weight_variations({TEORIA_CONSECUTIVE: 900})[0]
    assert base.weights[TEORIA_CONSECUTIVE] == 900
    assert base.weights[CLASSE_LATE_START] == DEFAULT_SOFT_WEIGHTS[CLASSE_LATE_START]


def test_ties_are_broken_by_the_order_of_the_defaults():
    flat = dict.fromkeys(DEFAULT_SOFT_WEIGHTS, 100)
    _, lower, higher = weight_variations(flat)
    keys = list(DEFAULT_SOFT_WEIGHTS)
    assert (lower.changed, higher.changed) == (keys[0], keys[1])


def test_varied_is_clamped_between_one_and_the_ceiling():
    assert varied(MAX_SOFT_WEIGHT, RAISE_FACTOR) == MAX_SOFT_WEIGHT
    assert varied(1, LOWER_FACTOR) == 1
    assert varied(MAX_SOFT_WEIGHT, LOWER_FACTOR) == MAX_SOFT_WEIGHT // 2
    assert varied(3, RAISE_FACTOR) == round(3 * RAISE_FACTOR)


@pytest.mark.parametrize(
    "weights",
    [
        dict.fromkeys(DEFAULT_SOFT_WEIGHTS, MAX_SOFT_WEIGHT),
        dict.fromkeys(DEFAULT_SOFT_WEIGHTS, 1),
        {CLASSE_LATE_START: MAX_SOFT_WEIGHT - 1, CONTRACTOR_GAP: MAX_SOFT_WEIGHT},
        {k: 2 for k in DEFAULT_SOFT_WEIGHTS},
    ],
)
def test_every_variation_stays_in_bounds_and_is_a_valid_weight_map(weights):
    for v in weight_variations(weights):
        assert set(v.weights) == set(DEFAULT_SOFT_WEIGHTS)
        resolve_weights(v.weights)  # raises on an unknown key or an out-of-range value
        if v.changed:
            assert 1 <= v.new <= MAX_SOFT_WEIGHT


def test_a_variation_that_changes_nothing_is_dropped():
    # Everything at the ceiling: halving still changes the heaviest, raising
    # the second heaviest is clamped to itself and dropped.
    maxed = dict.fromkeys(DEFAULT_SOFT_WEIGHTS, MAX_SOFT_WEIGHT)
    assert [v.changed for v in weight_variations(maxed)] == [None, CLASSE_LATE_START]
    # Everything at 1: halving clamps back to 1 and is dropped, the raise stays.
    ones = dict.fromkeys(DEFAULT_SOFT_WEIGHTS, 1)
    assert [v.changed for v in weight_variations(ones)] == [None, TEORIA_CONSECUTIVE]


def test_a_switched_off_weight_is_never_varied():
    weights = {k: 0 for k in DEFAULT_SOFT_WEIGHTS} | {SINGLE_CLASSE_DAY: 40}
    variations = weight_variations(weights)
    assert [v.changed for v in variations] == [None, SINGLE_CLASSE_DAY]
    for v in variations:
        assert (
            v.weights[CLASSE_LATE_START] == 0 and v.weights[ORE_TARGET_DEVIATION] == 0
        )
    assert weight_variations(dict.fromkeys(DEFAULT_SOFT_WEIGHTS, 0))[1:] == []


def test_at_most_three_alternatives():
    assert len(weight_variations(None)) == MAX_ALTERNATIVES


def test_bad_base_weights_are_rejected():
    with pytest.raises(ValueError):
        weight_variations({"nope": 1})


# ---- the loop, with a stand-in solver -----------------------------------------


def result(lessons=(), status=SolveStatus.OPTIMAL, violations=()):
    return SolveResult(
        status,
        [Lesson(*le) for le in lessons],
        violations=list(violations),
        quality_score=90.0,
        quality_level="B",
    )


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def run(results, total=60.0, clock=None, ctx=None):
    """generate_alternatives with `solve` answering from `results` in order."""
    clock = clock or Clock()
    calls = []

    def fake(run_ctx, weights=None):
        calls.append((run_ctx, weights))
        item = results[min(len(calls), len(results)) - 1]  # the last one repeats
        return item(clock) if callable(item) else item

    with mock.patch.object(alt_module, "solve", fake):
        outcome = generate_alternatives(ctx or ScheduleContext(), None, total, clock)
    return outcome, calls


def test_identical_lesson_sets_are_collapsed_into_one():
    same = result([(1, 0, 0)])
    outcome, calls = run([same, same, same])
    assert len(calls) == 3
    assert len(outcome.alternatives) == 1 and outcome.collapsed == 2


def test_only_the_duplicates_are_collapsed():
    a, b = result([(1, 0, 0)]), result([(1, 0, 1)])
    outcome, _ = run([a, b, a])
    assert [x.variation.index for x in outcome.alternatives] == [0, 1]
    assert outcome.collapsed == 1


def test_lesson_order_does_not_matter_for_collapsing():
    a = result([(1, 0, 0), (2, 1, 1)])
    b = result([(2, 1, 1), (1, 0, 0)])
    assert lesson_set(a) == lesson_set(b)
    outcome, _ = run([a, b, b])
    assert len(outcome.alternatives) == 1


def test_each_solve_gets_the_weights_of_its_variation():
    _, calls = run([result([(1, 0, 0)]), result([(1, 0, 1)]), result([(1, 0, 2)])])
    assert [w for _, w in calls] == [v.weights for v in weight_variations(None)]


def test_violations_are_counted_per_kind():
    p = Penalty(CLASSE_LATE_START, None, None, 0, 5)
    q = Penalty(CONTRACTOR_GAP, None, None, 0, 5)
    assert violation_counts(result(violations=[p, p, q])) == {
        CLASSE_LATE_START: 2,
        CONTRACTOR_GAP: 1,
    }
    outcome, _ = run([result([(1, 0, 0)], violations=[p, p, q])])
    assert outcome.alternatives[0].violations_by_kind == {
        CLASSE_LATE_START: 2,
        CONTRACTOR_GAP: 1,
    }


def test_an_infeasible_base_stops_everything():
    outcome, calls = run([result(status=SolveStatus.INFEASIBLE)])
    assert len(calls) == 1
    assert outcome.alternatives == []
    assert outcome.first_status == SolveStatus.INFEASIBLE


def test_a_base_without_a_solution_in_time_is_reported():
    outcome, calls = run([result(status=SolveStatus.UNKNOWN)])
    assert len(calls) == 1 and outcome.first_status == SolveStatus.UNKNOWN


def test_a_variation_without_a_solution_is_skipped_not_fatal():
    outcome, calls = run(
        [result([(1, 0, 0)]), result(status=SolveStatus.UNKNOWN), result([(1, 0, 1)])]
    )
    assert len(calls) == 3
    assert [a.variation.index for a in outcome.alternatives] == [0, 2]


def test_a_feasible_but_not_optimal_solution_counts():
    outcome, _ = run([result([(1, 0, 0)], status=SolveStatus.FEASIBLE)])
    assert len(outcome.alternatives) == 1


def test_the_whole_run_is_budgeted_not_each_solve():
    total = 30.0
    clock = Clock()
    ctxs = []

    def fake(run_ctx, weights=None):
        ctxs.append(run_ctx)
        clock.now += run_ctx.timeout_seconds  # every solve uses all it is allowed
        return result([(len(ctxs), 0, 0)])

    with mock.patch.object(alt_module, "solve", fake):
        generate_alternatives(ScheduleContext(), None, total, clock)
    assert len(ctxs) == 3
    assert sum(c.timeout_seconds for c in ctxs) <= total
    assert clock.now <= total


def test_time_is_shared_so_a_fast_solve_leaves_more_to_the_next():
    clock = Clock()
    ctxs = []

    def fake(run_ctx, weights=None):
        ctxs.append(run_ctx)
        clock.now += 1.0  # every solve is quick
        return result([(len(ctxs), 0, 0)])

    with mock.patch.object(alt_module, "solve", fake):
        generate_alternatives(ScheduleContext(), None, 30.0, clock)
    first, second, third = (c.timeout_seconds for c in ctxs)
    assert first < second < third
    assert third <= 30.0


def test_no_solve_starts_when_the_time_is_up():
    clock = Clock()
    ctxs = []

    def fake(run_ctx, weights=None):
        ctxs.append(run_ctx)
        clock.now += 100.0  # the first solve eats everything
        return result([(1, 0, 0)])

    with mock.patch.object(alt_module, "solve", fake):
        outcome = generate_alternatives(ScheduleContext(), None, 30.0, clock)
    assert len(ctxs) == 1
    assert len(outcome.alternatives) == 1  # what was found so far is returned


def test_a_solve_is_not_started_with_less_than_the_minimum_time():
    clock = Clock()
    ctxs = []

    def fake(run_ctx, weights=None):
        ctxs.append(run_ctx)
        clock.now += 10.0 - MIN_SOLVE_SECONDS / 2
        return result([(len(ctxs), 0, 0)])

    with mock.patch.object(alt_module, "solve", fake):
        generate_alternatives(ScheduleContext(), None, 10.0, clock)
    assert len(ctxs) == 1
    assert all(
        c.timeout_seconds >= MIN_SOLVE_SECONDS * alt_module.SOLVE_SHARE for c in ctxs
    )


def test_only_the_first_solve_may_explain_an_infeasible_week():
    ctxs = []

    def fake(run_ctx, weights=None):
        ctxs.append(run_ctx)
        return result([(len(ctxs), 0, 0)])

    base = ScheduleContext(explain_budget_seconds=10.0)
    with mock.patch.object(alt_module, "solve", fake):
        generate_alternatives(base, None, 60.0, Clock())
    assert ctxs[0].explain_budget_seconds > 0
    assert [c.explain_budget_seconds for c in ctxs[1:]] == [0.0, 0.0]
    assert ctxs[0].explain_budget_seconds <= base.explain_budget_seconds


# ---- the real solver ----------------------------------------------------------

MON = 0


def gap_ctx():
    # Class A may start at 08:00, class B only from 10:00: the contractor's
    # natural pattern leaves an unavoidable-or-avoidable hole at 09:00.
    return ScheduleContext(
        assignments=[
            Assignment(1, "T", "A", "S", 99),
            Assignment(2, "T", "B", "S", 99),
        ],
        teacher_types={"T": CONTRATTO},
        class_capacity={
            c: {d: (1 if d == MON else 0) for d in range(5)} for c in ("A", "B")
        },
        class_min_start={"B": 10},
        timeout_seconds=20,
    )


def assert_hard_constraints(ctx, lessons):
    """H1, H1b, H2 (exact capacity), H3-style start rule for the small context."""
    by_id = {a.id: a for a in ctx.assignments}
    teacher_cells = defaultdict(int)
    class_cells = defaultdict(int)
    per_class_day = defaultdict(int)
    for le in lessons:
        a = by_id[le.assignment_id]
        teacher_cells[a.teacher_id, le.day, le.hour] += 1
        class_cells[a.class_id, le.day, le.hour] += 1
        per_class_day[a.class_id, le.day] += 1
        assert le.hour + 8 >= ctx.class_min_start.get(a.class_id, 8)
    assert max(teacher_cells.values()) == 1
    assert max(class_cells.values()) == 1
    for class_id, days in ctx.class_capacity.items():
        for day, cap in days.items():
            assert per_class_day[class_id, day] == cap
    for a in ctx.assignments:
        assert sum(1 for le in lessons if le.assignment_id == a.id) <= a.residual_hours


def test_real_solver_gives_distinct_alternatives_that_keep_the_hard_constraints():
    ctx = gap_ctx()
    weights = {CLASSE_LATE_START: 500, CONTRACTOR_GAP: 600}
    outcome = generate_alternatives(ctx, weights, 60.0, time.monotonic)
    assert len(outcome.alternatives) >= 2
    assert len({lesson_set(a.result) for a in outcome.alternatives}) == len(
        outcome.alternatives
    )
    for alt in outcome.alternatives:
        assert alt.result.status == SolveStatus.OPTIMAL
        assert_hard_constraints(ctx, alt.result.lessons)
    scores = {tuple(sorted(a.violations_by_kind.items())) for a in outcome.alternatives}
    assert len(scores) >= 2  # the alternatives really trade one kind for another


def test_hard_constraint_checker_catches_a_double_booking():
    ctx = gap_ctx()
    with pytest.raises(AssertionError):
        assert_hard_constraints(ctx, [Lesson(1, 0, 2), Lesson(2, 0, 2)])
    with pytest.raises(AssertionError):
        assert_hard_constraints(ctx, [Lesson(1, 0, 0)])

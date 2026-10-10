"""Repair mode of the solver (architecture 7.4): perturbation, change penalty, diff."""

import dataclasses

import pytest

from scheduling.solver import (
    CLASS_CLOSED,
    CLASSE_UNFILLED,
    SLOT_REMOVED,
    TEACHER_ABSENT,
    Assignment,
    Change,
    Lesson,
    Pairing,
    Perturbation,
    ScheduleContext,
    SolveStatus,
    affected_lessons,
    apply_perturbation,
    diff_lessons,
    solve,
)
from scheduling.solver.context import (
    CHANGE_WEIGHT,
    DEFAULT_SOFT_WEIGHTS,
    EXACT_TARGET_WEIGHT,
    RELAX_WEIGHT,
)


def A(id, teacher, klass, residual=99):
    return Assignment(id, teacher, klass, "S", residual, "TEORIA")


def ctx_of(assignments, cap=2, classes=("c1",), **kw):
    return ScheduleContext(
        assignments=assignments,
        teacher_types={a.teacher_id: "ASSUNTO" for a in assignments},
        class_capacity={c: {d: cap for d in range(5)} for c in classes},
        timeout_seconds=20,
        **kw,
    )


def cells(res, aid=None):
    return {
        (le.assignment_id, le.day, le.hour)
        for le in res.lessons
        if aid is None or le.assignment_id == aid
    }


# ---- the perturbation reshapes the context ----


def test_teacher_absent_excludes_the_days_and_keeps_existing_exclusions():
    ctx = ctx_of(
        [A("a", "t", "c1")], excluded_days={"t": frozenset({0}), "u": frozenset({4})}
    )
    out = apply_perturbation(
        ctx, Perturbation(TEACHER_ABSENT, "t", days=frozenset({2, 3}))
    )
    assert out.excluded_days == {"t": frozenset({0, 2, 3}), "u": frozenset({4})}
    assert ctx.excluded_days["t"] == frozenset({0})  # the input is not mutated


def test_class_closed_zeroes_the_capacity_of_those_days_only():
    ctx = ctx_of([A("a", "t", "c1")], cap=3, classes=("c1", "c2"))
    out = apply_perturbation(
        ctx, Perturbation(CLASS_CLOSED, class_id="c1", days=frozenset({1, 4}))
    )
    assert out.class_capacity["c1"] == {0: 3, 1: 0, 2: 3, 3: 3, 4: 0}
    assert out.class_capacity["c2"] == {d: 3 for d in range(5)}
    assert ctx.class_capacity["c1"][1] == 3


def test_slot_removed_blocks_the_cell_and_takes_one_hour_off_the_day():
    ctx = ctx_of([A("a", "t", "c1")], cap=4)
    pert = Perturbation(SLOT_REMOVED, class_id="c1", days=frozenset({2}), hour=1)
    out = apply_perturbation(ctx, pert)
    assert out.blocked_cells == {"c1": frozenset({(2, 1)})}
    assert out.class_capacity["c1"][2] == 3
    assert out.class_capacity["c1"][1] == 4


def test_slot_removed_on_a_closed_day_does_not_go_negative():
    ctx = ctx_of([A("a", "t", "c1")], cap=0)
    pert = Perturbation(SLOT_REMOVED, class_id="c1", days=frozenset({0}), hour=0)
    assert apply_perturbation(ctx, pert).class_capacity["c1"][0] == 0


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        apply_perturbation(ctx_of([]), Perturbation("earthquake"))


# ---- which lessons a perturbation takes away ----


def lessons(*cells_):
    return [Lesson(*c) for c in cells_]


def test_affected_lessons_per_kind():
    ctx = ctx_of(
        [A("a", "t1", "c1"), A("b", "t2", "c1"), A("c", "t1", "c2")],
        classes=("c1", "c2"),
    )
    cur = lessons(("a", 0, 0), ("b", 0, 1), ("c", 1, 0), ("a", 1, 1))
    absent = Perturbation(TEACHER_ABSENT, "t1", days=frozenset({0}))
    assert affected_lessons(ctx, absent, cur) == lessons(("a", 0, 0))
    closed = Perturbation(CLASS_CLOSED, class_id="c1", days=frozenset({0, 1}))
    assert affected_lessons(ctx, closed, cur) == lessons(
        ("a", 0, 0), ("b", 0, 1), ("a", 1, 1)
    )
    removed = Perturbation(SLOT_REMOVED, class_id="c1", days=frozenset({0}), hour=1)
    assert affected_lessons(ctx, removed, cur) == lessons(("b", 0, 1))
    other_day = Perturbation(TEACHER_ABSENT, "t1", days=frozenset({3}))
    assert affected_lessons(ctx, other_day, cur) == []


# ---- the diff ----


def test_diff_classifies_moved_removed_and_added():
    old = lessons(("a", 0, 0), ("a", 0, 1), ("b", 2, 3), ("c", 1, 1))
    new = lessons(("a", 0, 0), ("a", 3, 2), ("c", 1, 1), ("d", 4, 0))
    assert diff_lessons(old, new) == [
        Change("a", (0, 1), (3, 2)),
        Change("b", (2, 3), None),
        Change("d", None, (4, 0)),
    ]


def test_diff_of_identical_timetables_is_empty():
    same = lessons(("a", 0, 0), ("b", 1, 1))
    assert diff_lessons(same, list(reversed(same))) == []


def test_diff_pairs_a_removed_cell_with_the_added_cell_of_the_same_day():
    old = lessons(("a", 0, 0), ("a", 3, 0))
    new = lessons(("a", 3, 2), ("a", 1, 1))
    changes = diff_lessons(old, new)
    assert Change("a", (3, 0), (3, 2)) in changes
    assert Change("a", (0, 0), (1, 1)) in changes
    assert len(changes) == 2


def test_diff_leaves_extra_removals_and_additions_unpaired():
    old = lessons(("a", 0, 0), ("a", 0, 1), ("a", 0, 2))
    new = lessons(("a", 1, 0))
    changes = diff_lessons(old, new)
    assert sum(1 for c in changes if c.old and c.new) == 1
    assert sum(1 for c in changes if c.old and not c.new) == 2
    assert not any(c.new and not c.old for c in changes)


# ---- the change penalty ----


def test_weights_order_the_objective():
    soft_max = max(DEFAULT_SOFT_WEIGHTS.values())
    assert soft_max < CHANGE_WEIGHT < EXACT_TARGET_WEIGHT < RELAX_WEIGHT


def late_start_ctx(current):
    """One class with 2 hours a day; starting at 09:00 costs S1, changing costs more."""
    ctx = ctx_of([A("a", "t", "c1", residual=10)], cap=2, current_lessons=current)
    # Friday is closed: H9 (a Friday block starts at 08:00) is not the point here.
    return dataclasses.replace(
        ctx, class_capacity={"c1": {0: 2, 1: 2, 2: 2, 3: 2, 4: 0}}
    )


def test_without_current_lessons_the_soft_preference_moves_the_lessons():
    res = solve(late_start_ctx(None))
    assert (("a", 0, 0)) in cells(res)  # starts at 08:00 everywhere


def test_with_current_lessons_the_timetable_is_kept_even_if_a_preference_dislikes_it():
    current = [Lesson("a", d, h) for d in range(4) for h in (1, 2)]  # starts at 09:00
    res = solve(late_start_ctx(current))
    assert cells(res) == {("a", d, h) for d in range(4) for h in (1, 2)}


def test_the_change_penalty_is_not_a_quality_penalty():
    plain = solve(late_start_ctx(None))
    current = [Lesson("a", d, h) for d in range(4) for h in (0, 1)]
    repaired = solve(late_start_ctx(current))
    assert repaired.n_penalties == plain.n_penalties
    assert repaired.quality_score == plain.quality_score


def test_a_lesson_that_must_change_costs_exactly_what_it_has_to():
    # The class is closed on day 0: its two lessons go, nothing else moves.
    current = [Lesson("a", d, h) for d in range(4) for h in (0, 1)]
    ctx = apply_perturbation(
        late_start_ctx(current),
        Perturbation(CLASS_CLOSED, class_id="c1", days=frozenset({0})),
    )
    res = solve(ctx)
    assert cells(res) == {("a", d, h) for d in range(1, 4) for h in (0, 1)}


# ---- repair results ----


def two_teacher_ctx(current):
    return ctx_of(
        [A("a", "t1", "c1"), A("b", "t2", "c1")],
        cap=2,
        current_lessons=current,
    )


def test_absent_teacher_gets_no_lesson_that_day_and_the_rest_stays():
    current = [Lesson(aid, d, h) for d in range(5) for h, aid in ((0, "a"), (1, "b"))]
    ctx = apply_perturbation(
        two_teacher_ctx(current),
        Perturbation(TEACHER_ABSENT, "t1", days=frozenset({2})),
    )
    res = solve(ctx)
    assert res.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    assert not [le for le in res.lessons if le.assignment_id == "a" and le.day == 2]
    # Wednesday is covered by the other teacher; every other day is as before.
    assert {(le.day, le.hour) for le in res.lessons if le.day == 2} == {(2, 0), (2, 1)}
    kept = {(le.assignment_id, le.day, le.hour) for le in res.lessons if le.day != 2}
    assert kept == {(a, d, h) for (a, d, h) in cells_of(current) if d != 2}


def cells_of(lessons_):
    return {(le.assignment_id, le.day, le.hour) for le in lessons_}


def test_slot_removed_leaves_the_cell_empty_and_the_class_one_lesson_short():
    current = [Lesson(aid, d, h) for d in range(5) for h, aid in ((0, "a"), (1, "b"))]
    pert = Perturbation(SLOT_REMOVED, class_id="c1", days=frozenset({1}), hour=1)
    res = solve(apply_perturbation(two_teacher_ctx(current), pert))
    day1 = {(le.assignment_id, le.hour) for le in res.lessons if le.day == 1}
    assert len(day1) == 1
    assert all(h != 1 for _, h in day1)
    assert not [v for v in res.violations if v.kind == CLASSE_UNFILLED]


def test_slot_removed_in_the_middle_moves_the_block_off_the_cell():
    current = [Lesson("a", 0, h) for h in range(3)]
    ctx = ctx_of(
        [A("a", "t", "c1")],
        cap=3,
        current_lessons=current,
    )
    ctx = dataclasses.replace(
        ctx, class_capacity={"c1": {0: 3, 1: 0, 2: 0, 3: 0, 4: 0}}
    )
    pert = Perturbation(SLOT_REMOVED, class_id="c1", days=frozenset({0}), hour=1)
    res = solve(apply_perturbation(ctx, pert))
    hours = sorted(le.hour for le in res.lessons)
    assert len(hours) == 2 and 1 not in hours and hours[1] - hours[0] == 1


def test_class_closed_reports_no_capacity_conflict_that_day():
    current = [Lesson(aid, d, h) for d in range(5) for h, aid in ((0, "a"), (1, "b"))]
    pert = Perturbation(CLASS_CLOSED, class_id="c1", days=frozenset({3}))
    res = solve(apply_perturbation(two_teacher_ctx(current), pert))
    assert not [le for le in res.lessons if le.day == 3]
    assert not [i for i in res.conflict_items if i["kind"] == CLASSE_UNFILLED]
    assert res.message == ""


def test_no_replacement_means_best_effort_with_unfilled_hours_and_an_explanation():
    ctx = ctx_of(
        [A("a", "t1", "c1")],
        cap=2,
        current_lessons=[Lesson("a", d, h) for d in range(5) for h in (0, 1)],
    )
    res = solve(
        apply_perturbation(ctx, Perturbation(TEACHER_ABSENT, "t1", days=frozenset({0})))
    )
    assert res.message
    assert any(i["kind"] == CLASSE_UNFILLED for i in res.conflict_items)
    assert res.explanations


def test_a_paired_lesson_counts_in_both_classes():
    ctx = ScheduleContext(
        assignments=[A("a", "t", "c1"), A("b", "t", "c2")],
        teacher_types={"t": "ASSUNTO"},
        class_capacity={c: {d: 1 for d in range(5)} for c in ("c1", "c2")},
        pairings=[Pairing("c1", "c2", "S", "t")],
        timeout_seconds=20,
    )
    current = [Lesson(a, d, 0) for d in range(5) for a in ("a", "b")]
    ctx = dataclasses.replace(ctx, current_lessons=current)
    res = solve(ctx)
    assert cells(res) == cells_of(current)

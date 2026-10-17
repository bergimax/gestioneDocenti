"""One-off teacher day exclusion (`exclude_day`, H7 fed from a derogation)."""

import datetime

from scheduling.context import build_week_context
from scheduling.derogations import derogation_from_params
from scheduling.solver import (
    CLASSE_UNFILLED,
    EXCLUDE_DAY,
    Assignment,
    Derogation,
    ScheduleContext,
    SolveStatus,
    solve,
)
from school.models import ClassPairing

MON, TUE = 0, 1
SOLVED = (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
WEEK = datetime.date(2026, 10, 12)


def days_of(result, assignment_ids):
    return {les.day for les in result.lessons if les.assignment_id in assignment_ids}


def solve_week(w, *derogations):
    ctx = build_week_context(WEEK, w.school, derogations)
    ctx = ScheduleContext(**{**ctx.__dict__, "timeout_seconds": 30})
    return ctx, solve(ctx)


def rossi_ids(w):
    return {w.asg["a_mat"].pk, w.asg["b_mat"].pk}


def test_derogation_becomes_excluded_days(w):
    ctx = build_week_context(
        WEEK,
        w.school,
        [
            Derogation(EXCLUDE_DAY, teacher_id=w.rossi.pk, day=MON),
            Derogation(EXCLUDE_DAY, teacher_id=w.rossi.pk, day=TUE),
            Derogation(EXCLUDE_DAY, teacher_id=w.bianchi.pk, day=TUE),
        ],
    )
    assert ctx.excluded_days == {
        w.rossi.pk: frozenset({MON, TUE}),
        w.bianchi.pk: frozenset({TUE}),
    }
    assert build_week_context(WEEK, w.school).excluded_days == {}


def test_params_give_a_solver_derogation():
    d = derogation_from_params(
        {
            "quick_action": {
                "action_type": "exclude_day",
                "class_id": None,
                "teacher_id": 7,
                "day": "GIOVEDI",
                "max_hours": None,
            }
        }
    )
    assert d == Derogation(EXCLUDE_DAY, None, 7, 3, None)


def test_teacher_has_no_lesson_on_the_excluded_day(w):
    _, free = solve_week(w)
    assert free.status in SOLVED
    assert MON in days_of(free, rossi_ids(w))  # not vacuous

    _, res = solve_week(w, Derogation(EXCLUDE_DAY, teacher_id=w.rossi.pk, day=MON))
    assert res.status in SOLVED
    assert MON not in days_of(res, rossi_ids(w))


def test_other_teachers_keep_the_day(w):
    _, res = solve_week(w, Derogation(EXCLUDE_DAY, teacher_id=w.rossi.pk, day=MON))
    other = {w.asg["a_ita"].pk, w.asg["b_ita"].pk, w.asg["a_lab"].pk}
    assert MON in days_of(res, other)


def test_paired_lesson_leaves_the_day_for_both_classes(w):
    ClassPairing.objects.create(
        school=w.school, class_a=w.a, class_b=w.b, subject=w.mat, teacher=w.rossi
    )
    _, free = solve_week(w)
    paired = {(les.assignment_id, les.day, les.hour) for les in free.lessons}
    assert any(d == MON and a in rossi_ids(w) for a, d, _ in paired)  # not vacuous
    _, res = solve_week(w, Derogation(EXCLUDE_DAY, teacher_id=w.rossi.pk, day=MON))
    assert res.status in SOLVED
    assert MON not in days_of(res, rossi_ids(w))


def test_infeasible_week_falls_back_and_explains():
    ctx = ScheduleContext(
        assignments=[Assignment(1, "T", "A", "S", 4)],
        class_capacity={"A": {MON: 4, TUE: 0, 2: 0, 3: 0, 4: 0}},
        excluded_days={"T": frozenset({MON})},
        teacher_names={"T": "ROSSI"},
        class_names={"A": "1A"},
        timeout_seconds=20,
    )
    res = solve(ctx)
    assert not [les for les in res.lessons if les.day == MON]
    assert [i for i in res.conflict_items if i["kind"] == CLASSE_UNFILLED]
    text = " ".join(res.explanations)
    assert "non insegna" in text and "lunedì" in text


def test_plain_regeneration_has_no_exclusion(w):
    assert build_week_context(WEEK, w.school, []).excluded_days == {}

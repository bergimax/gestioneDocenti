"""Soft preferences S1-S5, scarcity multiplier and weight map (specs 9.4, 9.5)."""

import math

import pytest

from scheduling.solver import (
    ASSUNTO,
    AUTHORIZE_EARLY_EXIT,
    AUTHORIZE_FRIDAY_LATE_START,
    AUTHORIZE_SINGLE_CLASSE_DAY,
    CLASSE_LATE_START,
    CONTRACTOR_GAP,
    CONTRATTO,
    DEFAULT_SOFT_WEIGHTS,
    EXACT_TARGET_WEIGHT,
    FIXED_PENALTY_WEIGHT,
    FORCE_3_HOURS_THEORY,
    MAX_SOFT_WEIGHT,
    ORE_TARGET_DEVIATION,
    OVERRIDE_AVAILABILITY,
    PRATICA,
    RELAX_WEIGHT,
    SINGLE_CLASSE_DAY,
    TEORIA_CONSECUTIVE,
    Assignment,
    Derogation,
    Pairing,
    ScheduleContext,
    SolveStatus,
    scaled_weight,
    scarcity_multiplier,
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
    """Teachers default to ASSUNTO (no availability, no gap penalty)."""
    return ScheduleContext(
        assignments=assignments,
        teacher_types=types or {},
        class_capacity=capacity,
        timeout_seconds=20,
        **kw,
    )


def of(res, kind):
    return [v for v in res.violations if v.kind == kind]


def solved(ctx, weights=None):
    res = solve(ctx, weights)
    assert res.status == SolveStatus.OPTIMAL
    return res


def hours_of(res, assignment_id):
    return sorted(
        (le.day, le.hour) for le in res.lessons if le.assignment_id == assignment_id
    )


# ---------------------------------------------------------------- S1


def test_s1_four_hour_day_with_free_choice_starts_at_eight():
    res = solved(make([A(1, "T", "A")], {"A": day_cap(MON, 4)}))
    assert [h for _, h in hours_of(res, 1)] == [0, 1, 2, 3]
    assert of(res, CLASSE_LATE_START) == []


def test_s1_late_start_unavoidable_is_reported_once():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 2)}, class_min_start={"A": 10})
    res = solved(ctx)
    (v,) = of(res, CLASSE_LATE_START)
    assert (v.class_id, v.teacher_id, v.day, v.weight) == ("A", None, MON, 100)
    assert min(h for _, h in hours_of(res, 1)) >= 2


def test_s1_full_six_hour_day_has_no_penalty_term():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 6)})
    assert of(solved(ctx), CLASSE_LATE_START) == []
    # With S2 and S5 off nothing else is left: a full day carries no S1 term.
    off = {TEORIA_CONSECUTIVE: 0, SINGLE_CLASSE_DAY: 0}
    assert solved(ctx, off).n_penalties == 0


def test_s1_day_without_lessons_has_no_penalty():
    res = solved(make([A(1, "T", "A")], {"A": day_cap(MON, 0)}))
    assert res.violations == []
    assert res.n_penalties == 0


def test_s1_a_teacher_shared_between_classes_makes_one_start_late():
    ctx = make(
        [A(1, "T", "A"), A(2, "T", "B")],
        {"A": day_cap(MON, 2), "B": day_cap(MON, 2)},
    )
    (v,) = of(solved(ctx), CLASSE_LATE_START)
    assert v.weight == 100 and v.day == MON


def test_s1_weight_zero_removes_its_effects():
    ctx = make(
        [A(1, "T", "A"), A(2, "T", "B")],
        {"A": day_cap(MON, 2), "B": day_cap(MON, 2)},
    )
    res = solved(ctx, {CLASSE_LATE_START: 0})
    assert of(res, CLASSE_LATE_START) == []
    assert res.n_penalties < solved(ctx).n_penalties


def test_s1_weight_override_changes_the_weight_used():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 2)}, class_min_start={"A": 10})
    (v,) = of(solved(ctx, {CLASSE_LATE_START: 37}), CLASSE_LATE_START)
    assert v.weight == 37


def friday_ctx(**kw):
    # Contractor free from 09:00: a Friday block of 3 cannot start at 08:00.
    return make(
        [A(1, "T", "A")],
        {"A": day_cap(FRI, 3)},
        types={"T": CONTRATTO},
        availability={"T": cells(FRI, [1, 2, 3, 4, 5])},
        **kw,
    )


def test_s1_is_not_double_counted_with_a_friday_h9_conflict():
    # The late Friday start is already the H9 penalty (1000): S1 stays silent.
    res = solved(friday_ctx())
    assert [c.kind for c in res.conflicts] == ["H9"]
    assert of(res, CLASSE_LATE_START) == []
    assert sorted(v.kind for v in res.violations) == ["H9", TEORIA_CONSECUTIVE]


def test_s1_applies_on_friday_once_h9_is_derogated():
    ctx = friday_ctx(derogations=[Derogation(AUTHORIZE_FRIDAY_LATE_START)])
    res = solved(ctx)
    assert res.conflicts == []
    assert len(of(res, CLASSE_LATE_START)) == 1


def test_s1_applies_on_thursday_where_h9_does_not():
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(THU, 3)},
        types={"T": CONTRATTO},
        availability={"T": cells(THU, [1, 2, 3, 4, 5])},
    )
    res = solved(ctx)
    assert res.conflicts == []
    assert len(of(res, CLASSE_LATE_START)) == 1


# ---------------------------------------------------------------- S2


def test_s2_two_teachers_can_avoid_three_consecutive_hours():
    ctx = make([A(1, "T1", "A"), A(2, "T2", "A")], {"A": day_cap(MON, 3)})
    res = solved(ctx)
    assert of(res, TEORIA_CONSECUTIVE) == []
    assert len(hours_of(res, 1)) in (1, 2)  # nobody teaches all three


def test_s2_three_consecutive_theory_hours_are_penalised_once():
    res = solved(make([A(1, "T", "A")], {"A": day_cap(MON, 3)}))
    (v,) = of(res, TEORIA_CONSECUTIVE)
    assert (v.class_id, v.teacher_id, v.day, v.weight) == ("A", "T", MON, 10)


def test_s2_is_penalised_once_per_excess_hour():
    res = solved(make([A(1, "T", "A")], {"A": day_cap(MON, 4)}))
    assert len(of(res, TEORIA_CONSECUTIVE)) == 2


def test_s2_two_consecutive_hours_are_fine():
    res = solved(make([A(1, "T", "A")], {"A": day_cap(MON, 2)}))
    assert of(res, TEORIA_CONSECUTIVE) == []


def test_s2_force_3_hours_theory_raises_the_cap():
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 3)},
        derogations=[Derogation(FORCE_3_HOURS_THEORY)],
    )
    assert of(solved(ctx), TEORIA_CONSECUTIVE) == []


def test_s2_force_3_hours_theory_still_caps_at_three():
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 4)},
        derogations=[Derogation(FORCE_3_HOURS_THEORY)],
    )
    assert len(of(solved(ctx), TEORIA_CONSECUTIVE)) == 1


def test_s2_force_3_hours_theory_honours_max_hours():
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 4)},
        derogations=[Derogation(FORCE_3_HOURS_THEORY, max_hours=4)],
    )
    assert of(solved(ctx), TEORIA_CONSECUTIVE) == []


def test_s2_derogation_for_another_scope_still_penalises():
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 3)},
        derogations=[
            Derogation(FORCE_3_HOURS_THEORY, teacher_id="other"),
            Derogation(FORCE_3_HOURS_THEORY, class_id="other"),
            Derogation(FORCE_3_HOURS_THEORY, day=THU),
        ],
    )
    assert len(of(solved(ctx), TEORIA_CONSECUTIVE)) == 1


def test_s2_practical_subjects_are_not_affected():
    ctx = make([A(1, "T", "A", subject_type=PRATICA)], {"A": day_cap(MON, 3)})
    res = solved(ctx)
    assert of(res, TEORIA_CONSECUTIVE) == []
    assert res.conflicts == []


def test_s2_different_teachers_are_not_affected():
    ctx = make(
        [A(1, "T1", "A", 1), A(2, "T2", "A", 1), A(3, "T3", "A", 1)],
        {"A": day_cap(MON, 3)},
    )
    assert of(solved(ctx), TEORIA_CONSECUTIVE) == []


def test_s2_same_teacher_two_subjects_count_together():
    ctx = make(
        [A(1, "T", "A", subject="M"), A(2, "T", "A", subject="N")],
        {"A": day_cap(MON, 3)},
    )
    assert len(of(solved(ctx), TEORIA_CONSECUTIVE)) == 1


def test_s2_weight_zero_removes_it():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 3)})
    assert of(solved(ctx, {TEORIA_CONSECUTIVE: 0}), TEORIA_CONSECUTIVE) == []


# ---------------------------------------------------------------- S3


def test_s3_target_met_gives_no_penalty():
    ctx = make(
        [A(1, "T1", "A"), A(2, "T2", "A")],
        {"A": day_cap(MON, 4)},
        targets={1: 2, 2: 2},
    )
    res = solved(ctx)
    assert len(hours_of(res, 1)) == len(hours_of(res, 2)) == 2
    assert of(res, ORE_TARGET_DEVIATION) == []


def test_s3_deviation_of_two_hours_is_twice_the_per_hour_weight():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 2)}, targets={1: 4})
    (v,) = of(solved(ctx), ORE_TARGET_DEVIATION)
    assert v.value == 2
    assert v.weight == 15 + EXACT_TARGET_WEIGHT
    assert v.weight * v.value == 1030
    assert (v.class_id, v.teacher_id) == ("A", "T")


def test_s3_excess_hours_over_target_are_a_deviation_too():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 3)}, targets={1: 1})
    (v,) = of(solved(ctx), ORE_TARGET_DEVIATION)
    assert v.value == 2


def test_s3_assignment_without_target_is_not_penalised():
    ctx = make(
        [A(1, "T1", "A"), A(2, "T2", "A")],
        {"A": day_cap(MON, 2)},
        targets={1: 2},
    )
    res = solved(ctx)
    assert hours_of(res, 1) == [(MON, 0), (MON, 1)]
    assert hours_of(res, 2) == []  # no target, no lessons, no penalty
    assert of(res, ORE_TARGET_DEVIATION) == []


def test_s3_target_pulls_hours_to_the_assignment_that_needs_them():
    ctx = make(
        [A(1, "T1", "A"), A(2, "T2", "A")],
        {"A": day_cap(MON, 3)},
        targets={1: 1, 2: 2},
    )
    res = solved(ctx)
    assert (len(hours_of(res, 1)), len(hours_of(res, 2))) == (1, 2)


def test_s3_weight_override_changes_the_per_hour_weight():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 2)}, targets={1: 3})
    (v,) = of(solved(ctx, {ORE_TARGET_DEVIATION: 100}), ORE_TARGET_DEVIATION)
    assert v.weight == 600


# ---------------------------------------------------------------- S4


def gap_ctx(types, **kw):
    # Class A may start at 08:00, class B only from 10:00: the teacher's
    # natural pattern is 08:00 + 10:00 with nothing at 09:00.
    return make(
        [A(1, "T", "A"), A(2, "T", "B")],
        {"A": day_cap(MON, 1), "B": day_cap(MON, 1)},
        types=types,
        class_min_start={"B": 10},
        **kw,
    )


def test_s4_contractor_gap_unavoidable_is_penalised_once():
    ctx = make(
        [A(1, "T", "A"), A(2, "T", "B")],
        {"A": day_cap(MON, 1), "B": day_cap(MON, 1)},
        types={"T": CONTRATTO},
        availability={"T": cells(MON, [0, 2])},
    )
    res = solved(ctx)
    assert sorted(h for _, h in hours_of(res, 1) + hours_of(res, 2)) == [0, 2]
    (v,) = of(res, CONTRACTOR_GAP)
    assert (v.class_id, v.teacher_id, v.day) == (None, "T", MON)
    assert v.weight == 215  # 150 * (1.5 - 2/30)


def test_s4_two_gap_hours_are_penalised_twice():
    ctx = make(
        [A(1, "T", "A"), A(2, "T", "B"), A(3, "T", "C")],
        {c: day_cap(MON, 1) for c in "ABC"},
        types={"T": CONTRATTO},
        availability={"T": cells(MON, [0, 2, 4])},
    )
    assert len(of(solved(ctx), CONTRACTOR_GAP)) == 2


def test_s4_contractor_avoids_the_gap_when_it_is_cheaper_to_do_so():
    # Avoiding the gap costs one more late start (100) < the gap (225).
    res = solved(gap_ctx({"T": CONTRATTO}))
    assert of(res, CONTRACTOR_GAP) == []
    assert len(of(res, CLASSE_LATE_START)) == 2


def test_s4_assunto_with_the_same_pattern_gets_no_gap_penalty():
    res = solved(gap_ctx({"T": ASSUNTO}))
    assert of(res, CONTRACTOR_GAP) == []
    # Gaps are free for an ASSUNTO: A keeps 08:00, only B starts late.
    assert hours_of(res, 1) == [(MON, 0)]
    assert hours_of(res, 2)[0][1] >= 2  # B never starts before 10:00
    assert [v.class_id for v in of(res, CLASSE_LATE_START)] == ["B"]


def test_s4_contractor_leaving_early_without_a_gap_gets_none():
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 2)},
        types={"T": CONTRATTO},
        availability={"T": cells(MON, [0, 1])},
    )
    res = solved(ctx)
    assert hours_of(res, 1) == [(MON, 0), (MON, 1)]
    assert of(res, CONTRACTOR_GAP) == []


def test_s4_authorize_early_exit_removes_the_penalty():
    ctx = gap_ctx({"T": CONTRATTO}, derogations=[Derogation(AUTHORIZE_EARLY_EXIT)])
    res = solved(ctx)
    assert of(res, CONTRACTOR_GAP) == []
    # Without the penalty the gap pattern wins, with one late start only.
    assert len(of(res, CLASSE_LATE_START)) == 1


def test_s4_early_exit_for_another_teacher_still_penalises():
    ctx = make(
        [A(1, "T", "A"), A(2, "T", "B")],
        {"A": day_cap(MON, 1), "B": day_cap(MON, 1)},
        types={"T": CONTRATTO},
        availability={"T": cells(MON, [0, 2])},
        derogations=[Derogation(AUTHORIZE_EARLY_EXIT, teacher_id="X")],
    )
    assert len(of(solved(ctx), CONTRACTOR_GAP)) == 1


def test_s4_paired_lesson_does_not_make_a_gap_with_itself():
    ctx = make(
        [A(1, "T", "A"), A(2, "T", "B")],
        {"A": day_cap(MON, 1), "B": day_cap(MON, 1)},
        types={"T": CONTRATTO},
        availability={"T": cells(MON, range(6))},
        pairings=[Pairing("A", "B", "S", "T")],
    )
    assert of(solved(ctx), CONTRACTOR_GAP) == []


def test_s4_weight_zero_removes_it():
    ctx = make(
        [A(1, "T", "A"), A(2, "T", "B")],
        {"A": day_cap(MON, 1), "B": day_cap(MON, 1)},
        types={"T": CONTRATTO},
        availability={"T": cells(MON, [0, 2])},
    )
    assert of(solved(ctx, {CONTRACTOR_GAP: 0}), CONTRACTOR_GAP) == []


# ---------------------------------------------------------------- S5


def single_day_ctx(**kw):
    # T teaches A and B; B has no lessons, so T spends Monday with A only.
    return make(
        [A(1, "T", "A"), A(2, "T", "B", 0)],
        {"A": day_cap(MON, 3), "B": day_cap(MON, 0)},
        **kw,
    )


def test_s5_single_class_teacher_is_never_penalised():
    res = solved(make([A(1, "T", "A")], {"A": day_cap(MON, 3)}))
    assert of(res, SINGLE_CLASSE_DAY) == []


def test_s5_two_classes_three_hours_with_one_is_penalised():
    res = solved(single_day_ctx())
    (v,) = of(res, SINGLE_CLASSE_DAY)
    assert (v.class_id, v.teacher_id, v.day, v.weight) == ("A", "T", MON, 10)


def test_s5_two_hours_with_one_class_is_fine():
    ctx = make(
        [A(1, "T", "A"), A(2, "T", "B", 0)],
        {"A": day_cap(MON, 2), "B": day_cap(MON, 0)},
    )
    assert of(solved(ctx), SINGLE_CLASSE_DAY) == []


def test_s5_avoided_when_the_day_can_be_shared():
    ctx = make(
        [A(1, "T", "A"), A(2, "U", "A"), A(3, "T", "B"), A(4, "V", "B")],
        {"A": day_cap(MON, 3), "B": day_cap(MON, 3)},
    )
    res = solved(ctx)
    assert of(res, SINGLE_CLASSE_DAY) == []
    assert hours_of(res, 1) and hours_of(res, 3)  # T works with both classes


def test_s5_authorize_single_classe_day_removes_it():
    ctx = single_day_ctx(derogations=[Derogation(AUTHORIZE_SINGLE_CLASSE_DAY)])
    assert of(solved(ctx), SINGLE_CLASSE_DAY) == []


def test_s5_derogation_for_another_scope_still_penalises():
    ctx = single_day_ctx(
        derogations=[
            Derogation(AUTHORIZE_SINGLE_CLASSE_DAY, teacher_id="other"),
            Derogation(AUTHORIZE_SINGLE_CLASSE_DAY, class_id="B"),
            Derogation(AUTHORIZE_SINGLE_CLASSE_DAY, day=THU),
        ]
    )
    assert len(of(solved(ctx), SINGLE_CLASSE_DAY)) == 1


def test_s5_weight_zero_removes_it():
    res = solved(single_day_ctx(), {SINGLE_CLASSE_DAY: 0})
    assert of(res, SINGLE_CLASSE_DAY) == []


# ------------------------------------------------- scarcity multiplier (9.5)


def contractor_ctx(hours=0, grid=True, **kw):
    availability = None
    if grid:
        all_cells = sorted((d, h) for d in range(5) for h in range(6))
        availability = {"T": frozenset(all_cells[:hours])}
    return ScheduleContext(
        teacher_types={"T": CONTRATTO}, availability=availability, **kw
    )


def weights_for(ctx, teacher="T"):
    s3 = DEFAULT_SOFT_WEIGHTS[ORE_TARGET_DEVIATION] + EXACT_TARGET_WEIGHT
    return (
        scaled_weight(DEFAULT_SOFT_WEIGHTS[CONTRACTOR_GAP], ctx, teacher),
        scaled_weight(s3, ctx, teacher),
    )


def test_scarcity_full_availability_halves_the_weights():
    ctx = contractor_ctx(30)
    assert scarcity_multiplier(ctx, "T") == 0.5
    assert weights_for(ctx) == (75, 258)


def test_scarcity_no_available_hours_gives_one_and_a_half():
    ctx = contractor_ctx(0)
    assert scarcity_multiplier(ctx, "T") == 1.5
    assert weights_for(ctx) == (225, 772)


def test_scarcity_no_grid_recorded_gives_one_and_a_half():
    contexts = (
        contractor_ctx(grid=False),
        ScheduleContext(teacher_types={"T": CONTRATTO}, availability={}),
    )
    for ctx in contexts:
        assert scarcity_multiplier(ctx, "T") == 1.5
        assert weights_for(ctx) == (225, 772)


def test_scarcity_intermediate_hours_are_linear():
    assert scarcity_multiplier(contractor_ctx(15), "T") == 1.0
    assert weights_for(contractor_ctx(15)) == (150, 515)


def test_scarcity_override_availability_gives_one():
    contexts = (
        contractor_ctx(
            0, derogations=[Derogation(OVERRIDE_AVAILABILITY, teacher_id="T")]
        ),
        contractor_ctx(grid=False, derogations=[Derogation(OVERRIDE_AVAILABILITY)]),
    )
    for ctx in contexts:
        assert scarcity_multiplier(ctx, "T") == 1.0
        assert weights_for(ctx) == (150, 515)


def test_scarcity_override_for_another_teacher_does_not_apply():
    ctx = contractor_ctx(
        0, derogations=[Derogation(OVERRIDE_AVAILABILITY, teacher_id="X")]
    )
    assert scarcity_multiplier(ctx, "T") == 1.5


def test_scarcity_non_contractor_gives_one():
    ctx = ScheduleContext(teacher_types={"T": ASSUNTO}, availability={"T": frozenset()})
    assert scarcity_multiplier(ctx, "T") == 1.0
    assert weights_for(ctx) == (150, 515)
    assert scarcity_multiplier(ScheduleContext(), "unknown") == 1.0


def test_scaled_weight_is_never_below_one():
    ctx = contractor_ctx(30)
    assert scaled_weight(1, ctx, "T") == 1
    assert scaled_weight(0.4, ctx, "T") == 1


def test_scarcity_override_is_used_by_the_solver_for_s4():
    ctx = make(
        [A(1, "T", "A"), A(2, "T", "B")],
        {"A": day_cap(MON, 1), "B": day_cap(MON, 1)},
        types={"T": CONTRATTO},
        availability={"T": cells(MON, [0, 2])},
        derogations=[Derogation(OVERRIDE_AVAILABILITY, teacher_id="T")],
    )
    res = solved(ctx)
    # Under the override the contractor is free everywhere and m = 1.0, so a
    # gap (150) is dearer than the extra late start (100): none is left.
    assert of(res, CONTRACTOR_GAP) == []


def test_scarcity_scales_the_s3_weight_the_solver_uses():
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 1)},
        types={"T": CONTRATTO},
        availability={"T": cells(MON, range(6))},
        targets={1: 2},
    )
    (v,) = of(solved(ctx), ORE_TARGET_DEVIATION)
    assert v.weight == scaled_weight(515, ctx, "T")
    assert v.weight != 515


# ---------------------------------------------------------------- weight map


@pytest.mark.parametrize(
    "bad",
    [
        {CLASSE_LATE_START: MAX_SOFT_WEIGHT + 1},
        {CLASSE_LATE_START: -1},
        {"unknown_key": 5},
        {CLASSE_LATE_START: "100"},
        {CLASSE_LATE_START: None},
        {CLASSE_LATE_START: True},
        {CLASSE_LATE_START: math.nan},
        {CLASSE_LATE_START: math.inf},
        {"classe_unfilled": 100},
    ],
)
def test_weight_map_rejects_bad_input_before_solving(bad):
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 2)})
    with pytest.raises(ValueError, match="soft weight"):
        solve(ctx, bad)


def test_weight_map_error_names_the_key():
    with pytest.raises(ValueError, match="nope"):
        solve(ScheduleContext(), {"nope": 1})


def test_weight_map_accepts_the_limits():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 2)})
    solved(ctx, {k: 0 for k in DEFAULT_SOFT_WEIGHTS})
    solved(ctx, {k: MAX_SOFT_WEIGHT for k in DEFAULT_SOFT_WEIGHTS})


def test_empty_weight_map_equals_the_defaults():
    ctx = make(
        [A(1, "T", "A"), A(2, "T", "B")],
        {"A": day_cap(MON, 2), "B": day_cap(MON, 2)},
    )
    assert solve(ctx, {}) == solve(ctx) == solve(ctx, dict(DEFAULT_SOFT_WEIGHTS))


def test_missing_keys_use_the_default():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 2)}, class_min_start={"A": 10})
    res = solved(ctx, {SINGLE_CLASSE_DAY: 50})
    assert of(res, CLASSE_LATE_START)[0].weight == 100


# ------------------------------------------------------------ priority order


def test_priority_constants_are_ordered():
    assert FIXED_PENALTY_WEIGHT == 1500
    assert RELAX_WEIGHT == 1000
    assert EXACT_TARGET_WEIGHT == 500
    assert MAX_SOFT_WEIGHT == 200
    s3_default = EXACT_TARGET_WEIGHT + DEFAULT_SOFT_WEIGHTS[ORE_TARGET_DEVIATION]
    assert FIXED_PENALTY_WEIGHT > RELAX_WEIGHT > s3_default > MAX_SOFT_WEIGHT
    assert all(0 <= w <= MAX_SOFT_WEIGHT for w in DEFAULT_SOFT_WEIGHTS.values())
    assert set(DEFAULT_SOFT_WEIGHTS) == {
        "classe_late_start",
        "teoria_consecutive",
        "ore_target_deviation",
        "contractor_gap",
        "single_classe_day",
    }


MAX_WEIGHTS = {k: MAX_SOFT_WEIGHT for k in DEFAULT_SOFT_WEIGHTS}


def test_priority_exact_target_beats_every_preference_at_max_weight():
    # T reaches its target of 3 hours only by spending the day with class A
    # (S5) and teaching 3 consecutive theory hours (S2): 400 < 515.
    ctx = make(
        [A(1, "T", "A"), A(2, "U", "A"), A(3, "T", "B", 0)],
        {"A": day_cap(MON, 3), "B": day_cap(MON, 0)},
        targets={1: 3},
    )
    res = solved(ctx, MAX_WEIGHTS)
    assert len(hours_of(res, 1)) == 3
    assert of(res, ORE_TARGET_DEVIATION) == []
    prefs = res.violations
    assert {v.kind for v in prefs} == {SINGLE_CLASSE_DAY, TEORIA_CONSECUTIVE}
    # In this case all the preferences together stay below the 500+ tier.
    assert sum(v.weight * v.value for v in prefs) == 400
    assert sum(v.weight * v.value for v in prefs) < (
        EXACT_TARGET_WEIGHT + DEFAULT_SOFT_WEIGHTS[ORE_TARGET_DEVIATION]
    )


def test_priority_500_deviation_beats_a_1000_quasi_hard_violation():
    # Reaching T's target of 5 hours with class A needs a fifth hour in the
    # day (H11, 1000); one hour short of target costs only 515.
    ctx = make(
        [A(1, "T", "A"), A(2, "U", "A"), A(3, "T", "B", 0)],
        {"A": day_cap(MON, 6), "B": day_cap(MON, 0)},
        targets={1: 5},
    )
    res = solved(ctx)
    assert res.conflicts == []
    (dev,) = of(res, ORE_TARGET_DEVIATION)
    assert dev.value == 1 and dev.weight == 515
    assert len(hours_of(res, 1)) == 4

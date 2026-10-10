"""Fallback chain, forced mode and conflict list of the solver (specs 9.7, 9.8)."""

import re

import pytest

from scheduling.solver import (
    ASSUNTO,
    CLASSE_DOUBLE_BOOKING,
    CLASSE_EARLY_START,
    CLASSE_OVER_CAPACITY,
    CLASSE_STAGE_DAY,
    CLASSE_UNFILLED,
    CONTRATTO,
    DOCENTE_DOUBLE_BOOKING,
    FIXED_PENALTY_WEIGHT,
    MONTE_ORE_EXCEEDED,
    NO_COMPLETE_MESSAGE,
    ORE_TARGET_DEVIATION,
    RELAX_WEIGHT,
    Assignment,
    Lesson,
    Pairing,
    Penalty,
    ScheduleContext,
    SolveStatus,
    solve,
)
from scheduling.solver import model as solver_model
from scheduling.solver.conflicts import build_conflict_items

MON = 0
SOLVED = (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)


def day_cap(day, cap):
    return {d: (cap if d == day else 0) for d in range(5)}


def A(id, teacher, klass, residual=99, subject="S"):
    return Assignment(id, teacher, klass, subject, residual)


def make(assignments, capacity, types=None, **kw):
    return ScheduleContext(
        assignments=assignments,
        teacher_types=types or {},
        class_capacity=capacity,
        timeout_seconds=20,
        **kw,
    )


def items(res, kind):
    return [i for i in res.conflict_items if i["kind"] == kind]


def unfilled_week():
    """Class A needs 6 hours on Monday, its only teacher can give 4."""
    return make([A(1, "T", "A", 4)], {"A": day_cap(MON, 6)})


# ----------------------------------------------------------- rigid and fallback


def test_feasible_week_is_solved_rigidly_without_message():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 3)})
    res = solve(ctx)
    assert res.status in SOLVED
    assert res.message == ""
    assert len(res.lessons) == 3
    assert items(res, CLASSE_UNFILLED) == []


def test_infeasible_week_falls_back_to_best_effort():
    ctx = unfilled_week()
    assert solve(ctx, fallback=False).status == SolveStatus.INFEASIBLE  # rigid fails
    res = solve(ctx)  # ... and the chain recovers
    assert res.status in SOLVED
    assert res.message == NO_COMPLETE_MESSAGE
    assert res.message.startswith("Nessun orario completo possibile")
    assert len(res.lessons) == 4
    assert res.slots == res.lessons
    assert len(items(res, CLASSE_UNFILLED)) == 2


def test_one_unfilled_conflict_per_uncovered_hour_with_weight_1500():
    res = solve(unfilled_week())
    holes = items(res, CLASSE_UNFILLED)
    taught = {le.hour for le in res.lessons}
    assert sorted(i["ore_slot"] for i in holes) == sorted(set(range(6)) - taught)
    assert all(i["classe_id"] == "A" and i["giorno"] == "LUNEDI" for i in holes)
    unfilled = [v for v in res.violations if v.kind == CLASSE_UNFILLED]
    assert [v.weight for v in unfilled] == [FIXED_PENALTY_WEIGHT] * 2
    assert FIXED_PENALTY_WEIGHT == 1500
    assert res.n_soft_conflicts >= 2 and res.n_penalties >= 2


def test_message_is_absent_when_best_effort_finds_nothing_uncovered():
    # A week the rigid model solves never carries the message.
    res = solve(make([A(1, "T", "A", 3)], {"A": day_cap(MON, 3)}))
    assert res.message == ""


def test_best_effort_never_empties_a_class_for_a_preference():
    # Target 2 hours for T, but the class has 4 hours and T can give 3. Leaving
    # the class with only 2 lessons would hit the target (cost 0 + two uncovered
    # hours); a third lesson costs one hour of deviation (515) but covers one
    # more hour (1500).
    ctx = make(
        [A(1, "T", "A", 3)],
        {"A": day_cap(MON, 4)},
        targets={1: 2},
    )
    res = solve(ctx)
    assert res.status in SOLVED
    assert len(res.lessons) == 3
    assert len(items(res, CLASSE_UNFILLED)) == 1
    (dev,) = items(res, ORE_TARGET_DEVIATION)
    assert dev["ore"] == 1


def test_no_solution_gives_score_0_level_c_empty_slots_and_no_exception(monkeypatch):
    monkeypatch.setattr(solver_model, "_STATUS", {})  # every status -> UNKNOWN
    res = solve(unfilled_week())
    assert res.status == SolveStatus.UNKNOWN
    assert res.lessons == [] and res.slots == []
    assert (res.quality_score, res.quality_level) == (0.0, "C")
    assert res.conflicts == [] and res.conflict_items == []
    assert res.n_soft_conflicts == 0


def test_tiny_time_limit_never_raises_and_keeps_the_result_shape():
    assignments = [
        A(i, f"T{i % 9}", f"C{i % 12}", 40, subject=str(i)) for i in range(90)
    ]
    ctx = ScheduleContext(
        assignments=assignments,
        teacher_types={f"T{i}": CONTRATTO for i in range(9)},
        timeout_seconds=0.0001,
    )
    res = solve(ctx)
    assert isinstance(res.conflict_items, list)
    if res.status not in SOLVED:
        assert res.lessons == []
        assert (res.quality_score, res.quality_level) == (0.0, "C")


# ------------------------------------------------------------------ forced mode


def forced(ctx, *cells):
    pinned = [Lesson(i, d, h) for i, d, h in cells]
    res = solve(ctx, pinned=pinned)
    assert res.status in SOLVED
    assert set(pinned) <= set(res.lessons)  # pinned lessons stay unchanged
    return res


def test_forced_over_capacity_is_a_conflict():
    ctx = make(
        [A(1, "T1", "A"), A(2, "T2", "A"), A(3, "T3", "A")], {"A": day_cap(MON, 2)}
    )
    res = forced(ctx, (1, MON, 0), (2, MON, 1), (3, MON, 2))
    (c,) = res.conflict_items
    assert c["kind"] == CLASSE_OVER_CAPACITY
    assert (c["classe_id"], c["giorno"], c["ore_slot"]) == ("A", "LUNEDI", 2)
    assert c["weight"] == FIXED_PENALTY_WEIGHT


def test_forced_teacher_double_booking_is_a_conflict():
    ctx = make(
        [A(1, "T", "A"), A(2, "T", "B")], {"A": day_cap(MON, 1), "B": day_cap(MON, 1)}
    )
    res = forced(ctx, (1, MON, 0), (2, MON, 0))
    (c,) = res.conflict_items
    assert c["kind"] == DOCENTE_DOUBLE_BOOKING
    assert (c["docente_id"], c["giorno"], c["ore_slot"]) == ("T", "LUNEDI", 0)
    assert c["weight"] == FIXED_PENALTY_WEIGHT


def test_forced_class_double_booking_is_a_conflict():
    ctx = make([A(1, "T1", "A"), A(2, "T2", "A")], {"A": day_cap(MON, 2)})
    res = forced(ctx, (1, MON, 0), (2, MON, 0))
    kinds = {i["kind"] for i in res.conflict_items}
    assert CLASSE_DOUBLE_BOOKING in kinds
    (c,) = items(res, CLASSE_DOUBLE_BOOKING)
    assert (c["classe_id"], c["ore_slot"]) == ("A", 0)
    assert c["weight"] == FIXED_PENALTY_WEIGHT


def test_forced_hours_budget_exceeded_is_a_conflict():
    ctx = make([A(1, "T", "A", 1)], {"A": day_cap(MON, 2)}, {"T": ASSUNTO})
    res = forced(ctx, (1, MON, 0), (1, MON, 1))
    (c,) = res.conflict_items
    assert c["kind"] == MONTE_ORE_EXCEEDED
    assert (c["docente_id"], c["ore_slot"]) == ("T", 1)
    assert c["weight"] == FIXED_PENALTY_WEIGHT
    assert [le for le in res.lessons if le.assignment_id == 1] == [
        Lesson(1, MON, 0),
        Lesson(1, MON, 1),
    ]


def test_forced_budget_is_not_applied_to_contractors():
    ctx = make(
        [A(1, "T", "A", 1)],
        {"A": day_cap(MON, 2)},
        {"T": CONTRATTO},
        availability={"T": frozenset((MON, h) for h in range(6))},
    )
    res = forced(ctx, (1, MON, 0), (1, MON, 1))
    assert items(res, MONTE_ORE_EXCEEDED) == []


def test_forced_start_before_minimum_is_a_conflict():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 2)}, class_min_start={"A": 10})
    res = forced(ctx, (1, MON, 0))
    (c,) = items(res, CLASSE_EARLY_START)
    assert (c["classe_id"], c["ore_slot"], c["weight"]) == ("A", 0, 1500)


def test_forced_lesson_on_a_stage_day_is_a_conflict():
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 2)},
        stage_days={"A": frozenset({MON})},
    )
    res = forced(ctx, (1, MON, 0))
    (c,) = items(res, CLASSE_STAGE_DAY)
    assert (c["classe_id"], c["giorno"], c["ore_slot"]) == ("A", "LUNEDI", 0)
    assert c["weight"] == FIXED_PENALTY_WEIGHT


def test_forced_unavailable_hour_is_reported_at_the_override_weight():
    free = frozenset((MON, h) for h in range(1, 6))  # not available at 08:00
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 1)},
        {"T": CONTRATTO},
        availability={"T": free},
    )
    res = forced(ctx, (1, MON, 0))
    (c,) = items(res, "H4")
    assert c["weight"] == RELAX_WEIGHT == 1000
    assert c["suggested_action"]["action_type"] == "override_availability"
    (q,) = res.conflicts
    assert (q.kind, q.penalty, q.hour) == ("H4", RELAX_WEIGHT, 0)


def test_plain_generation_never_places_an_unavailable_hour():
    free = frozenset((MON, h) for h in range(1, 6))
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 1)},
        {"T": CONTRATTO},
        availability={"T": free},
    )
    res = solve(ctx)
    assert res.status in SOLVED
    assert [le.hour for le in res.lessons] != [0]
    assert items(res, "H4") == []


def test_forced_mode_solves_the_rest_around_the_pinned_lessons():
    ctx = make(
        [A(1, "T1", "A"), A(2, "T2", "A"), A(3, "T3", "B")],
        {"A": day_cap(MON, 3), "B": day_cap(MON, 3)},
    )
    res = forced(ctx, (1, MON, 1))
    assert res.conflict_items == [] or all(
        i["kind"] != CLASSE_UNFILLED for i in res.conflict_items
    )
    by_class = {"A": 0, "B": 0}
    names = {1: "A", 2: "A", 3: "B"}
    for le in res.lessons:
        by_class[names[le.assignment_id]] += 1
    assert by_class == {"A": 3, "B": 3}
    assert res.message == ""


def test_forced_mode_with_no_pinned_lessons_equals_normal_solving():
    for ctx in (make([A(1, "T", "A")], {"A": day_cap(MON, 3)}), unfilled_week()):
        plain = solve(ctx)
        assert solve(ctx, pinned=[]) == plain
        assert solve(ctx, pinned=None) == plain
        assert solve(ctx, pinned=[]).message == plain.message


def test_forced_unknown_pinned_assignment_is_rejected():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 1)})
    with pytest.raises(ValueError):
        solve(ctx, pinned=[Lesson(99, MON, 0)])


# --------------------------------------------------------------- conflict list


def sample_conflicts():
    ctx = make(
        [A(1, "T", "A", 1)], {"A": day_cap(MON, 2)}, {"T": ASSUNTO}, targets={1: 1}
    )
    return solve(ctx, pinned=[Lesson(1, MON, 0), Lesson(1, MON, 1)])


def test_conflict_shape():
    res = sample_conflicts()
    assert res.conflict_items
    for c in res.conflict_items:
        assert {
            "conflict_id",
            "chiave",
            "description",
            "suggested_action",
            "classe_id",
            "docente_id",
            "giorno",
        } <= set(c)
        assert set(c["suggested_action"]) == {"action_type", "label"}
        assert c["suggested_action"]["action_type"] and c["suggested_action"]["label"]
        assert c["description"]
    (c,) = items(res, MONTE_ORE_EXCEEDED)
    assert c["ore_slot"] == 1 and "ore" not in c


def test_unfilled_without_a_slot_reports_the_hours():
    # The class can only start at 13:00: a 2-hour day cannot fit, both hours
    # are uncovered and no single slot names them.
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 2)}, class_min_start={"A": 13})
    res = solve(ctx)
    (c,) = items(res, CLASSE_UNFILLED)
    assert "ore_slot" not in c
    assert c["ore"] == 1 and c["chiave"] == "classe_unfilled|A||LUNEDI"


def test_chiave_format_and_stability_while_conflict_id_changes():
    pattern = re.compile(r"^[a-z_0-9A-Z]+\|[^|]*\|[^|]*\|[A-Z]+(\|\d)?$")
    first, second = sample_conflicts(), sample_conflicts()
    assert [c["chiave"] for c in first.conflict_items] == [
        c["chiave"] for c in second.conflict_items
    ]
    for c in first.conflict_items:
        assert pattern.match(c["chiave"]), c["chiave"]
    (c,) = items(first, MONTE_ORE_EXCEEDED)
    assert c["chiave"] == "monte_ore_exceeded|A|T|LUNEDI|1"
    ids_a = {c["conflict_id"] for c in first.conflict_items}
    ids_b = {c["conflict_id"] for c in second.conflict_items}
    assert ids_a.isdisjoint(ids_b)


def test_chiave_with_a_teacher_but_no_hour():
    pen = Penalty("H11", "A", "T", 2, RELAX_WEIGHT)
    (c,) = build_conflict_items([pen], [], ScheduleContext(), [])
    assert c["chiave"] == "H11|A|T|MERCOLEDI"
    assert c["giorno"] == "MERCOLEDI"


def test_key_does_not_change_when_an_unrelated_lesson_moves():
    def keys(b_hour):
        ctx = make(
            [A(1, "T1", "A"), A(2, "T2", "A"), A(3, "T3", "A"), A(4, "U", "B")],
            {"A": day_cap(MON, 2), "B": day_cap(MON, 2)},
        )
        res = solve(
            ctx,
            pinned=[
                Lesson(1, MON, 0),
                Lesson(2, MON, 1),
                Lesson(3, MON, 2),
                Lesson(4, MON, b_hour),
            ],
        )
        return {c["chiave"] for c in res.conflict_items if c["classe_id"] == "A"}, res

    before, _ = keys(0)
    after, _ = keys(1)
    assert before == after == {"classe_over_capacity|A|T3|LUNEDI|2"}


def test_conflicts_are_deduplicated_by_kind_class_teacher_day_hour():
    pens = [
        Penalty(ORE_TARGET_DEVIATION, "A", "T", None, 515, 1, None, 1),
        Penalty(ORE_TARGET_DEVIATION, "A", "T", None, 515, 2, None, 2),
        Penalty(ORE_TARGET_DEVIATION, "A", "U", None, 515, 1, None, 3),
    ]
    ctx = ScheduleContext(
        assignments=[A(1, "T", "A"), A(2, "T", "A"), A(3, "U", "A")],
        targets={1: 1, 2: 1, 3: 1},
    )
    out = build_conflict_items(pens, [], ctx, [])
    assert len(out) == 2
    merged = next(c for c in out if c["docente_id"] == "T")
    assert merged["ore"] == 3


def test_distinct_hours_are_not_merged():
    pens = [
        Penalty("classe_unfilled", "A", None, 0, 1500, 1, 2),
        Penalty("classe_unfilled", "A", None, 0, 1500, 1, 3),
    ]
    out = build_conflict_items(pens, [], ScheduleContext(), [])
    assert sorted(c["ore_slot"] for c in out) == [2, 3]


def deviation_ctx(kind, residual, target, cap=4):
    ctx = make(
        [A(1, "T", "A", residual)],
        {"A": day_cap(MON, cap)},
        {"T": kind},
        availability={"T": frozenset((MON, h) for h in range(6))},
        targets={1: target},
        target_sources={1: "residuo 8 ore su 2 settimane"},
    )
    return solve(ctx)


def test_target_deviation_description_and_reduce_suggestion_for_contractor_above():
    res = deviation_ctx(CONTRATTO, 99, 2)
    (c,) = items(res, ORE_TARGET_DEVIATION)
    for part in ("4 ore assegnate", "target 2", "scarto +2", "residuo 8 ore su 2"):
        assert part in c["description"]
    assert c["ore"] == 2
    assert c["suggested_action"]["action_type"] == "reduce_contract_hours"


def test_reduce_hours_is_not_suggested_for_an_employee_above_target():
    res = deviation_ctx(ASSUNTO, 99, 2)
    (c,) = items(res, ORE_TARGET_DEVIATION)
    assert "scarto +2" in c["description"]
    assert c["suggested_action"]["action_type"] != "reduce_contract_hours"


def test_reduce_hours_is_not_suggested_for_a_contractor_below_target():
    res = deviation_ctx(CONTRATTO, 99, 6)
    (c,) = items(res, ORE_TARGET_DEVIATION)
    assert "scarto -2" in c["description"]
    assert c["suggested_action"]["action_type"] != "reduce_contract_hours"


def test_conflict_free_timetable_returns_an_empty_list():
    ctx = make([A(1, "T1", "A"), A(2, "T2", "A")], {"A": day_cap(MON, 4)})
    res = solve(ctx)
    assert res.conflict_items == [] and isinstance(res.conflict_items, list)
    assert res.conflicts == []
    # Also for a result with no solution at all.
    assert solve(ScheduleContext()).conflict_items == []


def test_soft_violations_are_listed_too():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 3)}, class_min_start={"A": 10})
    res = solve(ctx)
    assert items(res, "classe_late_start")
    assert res.conflicts == []  # `conflicts` keeps only the quasi-hard overrides


def test_best_effort_counts_every_hole_as_a_penalty_term_for_the_score():
    covered = solve(make([A(1, "T", "A", 6)], {"A": day_cap(MON, 6)}))
    best_effort = solve(unfilled_week())
    # Six hour-slots of the day are penalty terms, two of them violated.
    assert best_effort.n_penalties == covered.n_penalties + 6
    assert len(best_effort.violations) == 2
    assert best_effort.quality_score == (best_effort.n_penalties - 2) * 100 / (
        best_effort.n_penalties
    )


def test_forced_lesson_on_an_excluded_day_is_kept_not_an_error():
    ctx = make(
        [A(1, "T", "A")],
        {"A": day_cap(MON, 1)},
        excluded_days={"T": frozenset({MON})},
    )
    res = forced(ctx, (1, MON, 0))
    assert res.conflict_items == []


def test_forced_lesson_over_the_weekly_cap_is_kept_not_an_error():
    ctx = make([A(1, "T", "A")], {"A": day_cap(MON, 1)}, weekly_caps={"T": 0})
    res = forced(ctx, (1, MON, 0))
    assert res.conflict_items == []


def test_forced_paired_lesson_pins_both_classes():
    ctx = make(
        [A(1, "T", "A"), A(2, "T", "B")],
        {"A": day_cap(MON, 1), "B": day_cap(MON, 1)},
        pairings=[Pairing("A", "B", "S", "T")],
    )
    res = forced(ctx, (1, MON, 3))  # only the first class's lesson is pinned
    assert Lesson(2, MON, 3) in res.lessons
    # Only the late start of the pinned hour is reported, nothing structural.
    assert {i["kind"] for i in res.conflict_items} == {"classe_late_start"}

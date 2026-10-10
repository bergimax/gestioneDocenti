"""Explainable infeasibility (architecture 7.3, issue 32): one test per cause."""

import dataclasses
import re

import pytest

from scheduling.solver import (
    CONTRATTO,
    NO_COMPLETE_MESSAGE,
    Assignment,
    Pairing,
    ScheduleContext,
    SolveStatus,
    solve,
)
from scheduling.solver import explain as explain_module
from scheduling.solver import model as solver_model

SOLVED = (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
MON, TUE = 0, 1

# Ids are numbers nobody would write in a sentence, so a leak is easy to see.
A, B = 7101, 7102
T, U = 8801, 8802
NAMES = {
    "class_names": {A: "1A", B: "1B"},
    "teacher_names": {T: "ROSSI MARIO", U: "BIANCHI LUCA"},
}


def caps(**by_day):
    """Class capacity from `mon=6`, `tue=3`; the other days are closed."""
    names = ["mon", "tue", "wed", "thu", "fri"]
    return {i: by_day.get(n, 0) for i, n in enumerate(names)}


def asg(id, teacher, klass, residual=99):
    return Assignment(id, teacher, klass, "S", residual)


def make(assignments, capacity, **kw):
    return ScheduleContext(
        assignments=assignments,
        class_capacity=capacity,
        timeout_seconds=20,
        **{**NAMES, **kw},
    )


def free(day, *hours):
    return frozenset((day, h) for h in hours)


def explained(ctx):
    res = solve(ctx)
    assert res.explanation_unavailable is False
    return res


def only(res):
    assert len(res.explanations) == 1, res.explanations
    return res.explanations[0]


# ---- the six causes of the issue -------------------------------------------


def test_a_class_needs_six_hours_teachers_cover_four():
    ctx = make(
        [asg(1, T, A), asg(2, U, A)],
        {A: caps(mon=6)},
        teacher_types={T: CONTRATTO, U: CONTRATTO},
        availability={T: free(MON, 0, 1), U: free(MON, 2, 3)},
    )
    res = explained(ctx)
    text = only(res)
    assert "1A" in text and "lunedì" in text
    assert "ROSSI MARIO" in text and "BIANCHI LUCA" in text
    assert "richiede 6 ore" in text
    assert "ne coprono solo 4" in text
    assert "disponibile solo 2 ore lunedì" in text
    assert "Mancano 2 ore" in text


def test_b_the_only_teacher_of_two_classes_in_the_same_hours():
    ctx = make([asg(1, T, A), asg(2, T, B)], {A: caps(mon=6), B: caps(mon=6)})
    text = only(explained(ctx))
    assert "ROSSI MARIO" in text and "lunedì" in text
    assert "classi 1A e 1B" in text
    assert "nelle stesse ore" in text
    assert "servono 12 ore ma ne può fare al massimo 6" in text
    assert "Mancano 6 ore" in text


def test_b_overlap_with_a_limited_contractor_uses_his_available_hours():
    ctx = make(
        [asg(1, T, A), asg(2, T, B)],
        {A: caps(mon=3), B: caps(mon=3)},
        teacher_types={T: CONTRATTO},
        availability={T: free(MON, 0, 1, 2, 3)},
    )
    res = explained(ctx)
    text = only(res)
    assert "servono 6 ore ma ne può fare al massimo 4" in text
    assert "Mancano 2 ore" in text
    assert [d["docente"] for d in res.suggested_deroghe] == [T]


def test_b_overlap_without_a_shortage_of_hours_just_says_same_hours():
    # Both classes start at 11:00 for 3 hours: 6 teacher-hours, but the same 3.
    ctx = make(
        [asg(1, T, A), asg(2, T, B)],
        {A: caps(mon=3), B: caps(mon=3)},
        class_min_start={A: 11, B: 11},
    )
    text = only(explained(ctx))
    assert text.endswith("nelle stesse ore.")
    assert "Mancano" not in text and "servono" not in text


def test_c_contractor_unavailable_on_the_only_day_of_the_class():
    ctx = make(
        [asg(1, T, A)],
        {A: caps(mon=3)},
        teacher_types={T: CONTRATTO},
        availability={T: free(TUE, 0, 1, 2, 3, 4, 5)},
    )
    res = explained(ctx)
    text = only(res)
    assert "1A" in text and "lunedì" in text and "ROSSI MARIO" in text
    assert "nessun docente può coprirle" in text
    assert "non disponibile lunedì" in text
    assert "Mancano 3 ore" in text
    assert res.suggested_deroghe == [
        {
            "action_type": "override_availability",
            "label": "Ignora la disponibilità",
            "classe": A,
            "docente": T,
            "giorno": "LUNEDI",
        }
    ]


def test_d_assunto_with_fewer_residual_hours_than_required():
    ctx = make([asg(1, T, A, residual=4)], {A: caps(mon=6)})
    res = explained(ctx)
    text = only(res)
    assert "1A" in text and "lunedì" in text and "ROSSI MARIO" in text
    assert "richiede 6 ore" in text
    assert "ha solo 4 ore residue" in text
    assert "Mancano 2 ore" in text
    assert res.suggested_deroghe == []


def test_d_residual_hours_are_shared_by_the_days_of_one_class():
    ctx = make([asg(1, T, A, residual=7)], {A: caps(mon=6, tue=6)})
    text = only(explained(ctx))
    assert "La classe 1A (lunedì e martedì) richiede in tutto 12 ore" in text
    assert "ne coprono solo 7" in text
    assert "Mancano 5 ore" in text


def test_e_stage_class_with_a_paired_lesson_forced_that_day():
    ctx = make(
        [asg(1, T, A), asg(2, T, B)],
        {A: caps(mon=3), B: caps(mon=3)},
        pairings=[Pairing(A, B, "S", T)],
        stage_days={A: frozenset({MON})},
    )
    res = explained(ctx)
    text = only(res)
    assert "1B" in text and "lunedì" in text and "ROSSI MARIO" in text
    assert "in coppia con la classe 1A" in text
    assert "1A è in stage" in text
    assert "Mancano 3 ore" in text
    assert res.suggested_deroghe == []


def test_e_closed_class_with_a_paired_lesson_forced_that_day():
    ctx = make(
        [asg(1, T, A), asg(2, T, B)],
        {A: caps(), B: caps(mon=3)},
        pairings=[Pairing(A, B, "S", T)],
    )
    text = only(explained(ctx))
    assert "1A è chiusa" in text
    assert "1A è in stage" not in text
    assert "1B" in text and "lunedì" in text and "Mancano 3 ore" in text


def test_f_paired_lesson_whose_teacher_is_unavailable():
    ctx = make(
        [asg(1, T, A), asg(2, T, B), asg(3, U, A)],
        {A: caps(mon=3), B: caps(mon=3)},
        teacher_types={T: CONTRATTO},
        pairings=[Pairing(A, B, "S", T)],
        availability={T: free(TUE, 0)},
    )
    res = explained(ctx)
    text = only(res)
    assert "1B" in text and "lunedì" in text and "ROSSI MARIO" in text
    assert "lezione in coppia con la classe 1A" in text
    assert "non disponibile lunedì" in text
    assert "Mancano 3 ore" in text
    assert [d["action_type"] for d in res.suggested_deroghe] == [
        "override_availability"
    ]


def test_f_one_paired_cause_reported_once_for_both_classes():
    ctx = make(
        [asg(1, T, A), asg(2, T, B)],
        {A: caps(mon=3), B: caps(mon=3)},
        teacher_types={T: CONTRATTO},
        pairings=[Pairing(A, B, "S", T)],
        availability={T: free(TUE, 0)},
    )
    res = explained(ctx)
    text = only(res)
    assert "1A" in text and "1B" in text
    assert len(res.suggested_deroghe) == 1


# ---- the other hard constraints --------------------------------------------


def test_h7_excluded_day():
    ctx = make([asg(1, T, A)], {A: caps(mon=3)}, excluded_days={T: frozenset({MON})})
    text = only(explained(ctx))
    assert "ROSSI MARIO: non insegna lunedì" in text
    assert "Mancano 3 ore" in text


def test_h8_weekly_cap_shared_by_two_classes():
    ctx = make(
        [asg(1, T, A), asg(2, T, B)],
        {A: caps(mon=3), B: caps(tue=3)},
        weekly_caps={T: 4},
    )
    text = only(explained(ctx))
    assert "Le classi 1A (lunedì) e 1B (martedì) richiedono in tutto 6 ore" in text
    assert "ne coprono solo 4" in text
    assert "ROSSI MARIO: massimo 4 ore a settimana" in text
    assert "Mancano 2 ore" in text


def test_a_class_without_teachers():
    ctx = make([asg(1, T, B)], {A: caps(mon=3), B: caps()})
    text = only(explained(ctx))
    assert "1A" in text and "nessun docente può coprirle" in text
    assert "nessun docente assegnato alla classe" in text
    assert "Mancano 3 ore" in text


def test_a_block_that_does_not_fit_after_the_start_hour():
    ctx = make([asg(1, T, A)], {A: caps(mon=3)}, class_min_start={A: 12})
    text = only(explained(ctx))
    assert "La classe 1A lunedì richiede 3 ore consecutive" in text
    assert "12:00" in text
    assert "non c'è spazio" in text


def test_paired_classes_with_different_day_lengths_get_a_generic_sentence():
    ctx = make(
        [asg(1, T, A), asg(2, T, B)],
        {A: caps(mon=6), B: caps(mon=3)},
        pairings=[Pairing(A, B, "S", T)],
    )
    text = only(explained(ctx))
    assert text.startswith("I vincoli di questa settimana non permettono")
    assert "1A e 1B" in text


# ---- several causes, feasible weeks, budget --------------------------------


def two_causes():
    return make(
        [asg(1, T, A, residual=4), asg(2, U, B, residual=2)],
        {A: caps(mon=6), B: caps(tue=6)},
    )


def test_two_causes_give_two_distinct_messages_without_duplicates():
    res = explained(two_causes())
    assert len(res.explanations) == 2
    assert len(set(res.explanations)) == 2
    first, second = res.explanations
    assert "1A" in first and "ROSSI MARIO" in first and "lunedì" in first
    assert "1B" in second and "BIANCHI LUCA" in second and "martedì" in second
    assert "1B" not in first and "1A" not in second


def test_a_feasible_week_never_extracts_a_core(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("core extraction on a feasible week")

    monkeypatch.setattr(solver_model, "explain_infeasibility", boom)
    monkeypatch.setattr(solver_model, "build_probe", boom)
    res = solve(make([asg(1, T, A)], {A: caps(mon=6)}))
    assert res.status in SOLVED and res.message == ""
    assert res.explanations == [] and res.suggested_deroghe == []
    assert res.explanation_unavailable is False


def test_infeasible_week_returns_best_effort_and_explanations():
    res = explained(make([asg(1, T, A, residual=4)], {A: caps(mon=6)}))
    assert res.status in SOLVED
    assert len(res.lessons) == 4
    assert res.message == NO_COMPLETE_MESSAGE
    assert res.explanations


def test_without_fallback_the_infeasible_result_is_explained_too():
    res = solve(make([asg(1, T, A, residual=4)], {A: caps(mon=6)}), fallback=False)
    assert res.status == SolveStatus.INFEASIBLE
    assert res.lessons == []
    assert "ha solo 4 ore residue" in only(res)


def test_zero_budget_gives_the_generic_message_and_the_flag():
    ctx = make([asg(1, T, A, residual=4)], {A: caps(mon=6)})
    res = solve(dataclasses.replace(ctx, explain_budget_seconds=0.0))
    assert res.status in SOLVED and len(res.lessons) == 4
    assert res.explanation_unavailable is True
    assert res.explanations == [] and res.suggested_deroghe == []
    assert res.message == NO_COMPLETE_MESSAGE


def test_budget_spent_during_extraction_drops_the_partial_explanation(monkeypatch):
    monkeypatch.setattr(explain_module, "_used", lambda solver: 1000.0)
    res = solve(two_causes())
    assert res.explanation_unavailable is True
    assert res.explanations == [] and res.suggested_deroghe == []
    assert res.message == NO_COMPLETE_MESSAGE


def test_solver_stopping_on_its_time_limit_is_unavailable(monkeypatch):
    from ortools.sat.python import cp_model

    monkeypatch.setattr(
        cp_model.CpSolver, "solve", lambda self, model: cp_model.UNKNOWN
    )
    monkeypatch.setattr(explain_module, "_used", lambda solver: 0.0)
    unavailable = explain_module.explain_infeasibility(two_causes())
    assert unavailable.unavailable is True and unavailable.messages == []


def test_a_week_the_probe_cannot_fail_is_unavailable(monkeypatch):
    monkeypatch.setattr(solver_model, "build_probe", lambda ctx: None)
    res = solve(make([asg(1, T, A, residual=4)], {A: caps(mon=6)}))
    assert res.explanation_unavailable is True and res.explanations == []


# ---- no internals in the text ----------------------------------------------

INTERNALS = re.compile(r"hc_|\b(cap|closed|hours|excl|busy|weekly|avail|pair)\b")


@pytest.mark.parametrize(
    "ctx",
    [
        make(
            [asg(1, T, A), asg(2, T, B)],
            {A: caps(mon=3), B: caps(mon=3)},
            teacher_types={T: CONTRATTO},
            pairings=[Pairing(A, B, "S", T)],
            availability={T: free(TUE, 0)},
        ),
        make([asg(1, T, A), asg(2, T, B)], {A: caps(mon=6), B: caps(mon=6)}),
        make(
            [asg(1, T, A), asg(2, T, B)],
            {A: caps(mon=3), B: caps(tue=3)},
            weekly_caps={T: 4},
        ),
        two_causes(),
    ],
)
def test_messages_have_no_ids_or_literal_names(ctx):
    res = explained(ctx)
    text = " ".join(res.explanations)
    assert text
    assert not INTERNALS.search(text)
    for internal in (A, B, T, U):
        assert str(internal) not in text

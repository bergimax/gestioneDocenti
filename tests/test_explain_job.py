"""Explainable infeasibility end to end: settings, job result, grid page."""

import datetime
from pathlib import Path
from unittest import mock

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, override_settings

from accounts.models import User
from config.env import get_explain_budget
from scheduling import board, tasks, views
from scheduling.models import JobStatus, SolveJob, Timetable
from scheduling.solver import NO_COMPLETE_MESSAGE
from school.models import (
    Assignment,
    CalendarEntry,
    Class,
    School,
    Subject,
    Teacher,
)

D = datetime.date
MON = D(2026, 10, 12)
GRID = f"/orario/{MON:%Y-%m-%d}/griglia/"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(views, "today", lambda: D(2026, 10, 14))


@pytest.fixture
def school(db):
    return School.objects.create(
        name="A",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2026, 12, 21),
    )


@pytest.fixture
def week(school):
    """Monday has 2 lessons and the rest is closed; ROSSI MARIO has 1 hour left."""
    CalendarEntry.objects.create(school=school, date=MON, max_hours=2)
    for i in range(1, 5):
        CalendarEntry.objects.create(
            school=school, date=MON + datetime.timedelta(days=i), is_closure=True
        )
    klass = Class.objects.create(school=school, name="1A")
    teacher = Teacher.objects.create(
        school=school, name="ROSSI MARIO", type="ASSUNTO", first_time=False
    )
    subject = Subject.objects.create(
        school=school, name="Mat", kind="TEORIA", cognitive_weight="MEDIO"
    )
    assignment = Assignment.objects.create(
        school=school,
        teacher=teacher,
        school_class=klass,
        subject=subject,
        ore_totali=1,
    )
    return school, assignment


def run(school):
    job = SolveJob.objects.create(school=school, week_start=MON)
    tasks.run_job(job.pk)
    job.refresh_from_db()
    return job


def login(role):
    user = User.objects.create_user(f"{role}@example.com", "pw-long-enough", role)
    client = Client()
    client.force_login(user)
    return client


# ---- the time budget setting -----------------------------------------------


def test_budget_defaults_to_ten_seconds():
    assert get_explain_budget({}, 60) == 10


def test_budget_comes_from_the_environment():
    assert get_explain_budget({"SOLVER_EXPLAIN_BUDGET_SECONDS": "25"}, 60) == 25


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "1.5"])
def test_bad_budget_fails(raw):
    with pytest.raises(ImproperlyConfigured, match="SOLVER_EXPLAIN_BUDGET_SECONDS"):
        get_explain_budget({"SOLVER_EXPLAIN_BUDGET_SECONDS": raw}, 60)


@pytest.mark.parametrize("raw", ["60", "61"])
def test_budget_must_stay_below_the_solver_timeout(raw):
    with pytest.raises(ImproperlyConfigured, match="below SOLVER_TIMEOUT_SECONDS"):
        get_explain_budget({"SOLVER_EXPLAIN_BUDGET_SECONDS": raw}, 60)


def test_default_budget_is_rejected_by_a_short_timeout():
    with pytest.raises(ImproperlyConfigured, match="SOLVER_EXPLAIN_BUDGET_SECONDS"):
        get_explain_budget({}, 10)


def test_budget_is_a_setting_below_the_timeout():
    assert settings.SOLVER_EXPLAIN_BUDGET_SECONDS == 10
    assert settings.SOLVER_EXPLAIN_BUDGET_SECONDS < settings.SOLVER_TIMEOUT_SECONDS


def test_budget_is_documented_and_supplied_to_tests():
    example = (ROOT / ".env.example").read_text()
    assert "SOLVER_EXPLAIN_BUDGET_SECONDS=10" in example
    assert (
        "SOLVER_EXPLAIN_BUDGET_SECONDS"
        in (ROOT / "config/settings_test.py").read_text()
    )


def test_the_job_gives_the_solver_the_configured_budget(week):
    school, _ = week
    seen = []

    def spy(ctx, pinned=None):
        seen.append(ctx.explain_budget_seconds)
        return tasks.solve(ctx)

    with override_settings(SOLVER_EXPLAIN_BUDGET_SECONDS=7):
        with mock.patch.object(tasks, "solve_exclusive", spy):
            run(school)
    assert seen == [7]


# ---- the job result --------------------------------------------------------


def test_infeasible_week_job_carries_the_explanation_and_the_best_effort(week):
    school, _ = week
    job = run(school)
    assert job.status == JobStatus.DONE
    result = job.result
    assert result["n_slots"] == 1
    assert result["message"] == NO_COMPLETE_MESSAGE
    assert result["explanation_unavailable"] is False
    [text] = result["explanations"]
    assert "1A" in text and "lunedì" in text and "ROSSI MARIO" in text
    assert "ha solo 1 ora residue" in text and "Mancano 1 ora" in text
    assert result["suggested_deroghe"] == []
    assert Timetable.objects.get().slots.count() == 1


def test_contractor_cause_lists_the_suggested_derogation(week):
    school, assignment = week
    Teacher.objects.filter(pk=assignment.teacher_id).update(type="CONTRATTO")
    job = run(school)
    assert job.status == JobStatus.DONE
    [suggestion] = job.result["suggested_deroghe"]
    assert suggestion == {
        "action_type": "override_availability",
        "label": "Ignora la disponibilità",
        "classe": assignment.school_class_id,
        "docente": assignment.teacher_id,
        "giorno": "LUNEDI",
    }
    assert "ROSSI MARIO" in job.result["explanations"][0]
    assert "non disponibile lunedì" in job.result["explanations"][0]


def test_feasible_week_job_has_no_explanations(week):
    school, assignment = week
    Assignment.objects.filter(pk=assignment.pk).update(ore_totali=20)
    result = run(school).result
    assert result["explanations"] == []
    assert result["suggested_deroghe"] == []
    assert result["explanation_unavailable"] is False
    assert result["message"] == ""


def test_exceeded_budget_completes_the_job_with_the_generic_message(week):
    school, _ = week
    with override_settings(SOLVER_EXPLAIN_BUDGET_SECONDS=0):
        job = run(school)
    assert job.status == JobStatus.DONE
    assert job.result["explanation_unavailable"] is True
    assert job.result["explanations"] == []
    assert job.result["suggested_deroghe"] == []
    assert job.result["message"] == NO_COMPLETE_MESSAGE
    assert Timetable.objects.get().slots.count() == 1


# ---- the grid page ----------------------------------------------------------


@pytest.mark.parametrize("role", ["ADMIN", "SEGRETERIA"])
def test_grid_lists_the_messages_above_the_grid(week, role):
    school, _ = week
    run(school)
    text = login(role).get(GRID).content.decode()
    section = text.index('class="tt-explanations"')
    assert section < text.index('class="tt-day"')
    items = text[section : text.index("</section>", section)]
    assert items.count("<li>") == 1
    assert "ROSSI MARIO" in items and "ha solo 1 ora residue" in items
    assert "<button" not in items and "<form" not in items


def test_grid_reads_the_stored_result_and_never_calls_the_solver(week):
    school, _ = week
    run(school)
    with (
        mock.patch.object(tasks, "solve", side_effect=AssertionError),
        mock.patch("scheduling.solver.model.solve", side_effect=AssertionError),
        mock.patch(
            "scheduling.solver.model.explain_infeasibility",
            side_effect=AssertionError,
        ),
    ):
        text = login("ADMIN").get(GRID).content.decode()
    assert "ha solo 1 ora residue" in text


def test_grid_of_a_feasible_week_shows_no_explanations(week):
    school, assignment = week
    Assignment.objects.filter(pk=assignment.pk).update(ore_totali=20)
    run(school)
    text = login("ADMIN").get(GRID).content.decode()
    assert "tt-explanations" not in text
    assert NO_COMPLETE_MESSAGE not in text


def test_grid_without_explanation_keeps_the_generic_best_effort_message(week):
    school, _ = week
    with override_settings(SOLVER_EXPLAIN_BUDGET_SECONDS=0):
        run(school)
    text = login("SEGRETERIA").get(GRID).content.decode()
    assert "tt-explanations" not in text
    assert "Nessun orario completo possibile" in text


def test_the_messages_of_another_timetable_are_not_shown(week):
    school, _ = week
    job = run(school)
    SolveJob.objects.filter(pk=job.pk).update(
        result={**job.result, "timetable_id": job.result["timetable_id"] + 1}
    )
    timetable = Timetable.objects.get()
    assert board.explanations(timetable) == []
    assert "tt-explanations" not in login("ADMIN").get(GRID).content.decode()

"""Alternatives and pairwise choice (#34): job, comparison, pick."""

import dataclasses
import datetime
import html as htmllib
import re
from unittest import mock

import pytest
from django.apps import apps
from django.core.exceptions import PermissionDenied
from django.template.defaultfilters import floatformat
from django.test import Client, override_settings

from accounts.models import User
from scheduling import alternatives, tasks
from scheduling.alternatives import (
    MSG_ALREADY_PICKED,
    MSG_NO_MEANINGFUL,
    MSG_STALE,
    active_soft_weights,
)
from scheduling.context import build_week_context
from scheduling.jobs import enqueue_generation
from scheduling.models import (
    AlternativePick,
    JobKind,
    JobStatus,
    Slot,
    SolveJob,
    Timetable,
    TimetableState,
)
from scheduling.services import apply_change
from scheduling.solver import (
    CLASSE_LATE_START,
    CONTRACTOR_GAP,
    DEFAULT_SOFT_WEIGHTS,
    TEORIA_CONSECUTIVE,
    Lesson,
    SolveStatus,
    weight_variations,
)
from scheduling.solver import alternatives as solver_alt
from scheduling.solver.context import Penalty
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
WEEK = f"{MON:%Y-%m-%d}"
GENERATE = f"/orario/{WEEK}/genera/"
PAGE = f"/orario/?settimana={WEEK}"
JOB_URL = f"/orario/{WEEK}/stato/"


@pytest.fixture
def rw(db):
    """3 classes x 5 teachers, 4 lessons a day Monday-Friday, a generated week."""

    class R:
        pass

    r = R()
    s = r.school = School.objects.create(
        name="S",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2026, 12, 21),
    )
    for i in range(5):
        CalendarEntry.objects.create(
            school=s, date=MON + datetime.timedelta(days=i), max_hours=4
        )
    r.classes = [Class.objects.create(school=s, name=n) for n in ("1A", "1B", "1C")]
    r.teachers = [
        Teacher.objects.create(
            school=s, name=f"DOC{i} NOME", type="ASSUNTO", first_time=False
        )
        for i in range(5)
    ]
    r.subjects = [
        Subject.objects.create(
            school=s, name=f"Mat{i}", kind="TEORIA", cognitive_weight="MEDIO"
        )
        for i in range(5)
    ]
    for c in r.classes:
        for t, sub in zip(r.teachers, r.subjects, strict=True):
            Assignment.objects.create(
                school=s, teacher=t, school_class=c, subject=sub, ore_totali=40
            )
    job = SolveJob.objects.create(school=s, week_start=MON)
    tasks.run_job(job.pk)
    r.tt = Timetable.objects.get(school=s, week_start=MON)
    ctx = build_week_context(MON, s)
    r.base = solver_alt.solve(dataclasses.replace(ctx, timeout_seconds=30))
    assert r.base.status == SolveStatus.OPTIMAL
    return r


@pytest.fixture
def admin(db):
    c = Client()
    c.user = User.objects.create_user("a@example.com", "pw-long-enough", "ADMIN")
    c.force_login(c.user)
    return c


@pytest.fixture
def seg(db):
    c = Client()
    c.force_login(
        User.objects.create_user("s@example.com", "pw-long-enough", "SEGRETERIA")
    )
    return c


def html(response):
    return htmllib.unescape(response.content.decode())


def late(n):
    return [Penalty(CLASSE_LATE_START, None, None, 0, 500)] * n


ITEM = {
    "kind": CLASSE_LATE_START,
    "chiave": "classe_late_start||SETTIMANA",
    "description": "Conflitto di prova",
    "suggested_action": {"action_type": "manual_edit", "label": "Modifica"},
    "classe_id": None,
    "docente_id": None,
    "giorno": None,
    "weight": 500,
    "conflict_id": "x",
}


def variant(r, i):
    """The i-th stand-in answer of the solver: different lessons and violations."""
    lessons = [Lesson(le.assignment_id, le.day, le.hour + i) for le in r.base.lessons]
    violations = [Penalty(TEORIA_CONSECUTIVE, None, None, 0, 50)] + late(i)
    if i == 2:
        violations.append(Penalty(CONTRACTOR_GAP, None, None, 0, 750))
    return dataclasses.replace(
        r.base,
        lessons=lessons,
        violations=violations,
        quality_score=100.0 - 5 * i,
        quality_level="A" if i == 0 else "B",
        conflict_items=[ITEM] if i else [],
    )


class Fake:
    """Stands in for `solve` inside the alternatives loop."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, ctx, weights=None, **kw):
        self.calls.append((ctx, weights))
        return self.answers[min(len(self.calls), len(self.answers)) - 1]


def distinct(r, n=3):
    return Fake([variant(r, i) for i in range(n)])


def start(client, alternatives_on=True, fake=None):
    """POST the generate form, run the job with `fake`, return the finished job."""
    data = {"alternative": "1"} if alternatives_on else {}
    response = client.post(GENERATE, data)
    assert response.status_code == 200, html(response)
    job = SolveJob.objects.order_by("-pk").first()
    with mock.patch.object(solver_alt, "solve", fake):
        tasks.run_job(job.pk)
    job.refresh_from_db()
    return job


def choose_url(job, index):
    return f"/orario/{WEEK}/alternative/{job.pk}/{index}/scegli/"


def view_url(job, index):
    return f"/orario/{WEEK}/alternative/{job.pk}/{index}/griglia/"


def week_cells(tt):
    return {
        (s.school_class_id, s.teacher_id, s.subject_id, s.day, s.start_hour)
        for s in Slot.objects.filter(timetable=tt)
    }


def state(r):
    r.tt.refresh_from_db()
    return r.tt.version, week_cells(r.tt), r.tt.quality_score, r.tt.conflicts


# ---- generating ----------------------------------------------------------------


def test_the_generate_form_has_the_option_for_an_admin(admin, rw):
    page = html(admin.get(PAGE))
    assert 'name="alternative"' in page and "Genera alternative" in page


def test_the_option_makes_an_alternatives_job(admin, rw):
    admin.post(GENERATE, {"alternative": "1"})
    job = SolveJob.objects.order_by("-pk").first()
    assert job.kind == JobKind.GENERATE and job.params == {"alternatives": True}


def test_without_the_option_it_is_an_ordinary_generation(admin, rw):
    admin.post(GENERATE, {})
    assert SolveJob.objects.order_by("-pk").first().params == {}


def test_an_ordinary_generation_still_rewrites_the_week(admin, rw):
    before = state(rw)[0]
    admin.post(GENERATE, {})
    tasks.run_job(SolveJob.objects.order_by("-pk").first().pk)
    assert state(rw)[0] == before + 1


def test_the_job_keeps_up_to_three_alternatives(admin, rw):
    job = start(admin, fake=distinct(rw))
    assert job.status == JobStatus.DONE
    alts = job.result["alternatives"]
    assert [a["index"] for a in alts] == [0, 1, 2]
    assert [a["label"] for a in alts] == [
        "Alternativa A",
        "Alternativa B",
        "Alternativa C",
    ]
    assert job.result["meaningful"] is True and job.result["picked"] is False


def test_two_distinct_alternatives_are_kept_too(admin, rw):
    job = start(admin, fake=distinct(rw, 2))
    assert len(job.result["alternatives"]) == 2 and job.result["meaningful"]


def test_each_alternative_is_solved_from_the_active_weights(admin, rw):
    base = {**DEFAULT_SOFT_WEIGHTS, CLASSE_LATE_START: 300, CONTRACTOR_GAP: 200}
    fake = distinct(rw)
    with mock.patch.object(alternatives, "active_soft_weights", return_value=base):
        job = start(admin, fake=fake)
    expected = [v.weights for v in weight_variations(base)]
    assert [w for _, w in fake.calls] == expected
    assert [a["weights"] for a in job.result["alternatives"]] == expected


def test_the_active_weights_are_the_defaults_for_now(rw):
    assert active_soft_weights(rw.school) == DEFAULT_SOFT_WEIGHTS
    active_soft_weights(rw.school)[CLASSE_LATE_START] = 1  # a copy
    assert DEFAULT_SOFT_WEIGHTS[CLASSE_LATE_START] != 1


def test_the_variation_is_stored_and_described(admin, rw):
    job = start(admin, fake=distinct(rw))
    first, second, third = job.result["alternatives"]
    assert first["variation"]["changed"] is None
    assert second["variation"]["changed"] == CONTRACTOR_GAP
    assert second["variation"]["factor"] == 0.5
    assert alternatives.describe(first["variation"]) == "Pesi attuali"
    text = alternatives.describe(second["variation"])
    assert "dimezzat" not in text and "ridotto" in text
    assert "×0.5" in text and str(second["variation"]["new"]) in text
    assert "aumentato" in alternatives.describe(third["variation"])


def test_the_week_and_its_version_are_untouched_until_the_pick(admin, rw):
    before = state(rw)
    start(admin, fake=distinct(rw))
    assert state(rw) == before
    assert Timetable.objects.count() == 1


def test_alternatives_are_not_lessons_of_the_grid(admin, rw):
    grid_url = f"/orario/{WEEK}/griglia/"

    def grid():
        return re.sub(r'data-csrf="[^"]*"', "", html(admin.get(grid_url)))

    before = grid()
    assert 'class="tt-card' in before
    job = start(admin, fake=distinct(rw))
    assert grid() == before  # same cells, same version
    assert job.result["alternatives"]  # held in the job, not in the week


def test_a_first_week_has_no_timetable_until_the_pick(admin, rw):
    Timetable.objects.all().delete()
    job = start(admin, fake=distinct(rw))
    assert Timetable.objects.count() == 0 and Slot.objects.count() == 0
    assert job.result["base_version"] == 0
    admin.post(choose_url(job, 1))
    assert Timetable.objects.get().version == 1


def test_identical_alternatives_collapse_to_none_significant(admin, rw):
    job = start(admin, fake=Fake([variant(rw, 0)]))
    assert job.status == JobStatus.DONE
    assert job.result["meaningful"] is False
    assert job.result["alternatives"] == []
    assert job.result["message"] == MSG_NO_MEANINGFUL
    page = html(admin.get(PAGE))
    assert MSG_NO_MEANINGFUL in page
    assert ">Scegli<" not in page and "Alternative (" not in page


def test_with_no_significant_alternative_no_pick_is_recorded(admin, rw):
    job = start(admin, fake=Fake([variant(rw, 0)]))
    before = state(rw)
    response = admin.post(choose_url(job, 0))
    assert response.status_code in (404, 409)
    assert AlternativePick.objects.count() == 0 and state(rw) == before


def test_a_base_solve_without_a_solution_fails_the_job(admin, rw):
    answer = dataclasses.replace(rw.base, status=SolveStatus.UNKNOWN, lessons=[])
    job = start(admin, fake=Fake([answer]))
    assert job.status == JobStatus.FAILED
    assert job.error == tasks.timeout_message(60)


def test_an_infeasible_week_fails_the_job_with_the_message(admin, rw):
    answer = dataclasses.replace(rw.base, status=SolveStatus.INFEASIBLE, lessons=[])
    job = start(admin, fake=Fake([answer]))
    assert job.status == JobStatus.FAILED
    assert job.error == tasks.infeasible_message()
    assert AlternativePick.objects.count() == 0


@override_settings(SOLVER_TIMEOUT_SECONDS=17)
def test_the_whole_job_gets_the_solver_timeout_as_its_total(admin, rw):
    seen = []
    real = alternatives.generate_alternatives

    def spy(ctx, weights, total, clock):
        seen.append(total)
        return real(ctx, weights, total, clock)

    with mock.patch.object(alternatives, "generate_alternatives", spy):
        start(admin, fake=distinct(rw))
    assert seen == [17]


@override_settings(SOLVER_TIMEOUT_SECONDS=12)
def test_the_solves_together_stay_within_the_timeout(admin, rw):
    fake = distinct(rw)
    start(admin, fake=fake)
    assert sum(ctx.timeout_seconds for ctx, _ in fake.calls) <= 12


def test_what_was_found_before_the_time_ran_out_is_returned(admin, rw):
    answers = [
        variant(rw, 0),
        variant(rw, 1),
        dataclasses.replace(rw.base, status=SolveStatus.UNKNOWN, lessons=[]),
    ]
    job = start(admin, fake=Fake(answers))
    assert job.status == JobStatus.DONE
    assert [a["index"] for a in job.result["alternatives"]] == [0, 1]


def test_an_unexpected_error_fails_the_job_with_a_message(admin, rw):
    def boom(*a, **k):
        raise RuntimeError("kaputt")

    job = start(admin, fake=boom)
    assert job.status == JobStatus.FAILED
    assert tasks.ERROR_UNEXPECTED in job.error and "kaputt" in job.error


def test_an_approved_week_rejects_generating_alternatives(admin, rw):
    Timetable.objects.filter(pk=rw.tt.pk).update(state=TimetableState.APPROVATO)
    jobs_before = SolveJob.objects.count()
    response = admin.post(GENERATE, {"alternative": "1"})
    assert response.status_code == 409
    assert tasks.ERROR_APPROVED in html(response)
    assert SolveJob.objects.count() == jobs_before


def test_the_job_notices_a_week_approved_while_it_waited(admin, rw):
    admin.post(GENERATE, {"alternative": "1"})
    job = SolveJob.objects.order_by("-pk").first()
    Timetable.objects.filter(pk=rw.tt.pk).update(state=TimetableState.APPROVATO)
    with mock.patch.object(solver_alt, "solve", distinct(rw)):
        tasks.run_job(job.pk)
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED and job.error == tasks.ERROR_APPROVED


# ---- the comparison --------------------------------------------------------------


def test_the_comparison_lists_label_score_level_and_conflicts(admin, rw):
    start(admin, fake=distinct(rw))
    page = html(admin.get(PAGE))
    for label in ("Alternativa A", "Alternativa B", "Alternativa C"):
        assert label in page
    for score in (100.0, 95.0, 90.0):
        assert floatformat(score, 1) in page
    assert "Conflitti morbidi" in page and "Punteggio" in page and "Livello" in page


def test_the_comparison_counts_violations_per_kind(admin, rw):
    job = start(admin, fake=distinct(rw))
    assert [a["violations_by_kind"] for a in job.result["alternatives"]] == [
        {TEORIA_CONSECUTIVE: 1},
        {TEORIA_CONSECUTIVE: 1, CLASSE_LATE_START: 1},
        {TEORIA_CONSECUTIVE: 1, CLASSE_LATE_START: 2, CONTRACTOR_GAP: 1},
    ]
    assert [a["n_soft_conflicts"] for a in job.result["alternatives"]] == [1, 2, 4]
    panel = alternatives.panel(job, rw.tt)
    by_kind = {row["kind"]: row for row in panel["rows"]}
    assert by_kind[CLASSE_LATE_START]["values"] == [0, 1, 2]
    assert by_kind[CONTRACTOR_GAP]["values"] == [0, 0, 1]
    assert by_kind[TEORIA_CONSECUTIVE]["values"] == [1, 1, 1]


def test_only_the_differing_kinds_are_highlighted(admin, rw):
    job = start(admin, fake=distinct(rw))
    rows = {r["kind"]: r["differs"] for r in alternatives.panel(job, rw.tt)["rows"]}
    assert rows == {
        CLASSE_LATE_START: True,
        CONTRACTOR_GAP: True,
        TEORIA_CONSECUTIVE: False,
    }
    page = html(admin.get(PAGE))
    assert page.count('data-differs="true"') == 2
    assert page.count("tt-alt-differs") == 2


def test_an_alternative_can_be_viewed_as_a_grid_before_picking(admin, rw):
    job = start(admin, fake=distinct(rw))
    before = state(rw)
    response = admin.get(view_url(job, 1))
    assert response.status_code == 200
    page = html(response)
    assert "Alternativa B" in page and 'class="tt-grid"' in page
    assert "Mat0" in page and "DOC0" in page  # lessons of the alternative
    assert state(rw) == before and AlternativePick.objects.count() == 0


def test_the_viewed_grid_shows_that_alternatives_own_lessons(admin, rw):
    job = start(admin, fake=distinct(rw))
    a = html(admin.get(view_url(job, 0)))
    c = html(admin.get(view_url(job, 2)))
    assert a != c  # shifted hours land in other cells
    assert a.count("tt-lesson") == c.count("tt-lesson") > 0


def test_viewing_an_unknown_alternative_is_404(admin, rw):
    job = start(admin, fake=distinct(rw))
    assert admin.get(view_url(job, 3)).status_code == 404
    assert admin.get(f"/orario/{WEEK}/alternative/99999/0/griglia/").status_code == 404


def test_the_page_offers_view_and_choose_buttons_to_the_admin(admin, rw):
    job = start(admin, fake=distinct(rw))
    page = html(admin.get(PAGE))
    assert page.count("Vedi griglia") == 3 and page.count(">Scegli<") == 3
    assert choose_url(job, 2) in page and view_url(job, 0) in page


# ---- the pick ---------------------------------------------------------------------


def test_the_pick_writes_lessons_score_and_conflicts(admin, rw):
    job = start(admin, fake=distinct(rw))
    chosen = job.result["alternatives"][2]
    response = admin.post(choose_url(job, 2))
    assert response.status_code == 200
    rw.tt.refresh_from_db()
    expected = {(a, d, h) for a, d, h in chosen["lessons"]}
    stored = {
        (
            Assignment.objects.get(
                teacher_id=s.teacher_id,
                school_class_id=s.school_class_id,
                subject_id=s.subject_id,
            ).pk,
            ["LUNEDI", "MARTEDI", "MERCOLEDI", "GIOVEDI", "VENERDI"].index(s.day),
            s.start_hour - 8,
        )
        for s in Slot.objects.filter(timetable=rw.tt)
    }
    assert stored == expected
    assert rw.tt.quality_score == chosen["score"] == 90.0
    assert rw.tt.quality_level == chosen["level"] == "B"
    assert rw.tt.soft_conflicts == chosen["n_soft_conflicts"] == 4
    assert rw.tt.conflicts == chosen["conflict_items"] == [ITEM]


def test_the_pick_bumps_the_version_exactly_once(admin, rw):
    job = start(admin, fake=distinct(rw))
    before = state(rw)[0]
    admin.post(choose_url(job, 1))
    assert state(rw)[0] == before + 1


def test_the_pick_returns_the_full_grid(admin, rw):
    job = start(admin, fake=distinct(rw))
    page = html(admin.post(choose_url(job, 1)))
    assert 'id="timetable-body"' in page and 'class="tt-grid"' in page
    assert "Alternativa B scelta" in page
    assert f'data-version="{state(rw)[0]}"' in page
    assert ">Scegli<" not in page  # the choice step is gone


def test_the_pick_replaces_the_conflict_decisions_of_the_old_week(admin, rw):
    from scheduling.models import ConflictDecision

    ConflictDecision.objects.create(timetable=rw.tt, chiave="k")
    job = start(admin, fake=distinct(rw))
    admin.post(choose_url(job, 1))
    assert not ConflictDecision.objects.filter(timetable=rw.tt).exists()


def test_a_stored_pick_has_every_field(admin, rw):
    job = start(admin, fake=distinct(rw))
    admin.post(choose_url(job, 1))
    pick = AlternativePick.objects.get()
    assert pick.job_id == job.pk
    assert pick.school_id == rw.school.pk and pick.week_start == MON
    assert pick.picked_by_id == admin.user.pk
    alts = job.result["alternatives"]
    keys = {
        "index",
        "label",
        "variation",
        "weights",
        "score",
        "level",
        "n_soft_conflicts",
        "violations_by_kind",
    }
    assert set(pick.chosen) == keys
    assert pick.chosen["label"] == "Alternativa B"
    assert pick.chosen["weights"] == alts[1]["weights"]
    assert pick.chosen["score"] == 95.0 and pick.chosen["level"] == "B"
    assert pick.chosen["n_soft_conflicts"] == 2
    assert pick.chosen["violations_by_kind"] == alts[1]["violations_by_kind"]
    assert [r["label"] for r in pick.rejected] == ["Alternativa A", "Alternativa C"]
    for rejected, original in zip(pick.rejected, (alts[0], alts[2]), strict=True):
        assert set(rejected) == keys
        assert rejected["weights"] == original["weights"]
        assert rejected["score"] == original["score"]
        assert rejected["violations_by_kind"] == original["violations_by_kind"]
        assert rejected["n_soft_conflicts"] == original["n_soft_conflicts"]
    assert pick.created_at is not None


def test_the_stored_pick_has_no_lessons(admin, rw):
    job = start(admin, fake=distinct(rw))
    admin.post(choose_url(job, 0))
    pick = AlternativePick.objects.get()
    assert "lessons" not in pick.chosen
    assert all("lessons" not in r for r in pick.rejected)


def test_picking_twice_is_refused_and_stores_one_pick(admin, rw):
    job = start(admin, fake=distinct(rw))
    assert admin.post(choose_url(job, 1)).status_code == 200
    after_first = state(rw)
    response = admin.post(choose_url(job, 2))
    assert response.status_code == 409
    assert MSG_ALREADY_PICKED in html(response)
    assert AlternativePick.objects.count() == 1
    assert AlternativePick.objects.get().chosen["label"] == "Alternativa B"
    assert state(rw) == after_first


def test_the_same_alternative_twice_is_refused_too(admin, rw):
    job = start(admin, fake=distinct(rw))
    admin.post(choose_url(job, 1))
    response = admin.post(choose_url(job, 1))
    assert response.status_code == 409 and MSG_ALREADY_PICKED in html(response)


def test_a_regenerated_week_refuses_the_pick(admin, rw):
    job = start(admin, fake=distinct(rw))
    regen = SolveJob.objects.create(school=rw.school, week_start=MON)
    tasks.run_job(regen.pk)  # bumps the version
    before = state(rw)
    response = admin.post(choose_url(job, 1))
    assert response.status_code == 409 and MSG_STALE in html(response)
    assert AlternativePick.objects.count() == 0 and state(rw) == before
    job.refresh_from_db()
    assert job.result["picked"] is False


def test_an_edited_week_refuses_the_pick(admin, rw):
    job = start(admin, fake=distinct(rw))
    apply_change(rw.tt.pk, lambda locked: None)  # any edit bumps the version
    before = state(rw)
    response = admin.post(choose_url(job, 1))
    assert response.status_code == 409 and MSG_STALE in html(response)
    assert AlternativePick.objects.count() == 0 and state(rw) == before


def test_a_week_created_after_the_job_refuses_the_pick(admin, rw):
    Timetable.objects.all().delete()
    job = start(admin, fake=distinct(rw))
    other = Timetable.objects.create(school=rw.school, week_start=MON)
    apply_change(other.pk, lambda locked: None)
    response = admin.post(choose_url(job, 1))
    assert response.status_code == 409 and MSG_STALE in html(response)
    assert AlternativePick.objects.count() == 0 and Slot.objects.count() == 0


def test_a_stale_comparison_shows_the_message_and_no_choose_button(admin, rw):
    job = start(admin, fake=distinct(rw))
    apply_change(rw.tt.pk, lambda locked: None)
    page = html(admin.get(PAGE))
    assert "L'orario è cambiato dopo il calcolo delle alternative" in page
    assert ">Scegli<" not in page
    assert alternatives.panel(job, Timetable.objects.get())["stale"] is True


def test_an_approved_week_rejects_the_pick(admin, rw):
    job = start(admin, fake=distinct(rw))
    Timetable.objects.filter(pk=rw.tt.pk).update(state=TimetableState.APPROVATO)
    before = state(rw)
    response = admin.post(choose_url(job, 1))
    assert response.status_code == 409
    assert tasks.ERROR_APPROVED in html(response)
    assert AlternativePick.objects.count() == 0 and state(rw) == before
    page = html(admin.get(PAGE))
    assert ">Scegli<" not in page


def test_a_failed_pick_leaves_everything_as_it_was(admin, rw):
    job = start(admin, fake=distinct(rw))
    before = state(rw)
    with mock.patch.object(
        AlternativePick.objects, "create", side_effect=RuntimeError("db")
    ):
        with pytest.raises(RuntimeError):
            admin.post(choose_url(job, 1))
    assert state(rw) == before
    job.refresh_from_db()
    assert job.result["picked"] is False


def test_an_unknown_alternative_or_job_is_404(admin, rw):
    job = start(admin, fake=distinct(rw))
    assert admin.post(choose_url(job, 7)).status_code == 404
    assert admin.post(f"/orario/{WEEK}/alternative/99999/0/scegli/").status_code == 404
    assert AlternativePick.objects.count() == 0


def test_a_job_of_another_week_cannot_be_picked_here(admin, rw):
    job = start(admin, fake=distinct(rw))
    other = f"/orario/{MON + datetime.timedelta(days=7):%Y-%m-%d}/alternative/"
    # The week is not in the formative year or the job belongs elsewhere.
    response = admin.post(f"{other}{job.pk}/0/scegli/")
    assert response.status_code == 404
    assert AlternativePick.objects.count() == 0


def test_an_ordinary_job_is_not_an_alternatives_job(admin, rw):
    ordinary = SolveJob.objects.filter(kind=JobKind.GENERATE).first()
    assert admin.post(choose_url(ordinary, 0)).status_code == 404
    assert admin.get(view_url(ordinary, 0)).status_code == 404


def test_a_job_not_done_cannot_be_picked(admin, rw):
    admin.post(GENERATE, {"alternative": "1"})
    job = SolveJob.objects.order_by("-pk").first()
    assert job.status == JobStatus.PENDING
    assert admin.post(choose_url(job, 0)).status_code == 409
    assert AlternativePick.objects.count() == 0


def test_choose_alternative_checks_the_predicate(rw, admin):
    job = start(admin, fake=distinct(rw))
    seg = User.objects.create_user("x@example.com", "pw-long-enough", "SEGRETERIA")
    with pytest.raises(PermissionDenied):
        alternatives.choose_alternative(job.pk, 1, seg)
    assert AlternativePick.objects.count() == 0


# ---- roles -------------------------------------------------------------------------


def test_segreteria_cannot_generate_alternatives(seg, rw):
    jobs_before = SolveJob.objects.count()
    assert seg.post(GENERATE, {"alternative": "1"}).status_code == 403
    assert SolveJob.objects.count() == jobs_before


def test_enqueueing_alternatives_checks_can_generate(rw):
    seg_user = User.objects.create_user("y@example.com", "pw-long-enough", "SEGRETERIA")
    with pytest.raises(PermissionDenied):
        enqueue_generation(MON, seg_user, alternatives=True)


def test_segreteria_cannot_pick_or_view_alternatives(admin, seg, rw):
    job = start(admin, fake=distinct(rw))
    before = state(rw)
    assert seg.post(choose_url(job, 1)).status_code == 403
    assert seg.get(view_url(job, 1)).status_code == 403
    assert AlternativePick.objects.count() == 0 and state(rw) == before


def test_segreteria_sees_no_buttons(admin, seg, rw):
    start(admin, fake=distinct(rw))
    page = html(seg.get(PAGE))
    assert ">Scegli<" not in page and "Vedi griglia" not in page
    assert "Genera alternative" not in page and "Alternativa A" not in page


def test_segreteria_does_not_see_the_no_significant_message_either(admin, seg, rw):
    start(admin, fake=Fake([variant(rw, 0)]))
    assert MSG_NO_MEANINGFUL not in html(seg.get(PAGE))


def test_anonymous_is_sent_to_login(admin, rw):
    job = start(admin, fake=distinct(rw))
    anon = Client()
    for response in (
        anon.post(GENERATE, {"alternative": "1"}),
        anon.post(choose_url(job, 1)),
        anon.get(view_url(job, 1)),
    ):
        assert response.status_code == 302 and "/login" in response["Location"]
    assert AlternativePick.objects.count() == 0


def test_the_polled_status_area_carries_the_comparison_when_done(admin, rw):
    start(admin, fake=distinct(rw))
    page = html(admin.get(JOB_URL))
    assert "Alternativa A" in page and ">Scegli<" in page


# ---- weights are never changed ---------------------------------------------------


def weight_models():
    return [
        m for m in apps.get_models() if m.__name__.lower() == "softweightadjustment"
    ]


def test_generating_and_picking_changes_no_weight(admin, rw):
    defaults = dict(DEFAULT_SOFT_WEIGHTS)
    before_active = active_soft_weights(rw.school)
    counts = {m: list(m.objects.values()) for m in weight_models()}
    job = start(admin, fake=distinct(rw))
    admin.post(choose_url(job, 2))
    assert DEFAULT_SOFT_WEIGHTS == defaults
    assert active_soft_weights(rw.school) == before_active
    assert {m: list(m.objects.values()) for m in weight_models()} == counts
    assert AlternativePick.objects.get().chosen["weights"] != before_active


def test_the_solver_is_not_handed_the_shared_weight_dict(admin, rw):
    fake = distinct(rw)
    start(admin, fake=fake)
    assert all(w is not DEFAULT_SOFT_WEIGHTS for _, w in fake.calls)


# ---- the real solver, end to end ----------------------------------------------------


def test_the_real_solver_job_stores_only_valid_alternatives(admin, rw):
    admin.post(GENERATE, {"alternative": "1"})
    job = SolveJob.objects.order_by("-pk").first()
    tasks.run_job(job.pk)
    job.refresh_from_db()
    assert job.status == JobStatus.DONE
    assert job.result["picked"] is False
    alts = job.result["alternatives"]
    assert len(alts) in (0, 2, 3)
    assert job.result["meaningful"] is bool(alts)
    for alt in alts:
        teacher_cells, class_cells = set(), set()
        by_id = Assignment.objects.in_bulk()
        per_class_day = {}
        for aid, day, hour in alt["lessons"]:
            a = by_id[aid]
            assert (a.teacher_id, day, hour) not in teacher_cells
            assert (a.school_class_id, day, hour) not in class_cells
            teacher_cells.add((a.teacher_id, day, hour))
            class_cells.add((a.school_class_id, day, hour))
            per_class_day[a.school_class_id, day] = (
                per_class_day.get((a.school_class_id, day), 0) + 1
            )
        assert set(per_class_day.values()) == {4}
        assert len(per_class_day) == 15

import datetime
import threading
import time
from pathlib import Path
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.test import override_settings
from django_tasks_db.models import DBTaskResult

from scheduling import jobs, tasks
from scheduling.jobs import GenerationRefused, enqueue_generation
from scheduling.models import (
    ConflictDecision,
    InvalidJobTransition,
    JobKind,
    JobStatus,
    Slot,
    SolveJob,
    Timetable,
    TimetableState,
)
from scheduling.solver import SolveResult, SolveStatus
from school.models import (
    Assignment,
    CalendarEntry,
    Class,
    ClassPairing,
    School,
    Subject,
    Teacher,
)

D = datetime.date
MON = D(2026, 10, 12)
User = get_user_model()
DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend"}}
IMMEDIATE_TASKS = {
    "default": {"BACKEND": "django_tasks.backends.immediate.ImmediateBackend"}
}


@pytest.fixture
def admin(db):
    return User.objects.create_user("a@example.com", "pw-long-enough", "ADMIN")


@pytest.fixture
def segreteria(db):
    return User.objects.create_user("s@example.com", "pw-long-enough", "SEGRETERIA")


@pytest.fixture
def school(db):
    return School.objects.create(
        name="A",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2026, 12, 21),
    )


def open_monday_only(school, hours=2):
    """Monday has `hours` lessons, the rest of the week is closed."""
    CalendarEntry.objects.create(school=school, date=MON, max_hours=hours)
    for i in range(1, 5):
        CalendarEntry.objects.create(
            school=school, date=MON + datetime.timedelta(days=i), is_closure=True
        )


@pytest.fixture
def week(school):
    """A feasible week: one class, 2 Monday lessons, one assignment."""
    open_monday_only(school)
    klass = Class.objects.create(school=school, name="1A")
    teacher = Teacher.objects.create(
        school=school, name="ROSSI", type="ASSUNTO", first_time=False
    )
    subject = Subject.objects.create(
        school=school, name="Mat", kind="TEORIA", cognitive_weight="MEDIO"
    )
    assignment = Assignment.objects.create(
        school=school,
        teacher=teacher,
        school_class=klass,
        subject=subject,
        ore_totali=20,
    )
    return school, assignment


def pending_job(school, **kw):
    return SolveJob.objects.create(school=school, week_start=MON, **kw)


def run(job):
    tasks.run_job(job.pk)
    job.refresh_from_db()
    return job


# ---- model and transitions ------------------------------------------------


def test_job_defaults(school):
    job = pending_job(school)
    assert job.status == JobStatus.PENDING
    assert job.kind == JobKind.GENERATE
    assert job.params == {} and job.result == {} and job.error == ""
    assert job.started_at is None and job.finished_at is None


ALLOWED = {
    (JobStatus.PENDING, JobStatus.RUNNING),
    (JobStatus.PENDING, JobStatus.FAILED),
    (JobStatus.RUNNING, JobStatus.DONE),
    (JobStatus.RUNNING, JobStatus.FAILED),
}


@pytest.mark.parametrize("start", JobStatus.values)
@pytest.mark.parametrize("target", JobStatus.values)
def test_transitions_only_forward(school, start, target):
    job = pending_job(school)
    SolveJob.objects.filter(pk=job.pk).update(status=start)
    job.refresh_from_db()
    if (start, target) in ALLOWED:
        job.transition_to(target)
        assert SolveJob.objects.get(pk=job.pk).status == target
    else:
        with pytest.raises(InvalidJobTransition):
            job.transition_to(target)
        assert SolveJob.objects.get(pk=job.pk).status == start


def test_transition_loses_against_a_concurrent_change(school):
    job = pending_job(school)
    SolveJob.objects.filter(pk=job.pk).update(status=JobStatus.RUNNING)
    with pytest.raises(InvalidJobTransition):
        job.transition_to(JobStatus.RUNNING)  # stale in-memory PENDING


def test_started_and_finished_at(week):
    school, _ = week
    job = pending_job(school)
    job.transition_to(JobStatus.RUNNING)
    job.refresh_from_db()
    assert job.started_at is not None and job.finished_at is None
    job.transition_to(JobStatus.FAILED, error="boom")
    job.refresh_from_db()
    assert job.finished_at >= job.started_at
    assert job.error == "boom"


def test_run_sets_both_timestamps(week):
    school, _ = week
    job = run(pending_job(school))
    assert job.status == JobStatus.DONE
    assert job.started_at is not None
    assert job.finished_at is not None and job.finished_at >= job.started_at


# ---- enqueue ----------------------------------------------------------------


def test_enqueue_creates_pending_job_and_does_not_run(week, admin):
    school, _ = week
    job = enqueue_generation(MON, admin)
    assert job.status == JobStatus.PENDING
    assert job.requested_by == admin
    assert (job.school, job.week_start, job.kind) == (school, MON, JobKind.GENERATE)
    assert not Timetable.objects.exists()


def test_enqueue_calls_the_task_with_the_job_id(week, admin):
    with mock.patch.object(jobs, "run_solve_job") as task:
        job = enqueue_generation(MON, admin)
    task.enqueue.assert_called_once_with(job.pk)


@override_settings(TASKS=DB_TASKS)
def test_enqueue_with_db_backend_writes_a_task_row(week, admin):
    job = enqueue_generation(MON, admin)
    rows = DBTaskResult.objects.all()
    assert rows.count() == 1
    assert rows[0].args_kwargs["args"] == [job.pk]
    assert job.status == JobStatus.PENDING


@override_settings(TASKS=IMMEDIATE_TASKS)
def test_enqueue_with_immediate_backend_solves(week, admin):
    job = enqueue_generation(MON, admin)
    job.refresh_from_db()
    assert job.status == JobStatus.DONE
    assert Timetable.objects.get().slots.count() == 2


def test_enqueue_failure_leaves_no_job(week, admin):
    with mock.patch.object(jobs, "run_solve_job", **{"enqueue.side_effect": OSError}):
        with pytest.raises(OSError):
            enqueue_generation(MON, admin)
    assert not SolveJob.objects.exists()


def test_enqueue_refused_for_approved_week(week, admin):
    school, _ = week
    Timetable.objects.create(
        school=school, week_start=MON, state=TimetableState.APPROVATO
    )
    with pytest.raises(GenerationRefused, match="approvata"):
        enqueue_generation(MON, admin)
    assert not SolveJob.objects.exists()


def test_enqueue_allowed_for_bozza_week(week, admin):
    school, _ = week
    Timetable.objects.create(school=school, week_start=MON)
    assert enqueue_generation(MON, admin).pk


def test_enqueue_refused_without_assignments(school, admin):
    with pytest.raises(GenerationRefused, match="assegnazion"):
        enqueue_generation(MON, admin)
    assert not SolveJob.objects.exists()


def test_enqueue_refused_when_only_inactive_teachers_have_assignments(week, admin):
    _, assignment = week
    Teacher.objects.filter(pk=assignment.teacher_id).update(active=False)
    with pytest.raises(GenerationRefused):
        enqueue_generation(MON, admin)


def test_enqueue_refused_for_segreteria(week, segreteria):
    with pytest.raises(PermissionDenied):
        enqueue_generation(MON, segreteria)
    assert not SolveJob.objects.exists()


def test_enqueue_refused_for_anonymous_and_none(week):
    from django.contrib.auth.models import AnonymousUser

    for user in (AnonymousUser(), None):
        with pytest.raises(PermissionDenied):
            enqueue_generation(MON, user)


def test_enqueue_allowed_for_admin(week, admin):
    assert enqueue_generation(MON, admin).status == JobStatus.PENDING


def test_enqueue_rejects_a_non_monday(week, admin):
    with pytest.raises(ValueError):
        enqueue_generation(MON + datetime.timedelta(days=1), admin)


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RUNNING])
def test_enqueue_returns_existing_active_job(week, admin, status):
    school, _ = week
    existing = pending_job(school)
    SolveJob.objects.filter(pk=existing.pk).update(status=status)
    with mock.patch.object(jobs, "run_solve_job") as task:
        job = enqueue_generation(MON, admin)
    assert job.pk == existing.pk
    assert SolveJob.objects.count() == 1
    task.enqueue.assert_not_called()


@pytest.mark.parametrize("status", [JobStatus.DONE, JobStatus.FAILED])
def test_enqueue_after_a_finished_job_creates_a_new_one(week, admin, status):
    school, _ = week
    old = pending_job(school)
    SolveJob.objects.filter(pk=old.pk).update(status=status)
    assert enqueue_generation(MON, admin).pk != old.pk


def test_enqueue_other_week_is_independent(week, admin):
    school, _ = week
    other = SolveJob.objects.create(
        school=school, week_start=MON + datetime.timedelta(days=7)
    )
    assert enqueue_generation(MON, admin).pk != other.pk


def test_one_active_job_per_week_is_a_database_constraint(school):
    from django.db import IntegrityError, transaction

    pending_job(school)
    with pytest.raises(IntegrityError), transaction.atomic():
        pending_job(school)


# ---- running ----------------------------------------------------------------


def test_run_writes_slots_score_and_bumps_version_once(week):
    school, assignment = week
    job = run(pending_job(school))
    assert job.status == JobStatus.DONE
    tt = Timetable.objects.get()
    assert tt.version == 1
    assert tt.state == TimetableState.BOZZA
    assert tt.quality_score is not None and tt.quality_level in {"A", "B", "C"}
    slots = list(tt.slots.order_by("start_hour"))
    assert [(s.day, s.start_hour, s.end_hour) for s in slots] == [
        ("LUNEDI", 8, 9),
        ("LUNEDI", 9, 10),
    ]
    assert {(s.school_class_id, s.teacher_id, s.subject_id) for s in slots} == {
        (
            assignment.school_class_id,
            assignment.teacher_id,
            assignment.subject_id,
        )
    }
    assert not any(s.paired for s in slots)
    assert job.result["version"] == 1
    assert job.result["timetable_id"] == tt.pk
    assert job.result["n_slots"] == 2
    assert job.result["quality_score"] == tt.quality_score
    assert job.result["quality_level"] == tt.quality_level
    assert job.error == ""


def test_run_stores_soft_conflict_count_and_conflict_items(week):
    school, _ = week
    items = [{"chiave": "k1", "kind": "x"}, {"chiave": "k2", "kind": "y"}]
    fake = SolveResult(
        SolveStatus.OPTIMAL,
        [],
        violations=[object(), object(), object()],
        quality_score=87.5,
        quality_level="B",
        conflict_items=items,
    )
    with mock.patch.object(tasks, "solve", return_value=fake):
        job = run(pending_job(school))
    tt = Timetable.objects.get()
    assert (tt.quality_score, tt.quality_level) == (87.5, "B")
    assert tt.soft_conflicts == 3
    assert tt.conflicts == items
    assert job.result["n_soft_conflicts"] == 3
    assert job.result["n_conflicts"] == 2


def test_run_stores_conflict_items_not_just_quasi_hard_conflicts(week):
    school, assignment = week
    # target 5 vs 2 lessons available: an S3 deviation, not a quasi-hard one
    run(pending_job(school))
    tt = Timetable.objects.get()
    assert tt.soft_conflicts == len(tt.conflicts)
    assert all("chiave" in c and "description" in c for c in tt.conflicts)


def test_conflict_descriptions_say_how_the_target_was_derived(week):
    school, assignment = week
    Assignment.objects.filter(pk=assignment.pk).update(ore_totali=3)
    run(pending_job(school))
    s3 = [
        c
        for c in Timetable.objects.get().conflicts
        if c["kind"] == "ore_target_deviation"
    ]
    assert s3 and "residuo 3 ore su 10 settimane" in s3[0]["description"]


def test_run_passes_the_timeout_from_settings(week):
    school, _ = week
    seen = []

    def fake(ctx, weights=None):
        seen.append(ctx.timeout_seconds)
        return SolveResult(SolveStatus.OPTIMAL)

    with override_settings(SOLVER_TIMEOUT_SECONDS=7):
        with mock.patch.object(tasks, "solve", side_effect=fake):
            run(pending_job(school))
    assert seen == [7]


def test_run_builds_the_context_of_the_job_week_and_school(week):
    school, _ = week
    with mock.patch.object(
        tasks, "build_week_context", side_effect=RuntimeError("x")
    ) as build:
        run(pending_job(school))
    build.assert_called_once_with(MON, school)


def test_regeneration_keeps_timetable_replaces_slots_clears_decisions(week):
    school, _ = week
    run(pending_job(school))
    tt = Timetable.objects.get()
    old_ids = set(tt.slots.values_list("pk", flat=True))
    ConflictDecision.objects.create(timetable=tt, chiave="k")
    other = Timetable.objects.create(
        school=school, week_start=MON + datetime.timedelta(7)
    )
    ConflictDecision.objects.create(timetable=other, chiave="k")
    run(pending_job(school))
    again = Timetable.objects.get(pk=tt.pk)
    assert again.version == 2
    assert Timetable.objects.filter(week_start=MON).count() == 1
    new_ids = set(again.slots.values_list("pk", flat=True))
    assert len(new_ids) == 2 and not (new_ids & old_ids)
    assert not ConflictDecision.objects.filter(timetable=tt).exists()
    assert ConflictDecision.objects.filter(timetable=other).count() == 1


def test_regeneration_replaces_the_stored_conflicts(week):
    school, _ = week
    run(pending_job(school))
    Timetable.objects.update(conflicts=[{"chiave": "stale"}], soft_conflicts=9)
    run(pending_job(school))
    tt = Timetable.objects.get()
    assert "stale" not in str(tt.conflicts)
    assert tt.soft_conflicts == len(tt.conflicts)


def test_write_failure_rolls_back_everything(week):
    school, _ = week
    run(pending_job(school))
    tt = Timetable.objects.get()
    before = sorted(tt.slots.values_list("pk", flat=True))
    ConflictDecision.objects.create(timetable=tt, chiave="k")
    score = tt.quality_score
    with mock.patch.object(
        tasks.Slot.objects, "bulk_create", side_effect=OSError("disk")
    ):
        job = run(pending_job(school))
    assert job.status == JobStatus.FAILED
    assert "disk" in job.error
    again = Timetable.objects.get()
    assert again.version == tt.version
    assert again.quality_score == score
    assert sorted(again.slots.values_list("pk", flat=True)) == before
    assert ConflictDecision.objects.filter(timetable=tt).count() == 1


def test_write_failure_on_a_new_week_leaves_no_timetable(week):
    school, _ = week
    with mock.patch.object(tasks.Slot.objects, "bulk_create", side_effect=OSError):
        job = run(pending_job(school))
    assert job.status == JobStatus.FAILED
    assert not Timetable.objects.exists()


def test_infeasible_week_gives_best_effort_with_message(week):
    school, assignment = week
    Assignment.objects.filter(pk=assignment.pk).update(ore_totali=0)
    job = run(pending_job(school))
    assert job.status == JobStatus.DONE
    assert job.result["message"].startswith("Nessun orario completo possibile")
    tt = Timetable.objects.get()
    assert tt.version == 1
    assert any(c["kind"] == "classe_unfilled" for c in tt.conflicts)


def test_feasible_week_has_empty_message(week):
    school, _ = week
    assert run(pending_job(school)).result["message"] == ""


def test_timeout_without_solution_fails_and_keeps_slots(week):
    school, _ = week
    run(pending_job(school))
    tt = Timetable.objects.get()
    before = sorted(tt.slots.values_list("pk", flat=True))
    unknown = SolveResult(SolveStatus.UNKNOWN)
    with override_settings(SOLVER_TIMEOUT_SECONDS=1):
        with mock.patch.object(tasks, "solve", return_value=unknown):
            job = run(pending_job(school))
    assert job.status == JobStatus.FAILED
    assert "timeout" in job.error and "1 secondi" in job.error
    again = Timetable.objects.get()
    assert again.version == tt.version
    assert sorted(again.slots.values_list("pk", flat=True)) == before


def test_infeasible_result_fails_with_message(week):
    school, _ = week
    with mock.patch.object(
        tasks, "solve", return_value=SolveResult(SolveStatus.INFEASIBLE)
    ):
        job = run(pending_job(school))
    assert job.status == JobStatus.FAILED
    assert job.error == "Nessun orario possibile per questa settimana."
    assert not Timetable.objects.exists()


@pytest.mark.parametrize("where", ["solve", "build_week_context"])
def test_exception_fails_the_job_with_a_message_not_a_traceback(week, where):
    school, _ = week
    with mock.patch.object(tasks, where, side_effect=ValueError("modello rotto")):
        job = run(pending_job(school))
    assert job.status == JobStatus.FAILED
    assert "modello rotto" in job.error
    assert "Traceback" not in job.error
    assert job.finished_at is not None


def test_worker_keeps_going_after_a_failed_job(week):
    school, _ = week
    first = pending_job(school)
    with mock.patch.object(tasks, "solve", side_effect=RuntimeError("x")):
        tasks.run_solve_job.call(first.pk)  # does not raise
    first.refresh_from_db()
    assert first.status == JobStatus.FAILED
    second = run(pending_job(school))
    assert second.status == JobStatus.DONE


def test_approved_between_enqueue_and_run_fails_the_job(week):
    school, _ = week
    Timetable.objects.create(
        school=school, week_start=MON, state=TimetableState.APPROVATO
    )
    job = run(pending_job(school))
    assert job.status == JobStatus.FAILED
    assert "approvata" in job.error
    assert Timetable.objects.get().version == 0


def test_run_skips_a_job_that_is_not_pending(week):
    school, _ = week
    job = pending_job(school)
    SolveJob.objects.filter(pk=job.pk).update(status=JobStatus.DONE)
    with mock.patch.object(tasks, "solve") as solve:
        tasks.run_job(job.pk)
    solve.assert_not_called()
    tasks.run_job(999999)  # missing job: no error


def test_paired_lessons_become_paired_slots(week):
    school, assignment = week
    klass_b = Class.objects.create(school=school, name="1B")
    Assignment.objects.create(
        school=school,
        teacher=assignment.teacher,
        school_class=klass_b,
        subject=assignment.subject,
        ore_totali=20,
    )
    ClassPairing.objects.create(
        school=school,
        class_a=assignment.school_class,
        class_b=klass_b,
        subject=assignment.subject,
        teacher=assignment.teacher,
    )
    job = run(pending_job(school))
    assert job.status == JobStatus.DONE
    slots = Slot.objects.all()
    assert slots.count() == 4
    for s in slots:
        assert s.paired
        assert s.paired_class_id == (
            klass_b.pk
            if s.school_class_id == assignment.school_class_id
            else assignment.school_class_id
        )


# ---- one solve at a time, timeout setting, worker command ---------------------


def test_solves_never_overlap_in_a_process():
    active, peak = [0], [0]
    guard = threading.Lock()

    def slow(ctx):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with guard:
            active[0] -= 1
        return SolveResult(SolveStatus.OPTIMAL)

    with mock.patch.object(tasks, "solve", side_effect=slow):
        threads = [
            threading.Thread(target=tasks.solve_exclusive, args=(None,))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert peak[0] == 1


def test_no_hardcoded_sixty_in_scheduling_code():
    root = Path(__file__).resolve().parent.parent / "scheduling"
    for path in (root / "tasks.py", root / "jobs.py", root / "models.py"):
        assert "60" not in path.read_text(), path


def test_documented_and_supplied_timeout_setting():
    base = Path(__file__).resolve().parent.parent
    assert "SOLVER_TIMEOUT_SECONDS=" in (base / ".env.example").read_text()
    assert "SOLVER_TIMEOUT_SECONDS" in (base / "config/settings_test.py").read_text()


@override_settings(TASKS=DB_TASKS)
@pytest.mark.django_db(transaction=True)
def test_solve_worker_processes_a_pending_job_and_exits(week, admin):
    job = enqueue_generation(MON, admin)
    call_command("solve_worker", "--batch", "--no-startup-delay", "--interval", "0")
    job.refresh_from_db()
    assert job.status == JobStatus.DONE
    assert Timetable.objects.get().slots.count() == 2

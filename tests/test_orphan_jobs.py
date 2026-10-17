"""Recovery of PENDING solve jobs whose task row is gone or dead (#109)."""

import datetime
from unittest import mock

import pytest
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from accounts.models import User
from scheduling.jobs import (
    ERROR_LOST_JOB,
    enqueue_generation,
    fail_stale_jobs,
)
from scheduling.models import (
    AuditLog,
    JobKind,
    JobStatus,
    Slot,
    SolveJob,
    Timetable,
)
from scheduling.tasks import ALTERNATIVES_KEY, run_repair_job, run_solve_job
from school.models import Assignment, CalendarEntry, Class, School, Subject, Teacher

D = datetime.date
MON = D(2026, 10, 12)
OTHER_MON = D(2026, 10, 19)
STATUS = f"/orario/{MON:%Y-%m-%d}/stato/"
DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend"}}
SEC = datetime.timedelta(seconds=1)


@pytest.fixture
def school(db):
    return School.objects.create(
        name="S",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2026, 12, 21),
    )


@pytest.fixture
def week(school):
    CalendarEntry.objects.create(school=school, date=MON, max_hours=2)
    for i in range(1, 5):
        CalendarEntry.objects.create(
            school=school, date=MON + datetime.timedelta(days=i), is_closure=True
        )
    klass = Class.objects.create(school=school, name="1A")
    teacher = Teacher.objects.create(
        school=school, name="ROSSI", type="ASSUNTO", first_time=False
    )
    subject = Subject.objects.create(
        school=school, name="Mat", kind="TEORIA", cognitive_weight="MEDIO"
    )
    Assignment.objects.create(
        school=school,
        teacher=teacher,
        school_class=klass,
        subject=subject,
        ore_totali=20,
    )
    return school


@pytest.fixture
def admin(db):
    return User.objects.create_user("a@example.com", "pw-long-enough", "ADMIN")


def limit():
    from django.conf import settings

    return datetime.timedelta(
        seconds=settings.SOLVER_TIMEOUT_SECONDS + settings.SOLVER_STALE_MARGIN_SECONDS
    )


def pending(school, age=None, task=run_solve_job, enqueue=True, week_start=MON, **kw):
    """A PENDING job `age` old (default: past the limit), with a real task row."""
    job = SolveJob.objects.create(school=school, week_start=week_start, **kw)
    SolveJob.objects.filter(pk=job.pk).update(
        created_at=timezone.now() - (age if age is not None else limit() + SEC)
    )
    if enqueue:
        with override_settings(TASKS=DB_TASKS):
            task.enqueue(job.pk)
    return job


def row(job):
    return DBTaskResult.objects.get(args_kwargs__args__0=job.pk)


def state(job):
    job.refresh_from_db()
    return job.status


def test_no_task_row_is_failed(school):
    job = pending(school, enqueue=False)
    assert fail_stale_jobs() == 1
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED and job.error == ERROR_LOST_JOB
    assert job.finished_at is not None and job.started_at is None


def test_ready_row_is_never_failed(school):
    job = pending(school, age=limit() * 10)
    assert fail_stale_jobs() == 0
    assert state(job) == JobStatus.PENDING


def test_running_row_old_is_failed_recent_is_kept(school):
    old = pending(school)
    DBTaskResult.objects.filter(pk=row(old).pk).update(
        status="RUNNING", started_at=timezone.now() - limit() - SEC
    )
    recent = pending(school, week_start=OTHER_MON)
    DBTaskResult.objects.filter(pk=row(recent).pk).update(
        status="RUNNING", started_at=timezone.now()
    )
    assert fail_stale_jobs() == 1
    assert state(old) == JobStatus.FAILED
    assert state(recent) == JobStatus.PENDING


def test_failed_and_successful_rows_are_orphans(school):
    failed = pending(school)
    row(failed).set_failed(RuntimeError("boom"))
    done = pending(school, week_start=OTHER_MON)
    DBTaskResult.objects.filter(pk=row(done).pk).update(status="SUCCESSFUL")
    assert fail_stale_jobs() == 2
    assert state(failed) == state(done) == JobStatus.FAILED


def test_boundaries(school):
    now = timezone.now()
    exact = pending(school, enqueue=False)
    SolveJob.objects.filter(pk=exact.pk).update(created_at=now - limit())
    inside = pending(school, age=limit() - SEC, enqueue=False, week_start=OTHER_MON)
    assert fail_stale_jobs(now=now) == 0
    assert state(exact) == state(inside) == JobStatus.PENDING
    assert fail_stale_jobs(now=now + SEC) == 1
    assert state(exact) == JobStatus.FAILED and state(inside) == JobStatus.PENDING


def test_row_matched_by_pk_and_task_path(school):
    lost = pending(school, enqueue=False)
    pending(school, week_start=OTHER_MON)  # another job with a READY row
    # a READY row of another task path with the lost job's pk is no help
    with override_settings(TASKS=DB_TASKS):
        run_solve_job.enqueue(lost.pk)
    DBTaskResult.objects.filter(args_kwargs__args__0=lost.pk).update(
        task_path="other.module.task"
    )
    assert fail_stale_jobs() == 1
    assert state(lost) == JobStatus.FAILED


def test_unrelated_task_path_does_not_save(school):
    job = pending(school)
    DBTaskResult.objects.filter(pk=row(job).pk).update(task_path="other.module.task")
    assert fail_stale_jobs() == 1
    assert state(job) == JobStatus.FAILED


@pytest.mark.parametrize(
    "kw",
    [
        {"kind": JobKind.REPAIR},
        {"params": {ALTERNATIVES_KEY: True}},
    ],
)
def test_repair_and_alternatives(school, kw):
    task = run_repair_job if kw.get("kind") == JobKind.REPAIR else run_solve_job
    lost = pending(school, enqueue=False, **kw)
    kept = pending(school, task=task, week_start=OTHER_MON, **kw)
    assert fail_stale_jobs() == 1
    assert state(lost) == JobStatus.FAILED and state(kept) == JobStatus.PENDING


def login():
    user = User.objects.create_user("b@example.com", "pw-long-enough", "ADMIN")
    client = Client()
    client.force_login(user)
    return client


def test_week_unblocked_and_poll_shows_error(week, admin):
    old = pending(week, enqueue=False)
    text = login().get(STATUS).content.decode()
    assert ERROR_LOST_JOB in text and "Genera orario" in text
    assert 'hx-trigger="every 2s"' not in text
    assert state(old) == JobStatus.FAILED
    with override_settings(TASKS=DB_TASKS):
        new = enqueue_generation(MON, admin)
    assert new.pk != old.pk and new.status == JobStatus.PENDING
    assert DBTaskResult.objects.filter(args_kwargs__args__0=new.pk).exists()


@override_settings(TASKS=DB_TASKS)
@pytest.mark.django_db(transaction=True)
def test_worker_start_fails_orphan_and_processes_fresh(week, admin):
    orphan = pending(week, enqueue=False, week_start=OTHER_MON)
    fresh = enqueue_generation(MON, admin)
    call_command("solve_worker", "--batch", "--no-startup-delay", "--interval", "0")
    assert state(orphan) == JobStatus.FAILED
    assert state(fresh) == JobStatus.DONE


def test_scope_and_mixed_count(school):
    other_school = School.objects.create(
        name="O",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2026, 12, 21),
    )
    mine = pending(school, enqueue=False)
    other_week = pending(school, enqueue=False, week_start=OTHER_MON)
    other = pending(other_school, enqueue=False)
    assert fail_stale_jobs(school, MON) == 1
    assert state(mine) == JobStatus.FAILED
    assert state(other_week) == state(other) == JobStatus.PENDING
    running = SolveJob.objects.create(
        school=school,
        week_start=D(2026, 10, 26),
        status=JobStatus.RUNNING,
        started_at=timezone.now() - limit() - SEC,
    )
    assert fail_stale_jobs() == 3
    assert state(running) == JobStatus.FAILED


def test_lost_race_is_skipped(school):
    job = pending(school, enqueue=False)
    real = SolveJob.transition_to

    def racing(self, status, **kw):
        SolveJob.objects.filter(pk=self.pk).update(status=JobStatus.RUNNING)
        return real(self, status, **kw)

    with mock.patch.object(SolveJob, "transition_to", racing):
        assert fail_stale_jobs() == 0
    assert state(job) == JobStatus.RUNNING


def test_touches_only_the_job_row(week):
    Timetable.objects.create(school=week, week_start=MON)
    lost = pending(week, enqueue=False)
    kept = pending(week, week_start=OTHER_MON)
    rows = list(DBTaskResult.objects.order_by("pk").values())
    counts = (Timetable.objects.get().version, Slot.objects.count())
    audit = AuditLog.objects.count()
    assert fail_stale_jobs() == 1
    assert list(DBTaskResult.objects.order_by("pk").values()) == rows
    assert (Timetable.objects.get().version, Slot.objects.count()) == counts
    assert AuditLog.objects.count() == audit
    assert lost.pk and kept.pk

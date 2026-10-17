"""Recovery of solve jobs left RUNNING by a killed worker (#57)."""

import datetime
from types import SimpleNamespace
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from accounts.models import User
from config.env import DEFAULT_SOLVER_STALE_MARGIN_SECONDS, get_positive_int
from scheduling import alternatives, repair, tasks
from scheduling.jobs import ERROR_STALE_JOB, enqueue_generation, fail_stale_jobs
from scheduling.models import (
    AuditLog,
    JobKind,
    JobStatus,
    Slot,
    SolveJob,
    Timetable,
)
from scheduling.solver import SolveResult, SolveStatus
from school.models import Assignment, CalendarEntry, Class, School, Subject, Teacher

D = datetime.date
MON = D(2026, 10, 12)
OTHER_MON = D(2026, 10, 19)
STATUS = f"/orario/{MON:%Y-%m-%d}/stato/"
GENERATE = f"/orario/{MON:%Y-%m-%d}/genera/"
DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend"}}
NAME = "SOLVER_STALE_MARGIN_SECONDS"


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


def make(school, status=JobStatus.RUNNING, age=None, week_start=MON, **kw):
    """A job whose started_at (or created_at when started_at=None) is `age` ago."""
    job = SolveJob.objects.create(
        school=school, week_start=week_start, status=status, **kw
    )
    if age is not None:
        job.started_at = timezone.now() - age
        job.save(update_fields=["started_at"])
    return job


def stale(school, **kw):
    return make(school, age=limit() + datetime.timedelta(seconds=1), **kw)


# ---- config ---------------------------------------------------------------


def test_margin_default_and_value():
    assert get_positive_int({}, NAME, DEFAULT_SOLVER_STALE_MARGIN_SECONDS) == 120
    assert get_positive_int({NAME: "300"}, NAME, 120) == 300
    assert get_positive_int({NAME: ""}, NAME, 120) == 120


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "1.5"])
def test_bad_margin_fails(raw):
    with pytest.raises(ImproperlyConfigured, match=NAME):
        get_positive_int({NAME: raw}, NAME, 120)


def test_margin_setting_exists(settings):
    assert settings.SOLVER_STALE_MARGIN_SECONDS == 120


# ---- fail_stale_jobs ------------------------------------------------------


def test_stale_job_is_failed(school):
    job = stale(school)
    assert fail_stale_jobs() == 1
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error == ERROR_STALE_JOB and "Traceback" not in job.error
    assert job.finished_at is not None


def test_boundaries(school):
    now = timezone.now()
    exact = make(school)
    SolveJob.objects.filter(pk=exact.pk).update(started_at=now - limit())
    inside = make(school, week_start=OTHER_MON)
    SolveJob.objects.filter(pk=inside.pk).update(
        started_at=now - limit() + datetime.timedelta(seconds=1)
    )
    fresh = make(school, week_start=D(2026, 10, 26), age=datetime.timedelta())
    assert fail_stale_jobs(now=now) == 0
    assert set(SolveJob.objects.values_list("status", flat=True)) == {JobStatus.RUNNING}
    assert fresh.pk and fail_stale_jobs(now=now + datetime.timedelta(seconds=1)) == 1


def test_threshold_follows_settings(school):
    make(school, age=datetime.timedelta(seconds=100))
    assert fail_stale_jobs() == 0
    with override_settings(SOLVER_TIMEOUT_SECONDS=50, SOLVER_STALE_MARGIN_SECONDS=10):
        assert fail_stale_jobs() == 1


def test_created_at_fallback(school):
    old = make(school)  # started_at null
    SolveJob.objects.filter(pk=old.pk).update(
        created_at=timezone.now() - limit() - datetime.timedelta(seconds=1)
    )
    fresh = make(school, week_start=OTHER_MON)
    assert fail_stale_jobs() == 1
    old.refresh_from_db()
    fresh.refresh_from_db()
    assert old.status == JobStatus.FAILED and fresh.status == JobStatus.RUNNING


def test_other_states_untouched(school):
    old = limit() * 10
    pending = make(school, JobStatus.PENDING)
    SolveJob.objects.filter(pk=pending.pk).update(created_at=timezone.now() - old)
    done = make(school, JobStatus.DONE, age=old, week_start=OTHER_MON, result={"a": 1})
    failed = make(
        school, JobStatus.FAILED, age=old, week_start=D(2026, 10, 26), error="x"
    )
    before = list(SolveJob.objects.order_by("pk").values())
    assert fail_stale_jobs() == 0
    assert list(SolveJob.objects.order_by("pk").values()) == before
    assert done.pk and failed.pk


def test_all_kinds_are_failed(school):
    stale(school, kind=JobKind.REPAIR)
    stale(school, params={tasks.ALTERNATIVES_KEY: True}, week_start=OTHER_MON)
    assert fail_stale_jobs() == 2
    assert not SolveJob.objects.filter(status=JobStatus.RUNNING).exists()


def test_scope(school):
    other_school = School.objects.create(
        name="O",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2026, 12, 21),
    )
    mine = stale(school)
    other_week = stale(school, week_start=OTHER_MON)
    other = stale(other_school)
    assert fail_stale_jobs(school, MON) == 1
    for j in (other_week, other):
        j.refresh_from_db()
        assert j.status == JobStatus.RUNNING
    mine.refresh_from_db()
    assert mine.status == JobStatus.FAILED
    assert fail_stale_jobs() == 2


def test_lost_race_is_skipped(school):
    job = stale(school)
    real = SolveJob.transition_to

    def racing(self, status, **kw):
        SolveJob.objects.filter(pk=self.pk).update(status=JobStatus.DONE)
        return real(self, status, **kw)

    with mock.patch.object(SolveJob, "transition_to", racing):
        assert fail_stale_jobs() == 0
    job.refresh_from_db()
    assert job.status == JobStatus.DONE


def test_touches_only_the_job_row(week, admin):
    tt = Timetable.objects.create(school=week, week_start=MON)
    stale(week)
    state = (
        Timetable.objects.get().version,
        Slot.objects.count(),
        AuditLog.objects.count(),
    )
    fail_stale_jobs()
    assert state == (
        Timetable.objects.get().version,
        Slot.objects.count(),
        AuditLog.objects.count(),
    )
    assert tt.pk


# ---- where it runs --------------------------------------------------------


def login():
    user = User.objects.create_user("b@example.com", "pw-long-enough", "ADMIN")
    client = Client()
    client.force_login(user)
    return client


def test_poll_recovers_and_offers_generate(week):
    old = stale(week)
    resp = login().get(STATUS)
    text = resp.content.decode()
    assert ERROR_STALE_JOB in text and "Genera orario" in text
    assert 'hx-trigger="every 2s"' not in text
    old.refresh_from_db()
    assert old.status == JobStatus.FAILED


def test_generate_after_recovery_creates_new_job(week):
    old = stale(week)
    with override_settings(TASKS=DB_TASKS):
        resp = login().post(GENERATE)
    assert resp.status_code == 200
    assert SolveJob.objects.filter(status=JobStatus.PENDING).count() == 1
    old.refresh_from_db()
    assert old.status == JobStatus.FAILED


def test_enqueue_recovers_without_a_poll(week, admin):
    old = stale(week)
    with override_settings(TASKS=DB_TASKS):
        new = enqueue_generation(MON, admin)
    assert new.pk != old.pk and new.status == JobStatus.PENDING
    old.refresh_from_db()
    assert old.status == JobStatus.FAILED


def test_poll_of_a_fresh_job_is_unchanged(week):
    job = make(week, age=datetime.timedelta())
    text = login().get(STATUS).content.decode()
    assert "Generazione in corso" in text
    job.refresh_from_db()
    assert job.status == JobStatus.RUNNING


@override_settings(TASKS=DB_TASKS)
@pytest.mark.django_db(transaction=True)
def test_worker_start_recovers_and_still_processes(week, admin):
    old = stale(week, week_start=OTHER_MON)
    pending = enqueue_generation(MON, admin)
    call_command("solve_worker", "--batch", "--no-startup-delay", "--interval", "0")
    old.refresh_from_db()
    pending.refresh_from_db()
    assert old.status == JobStatus.FAILED
    assert pending.status == JobStatus.DONE
    assert DBTaskResult.objects.exists()


# ---- late finisher --------------------------------------------------------


def fail_meanwhile(job, then):
    def _inner(*args, **kw):
        SolveJob.objects.filter(pk=job.pk).update(
            status=JobStatus.FAILED, error=ERROR_STALE_JOB
        )
        return then

    return _inner


def assert_untouched(job):
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED and job.error == ERROR_STALE_JOB
    assert not Timetable.objects.exists()
    assert not Slot.objects.exists()
    assert not AuditLog.objects.exists()


def test_late_generate_does_not_raise(week):
    job = SolveJob.objects.create(school=week, week_start=MON)
    result = SolveResult(SolveStatus.OPTIMAL)
    with mock.patch.object(
        tasks, "solve_exclusive", side_effect=fail_meanwhile(job, result)
    ):
        tasks.run_job(job.pk)
    assert_untouched(job)


def test_late_generate_failure_does_not_raise(week):
    job = SolveJob.objects.create(school=week, week_start=MON)
    unknown = SolveResult(SolveStatus.UNKNOWN)
    with mock.patch.object(
        tasks, "solve_exclusive", side_effect=fail_meanwhile(job, unknown)
    ):
        tasks.run_job(job.pk)
    assert_untouched(job)


def test_late_repair_does_not_raise(week):
    job = SolveJob.objects.create(school=week, week_start=MON, kind=JobKind.REPAIR)
    with mock.patch.object(
        repair, "_solve_repair", side_effect=fail_meanwhile(job, {"diff": []})
    ):
        repair.run_repair(job.pk)
    assert_untouched(job)


def test_late_alternatives_does_not_raise(week):
    job = SolveJob.objects.create(
        school=week, week_start=MON, params={tasks.ALTERNATIVES_KEY: True}
    )
    outcome = SimpleNamespace(alternatives=[object()], first_status=None)
    with (
        mock.patch.object(alternatives, "_base_version", return_value=1),
        mock.patch.object(
            alternatives,
            "generate_alternatives",
            side_effect=fail_meanwhile(job, outcome),
        ),
    ):
        tasks.run_job(job.pk)
    assert_untouched(job)

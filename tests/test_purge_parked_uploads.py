import datetime
from io import StringIO
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from accounts.models import User
from config.env import (
    DEFAULT_SETUP_UPLOAD_MAX_AGE_HOURS,
    get_positive_int,
)
from scheduling.models import AuditLog, Timetable, TimetableFeedback
from school import setup_service
from school.models import Class, ParkedUpload, School, Teacher
from tests.test_setup_replace import old  # noqa: F401
from tests.test_setup_wizard import (  # noqa: F401
    admin,
    correct,
    html,
    upload,
    upload_all,
)

NOW = datetime.datetime(2026, 10, 17, 12, 0, tzinfo=datetime.UTC)
TYPES = ["calendar", "teachers", "classes", "subjects", "assignments", "pairings"]


def park(file_type, age_hours=0, seconds=0):
    row = ParkedUpload.objects.create(file_type=file_type, source="csv", filename="f")
    when = NOW - datetime.timedelta(hours=age_hours, seconds=seconds)
    ParkedUpload.objects.filter(pk=row.pk).update(updated_at=when)
    return row


def run(*args):
    out = StringIO()
    with mock.patch("django.utils.timezone.now", return_value=NOW):
        call_command("purge_parked_uploads", *args, stdout=out)
    return out.getvalue()


def types():
    return set(ParkedUpload.objects.values_list("file_type", flat=True))


@pytest.mark.django_db
def test_default_age_deletes_only_stale_rows():
    for t, age in zip(TYPES, [100, 73, 71, 0], strict=False):
        park(t, age)
    out = run()
    assert "Deleted 2 " in out
    assert types() == {"classes", "subjects"}


@pytest.mark.django_db
def test_boundary_is_kept_one_second_older_is_deleted():
    park("calendar", 72)
    park("teachers", 72, seconds=1)
    run()
    assert types() == {"calendar"}


@pytest.mark.django_db
def test_setting_and_option_change_cutoff():
    park("calendar", 30)
    with override_settings(SETUP_UPLOAD_MAX_AGE_HOURS=24):
        assert "Deleted 1 " in run()
    assert types() == set()
    park("calendar", 5)
    assert "Deleted 0 " in run()
    with override_settings(SETUP_UPLOAD_MAX_AGE_HOURS=24):
        assert "Deleted 1 " in run("--max-age-hours", "1")


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["0", "-3", "abc", ""])
def test_invalid_age_option_is_refused(value):
    park("calendar", 500)
    with pytest.raises(CommandError):
        run("--max-age-hours", value)
    assert types() == {"calendar"}


@pytest.mark.django_db
def test_dry_run_reports_and_deletes_nothing():
    park("calendar", 100)
    park("teachers", 100)
    park("classes", 1)
    out = run("--dry-run")
    assert "Would delete 2 " in out
    assert "calendar" in out and "teachers" in out and "classes" not in out
    assert len(types()) == 3


@pytest.mark.django_db
def test_nothing_stale_and_empty_table():
    assert "0 parked" in run()
    park("calendar", 1)
    assert "0 parked" in run()
    assert types() == {"calendar"}


def test_env_parsing():
    name = "SETUP_UPLOAD_MAX_AGE_HOURS"
    assert get_positive_int({}, name, DEFAULT_SETUP_UPLOAD_MAX_AGE_HOURS) == 72
    assert get_positive_int({name: "48"}, name, 72) == 48
    for bad in ["0", "-5", "abc"]:
        with pytest.raises(ImproperlyConfigured):
            get_positive_int({name: bad}, name, 72)
    # Blank means unset for every positive-int setting (compose passes empty vars).
    assert get_positive_int({name: ""}, name, 72) == 72
    text = open(".env.example").read()
    assert name in text
    line = next(
        i for i, ln in enumerate(text.splitlines()) if ln.startswith(name + "=")
    )
    comment = text.splitlines()[line - 1]
    assert "default 72" in comment and "hours" in comment


def test_activity_refreshes_updated_at(admin):  # noqa: F811
    upload_all(admin)
    long_ago = timezone.now() - datetime.timedelta(hours=500)
    ParkedUpload.objects.update(updated_at=long_ago)
    assert correct(admin, "teachers", 2, "nome", "Nuovo Nome").status_code == 200
    setup_service.add_manual_date("2026-10-20", "6", False, "")
    assert ParkedUpload.objects.get(file_type="teachers").updated_at > long_ago
    assert ParkedUpload.objects.get(file_type="calendar").updated_at > long_ago
    call_command("purge_parked_uploads", stdout=StringIO())
    assert types() == {"teachers", "calendar"}


def test_only_parked_uploads_are_removed(old):  # noqa: F811
    park("calendar", 100)
    counts = lambda: [  # noqa: E731
        m.objects.count()
        for m in (School, Class, Teacher, User, Timetable, AuditLog, TimetableFeedback)
    ]
    before = counts()
    assert min(before) > 0
    run()
    assert types() == set()
    assert counts() == before


@pytest.mark.django_db
def test_purge_takes_the_lock_inside_the_transaction():
    park("calendar", 100)
    calls = []
    real = setup_service.connection.cursor

    def spy():
        cursor = real()
        orig = cursor.execute

        def execute(sql, params=None):
            if "pg_advisory_xact_lock" in sql:
                calls.append((params, setup_service.connection.in_atomic_block))
            return orig(sql, params)

        cursor.execute = execute
        return cursor

    with mock.patch.object(setup_service.connection, "cursor", spy):
        run()
    assert calls == [([setup_service.SETUP_LOCK_ID], True)]


@pytest.mark.django_db
def test_failure_partway_keeps_every_row():
    park("calendar", 100)
    park("teachers", 100)
    with mock.patch(
        "django.db.models.query.QuerySet.delete", side_effect=RuntimeError("boom")
    ):
        with pytest.raises(RuntimeError):
            run()
    assert types() == {"calendar", "teachers"}


def test_wizard_works_after_purge(admin):  # noqa: F811
    upload_all(admin)
    ParkedUpload.objects.update(
        updated_at=timezone.now() - datetime.timedelta(hours=500)
    )
    call_command("purge_parked_uploads", stdout=StringIO())
    assert ParkedUpload.objects.count() == 0
    assert admin.get("/setup/").status_code == 200
    assert upload(admin, "teachers", "x").status_code in (200, 422)

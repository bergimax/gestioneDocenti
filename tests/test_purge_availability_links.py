import datetime
from io import StringIO
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings

from accounts.models import User
from availability import links
from availability.models import Availability, AvailabilityLink, LinkLookupFailure
from config.env import DEFAULT_AVAILABILITY_LINK_PURGE_DAYS, get_positive_int
from ledger.models import EntryKind, LedgerEntry
from scheduling.models import AuditLog, TimetableFeedback
from school.models import Assignment, Class, School, Subject, Teacher
from tests.test_setup_replace import old  # noqa: F401

NOW = datetime.datetime(2026, 10, 17, 12, 0, tzinfo=datetime.UTC)
D = datetime.date
DAY = datetime.timedelta(days=1)
N = [0]


@pytest.fixture
def teacher(db):
    school = School.objects.create(
        name="S",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )
    return Teacher.objects.create(
        school=school, name="ROSSI", type="CONTRATTO", first_time=False
    )


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(links, "now", lambda: NOW)


def mk(teacher, expired=None, revoked=None, used=False, active=False):
    """A link; `expired`/`revoked` are ages as timedeltas before NOW."""
    N[0] += 1
    return AvailabilityLink.objects.create(
        teacher=teacher,
        week_start=D(2026, 9, 7) + datetime.timedelta(days=7 * N[0]),
        token_hash=f"{N[0]:064d}",
        expires_at=NOW - expired if expired is not None else NOW + 5 * DAY,
        revoked_at=NOW - revoked if revoked is not None else None,
        used_at=NOW - 100 * DAY if used else None,
    )


def fail(age_seconds, address="1.2.3.4"):
    return LinkLookupFailure.objects.create(
        client_address=address,
        attempted_at=NOW - datetime.timedelta(seconds=age_seconds),
    )


def run(*args):
    out = StringIO()
    call_command("purge_availability_links", *args, stdout=out)
    return out.getvalue()


def ids():
    return set(AvailabilityLink.objects.values_list("pk", flat=True))


def test_default_30_days(teacher):
    a = mk(teacher, expired=31 * DAY)
    b = mk(teacher, expired=29 * DAY)
    c = mk(teacher, revoked=31 * DAY, expired=-5 * DAY)
    d = mk(teacher, revoked=29 * DAY, expired=-5 * DAY)
    e = mk(teacher)
    out = run()
    assert out == (
        "Deleted 2 availability link(s) and 0 lookup failure(s) older than 30 days.\n"
    )
    assert ids() == {b.pk, d.pk, e.pk}
    assert a.pk not in ids() and c.pk not in ids()


@pytest.mark.django_db
def test_boundary(teacher):
    keep = mk(teacher, expired=30 * DAY)
    gone = mk(teacher, expired=30 * DAY + datetime.timedelta(seconds=1))
    run()
    assert ids() == {keep.pk}
    assert gone.pk not in ids()


def test_dead_since_edge_cases(teacher):
    revoked_future = mk(teacher, revoked=40 * DAY)
    expired_then_revoked = mk(teacher, expired=40 * DAY, revoked=1 * DAY)
    revoked_old_expired_recent = mk(teacher, revoked=40 * DAY, expired=1 * DAY)
    active_old = mk(teacher)
    AvailabilityLink.objects.filter(pk=active_old.pk).update(created_at=NOW - 400 * DAY)
    used_old = mk(teacher, expired=40 * DAY, used=True)
    unused_old = mk(teacher, expired=40 * DAY)
    used_recent = mk(teacher, expired=1 * DAY, used=True)
    run()
    assert ids() == {active_old.pk, used_recent.pk}
    for gone in (
        revoked_future,
        expired_then_revoked,
        revoked_old_expired_recent,
        used_old,
        unused_old,
    ):
        assert gone.pk not in ids()


def test_lookup_failures_and_rate_limit(teacher):
    window = 300
    for _ in range(10):
        fail(10)
    fail(window + 1)
    fail(window + 1000, address="9.9.9.9")
    fail(window - 1, address="9.9.9.9")
    with (
        mock.patch.object(links, "now", lambda: NOW),
        mock.patch("django.utils.timezone.now", return_value=NOW),
    ):
        before = links.is_rate_limited("1.2.3.4")
        out = run()
        after = links.is_rate_limited("1.2.3.4")
    assert before is after is True
    assert "and 2 lookup failure(s)" in out
    assert LinkLookupFailure.objects.count() == 11


def test_dry_run(teacher):
    mk(teacher, expired=40 * DAY)
    mk(teacher)
    fail(10000)
    out = run("--dry-run")
    assert out.startswith("Would delete 1 availability link(s) and 1 lookup ")
    assert AvailabilityLink.objects.count() == 2
    assert LinkLookupFailure.objects.count() == 1
    assert run().startswith("Deleted 1 availability link(s) and 1 lookup ")


def test_precedence(teacher):
    mk(teacher, expired=10 * DAY)
    assert "Deleted 1 " in run("--days", "7")
    mk(teacher, expired=10 * DAY)
    with override_settings(AVAILABILITY_LINK_PURGE_DAYS=15):
        assert "Deleted 0 " in run()
    with override_settings(AVAILABILITY_LINK_PURGE_DAYS=15):
        assert "Deleted 1 " in run("--days", "7")
    mk(teacher, expired=10 * DAY)
    with override_settings(AVAILABILITY_LINK_PURGE_DAYS=5):
        out = run()
    assert "Deleted 1 " in out and "older than 5 days" in out


@pytest.mark.parametrize("value", ["0", "-3", "abc", ""])
def test_invalid_days_option(teacher, value):
    mk(teacher, expired=500 * DAY)
    fail(100000)
    with pytest.raises(CommandError):
        run("--days", value)
    assert AvailabilityLink.objects.count() == 1
    assert LinkLookupFailure.objects.count() == 1


def test_env_parsing():
    name = "AVAILABILITY_LINK_PURGE_DAYS"
    assert get_positive_int({}, name, DEFAULT_AVAILABILITY_LINK_PURGE_DAYS) == 30
    assert get_positive_int({name: ""}, name, 30) == 30
    assert get_positive_int({name: "45"}, name, 30) == 45
    for bad in ["0", "-5", "abc"]:
        with pytest.raises(ImproperlyConfigured, match=name):
            get_positive_int({name: bad}, name, 30)
    lines = open(".env.example").read().splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.startswith(name + "="))
    assert "default 30" in lines[i - 1] and "days" in lines[i - 1]


@pytest.mark.django_db
def test_nothing_stale_and_empty_tables():
    assert run() == (
        "Deleted 0 availability link(s) and 0 lookup failure(s) older than 30 days.\n"
    )


def test_append_only_data_untouched(old):  # noqa: F811
    user = User.objects.get(email="admin@example.com")
    school = old["school"]
    teacher = Teacher.objects.get(school=school)
    LedgerEntry.objects.create(
        assignment=Assignment.objects.first(), hours=2, kind=EntryKind.DELIVERED
    )
    link = mk(teacher, expired=90 * DAY)
    AvailabilityLink.objects.filter(pk=link.pk).update(created_by=user)
    models = (
        AuditLog,
        LedgerEntry,
        TimetableFeedback,
        Teacher,
        Availability,
        User,
        School,
        Class,
        Subject,
    )

    def snap():
        return {m: list(m.objects.order_by("pk").values()) for m in models}

    before = snap()
    assert before[AuditLog] and before[LedgerEntry] and before[TimetableFeedback]
    assert "Deleted 1 " in run()
    assert not AvailabilityLink.objects.filter(pk=link.pk).exists()
    assert snap() == before


def test_output_has_no_secrets(teacher):
    link, token = links.create_link(teacher, D(2026, 9, 7), None)
    AvailabilityLink.objects.filter(pk=link.pk).update(expires_at=NOW - 90 * DAY)
    for args in ((), ("--dry-run",)):
        out = run(*args)
        assert token not in out
        assert link.token_hash not in out
        assert "ROSSI" not in out
        assert str(link.teacher_id) not in out.replace("30", "")


@pytest.mark.django_db
def test_takes_setup_lock_inside_transaction():
    from school import setup_service

    calls = []
    real = links.connection.cursor

    def spy():
        cursor = real()
        orig = cursor.execute

        def execute(sql, params=None):
            if "pg_advisory_xact_lock" in sql:
                calls.append((params, links.connection.in_atomic_block))
            return orig(sql, params)

        cursor.execute = execute
        return cursor

    with mock.patch.object(links.connection, "cursor", spy):
        run()
    assert calls == [([setup_service.SETUP_LOCK_ID], True)]


def test_purged_token_behaves_as_unknown(teacher):
    link, token = links.create_link(teacher, D(2026, 9, 7), None)
    AvailabilityLink.objects.filter(pk=link.pk).update(revoked_at=NOW - 40 * DAY)
    unknown = "A" * 43
    c = Client()
    before = c.get(f"/l/{token}/")
    run()
    assert links.lookup_link(token) is None
    after = c.get(f"/l/{token}/")
    ref = Client().get(f"/l/{unknown}/")
    assert after.status_code == ref.status_code == before.status_code
    assert after.content == ref.content
    assert LinkLookupFailure.objects.count() >= 1

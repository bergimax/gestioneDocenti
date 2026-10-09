import datetime
import threading
from unittest import mock

import pytest
from django.db import connection

from scheduling.models import Slot, Timetable
from scheduling.services import (
    ForbiddenTimetableChange,
    TimetableLocked,
    apply_change,
)
from school.models import Class, School, Subject, Teacher

D = datetime.date


@pytest.fixture
def tt(db):
    school = School.objects.create(
        name="A",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )
    t = Timetable.objects.create(school=school, week_start=D(2026, 9, 7))
    t.cls = Class.objects.create(school=school, name="1A")
    t.teacher = Teacher.objects.create(school=school, name="ROSSI", type="ASSUNTO")
    t.subject = Subject.objects.create(
        school=school, name="MAT", kind="TEORIA", cognitive_weight="ALTO"
    )
    return t


def add_slot(t, hour=8, day="LUNEDI"):
    return Slot.objects.create(
        timetable=t,
        school_class=Class.objects.get(school_id=t.school_id),
        teacher=Teacher.objects.get(school_id=t.school_id),
        subject=Subject.objects.get(school_id=t.school_id),
        day=day,
        start_hour=hour,
        end_hour=hour + 1,
    )


def db_version(t):
    return Timetable.objects.get(pk=t.pk).version


def test_sequential_calls(tt):
    for n in range(1, 6):
        res = apply_change(tt.pk, lambda t: None)
        assert res.version == n
        assert isinstance(res, Timetable)
    assert db_version(tt) == 5


def test_noop_change_bumps_once(tt):
    apply_change(tt.pk, lambda t: None)
    assert db_version(tt) == 1


def test_many_edits_bump_once(tt):
    s1, s2 = add_slot(tt, 8), add_slot(tt, 9)

    def change(t):
        add_slot(t, 10)
        add_slot(t, 11)
        Slot.objects.filter(pk=s1.pk).update(day="MARTEDI")
        s2.delete()

    res = apply_change(tt.pk, change)
    assert res.version == 1
    assert Slot.objects.filter(timetable=tt).count() == 3


def test_change_may_write_score(tt):
    def change(t):
        t.quality_score = 80
        t.quality_level = "A"
        t.soft_conflicts = 2
        t.save()

    res = apply_change(tt.pk, change)
    assert (res.quality_score, res.quality_level, res.soft_conflicts) == (80, "A", 2)
    assert res.version == 1


def test_failing_change_rolls_back(tt):
    def change(t):
        add_slot(t)
        raise KeyError("boom")

    with pytest.raises(KeyError):
        apply_change(tt.pk, change)
    assert Slot.objects.count() == 0
    assert db_version(tt) == 0


def test_approved_is_locked(tt):
    add_slot(tt)
    Timetable.objects.filter(pk=tt.pk).update(state="APPROVATO", version=3)
    spy = mock.Mock()
    with pytest.raises(TimetableLocked):
        apply_change(tt.pk, spy)
    spy.assert_not_called()
    assert Slot.objects.count() == 1
    assert db_version(tt) == 3


def test_stale_instance_does_not_bypass_lock(tt):
    stale = Timetable.objects.get(pk=tt.pk)
    assert stale.state == "BOZZA"
    Timetable.objects.filter(pk=tt.pk).update(state="APPROVATO")
    spy = mock.Mock()
    with pytest.raises(TimetableLocked):
        apply_change(stale.pk, spy)
    spy.assert_not_called()
    assert db_version(tt) == 0


@pytest.mark.parametrize("field", ["state", "approved_at"])
def test_forbidden_change(tt, field):
    from django.utils import timezone

    def change(t):
        add_slot(t)
        if field == "state":
            Timetable.objects.filter(pk=t.pk).update(state="APPROVATO")
        else:
            Timetable.objects.filter(pk=t.pk).update(approved_at=timezone.now())

    with pytest.raises(ForbiddenTimetableChange):
        apply_change(tt.pk, change)
    fresh = Timetable.objects.get(pk=tt.pk)
    assert fresh.state == "BOZZA" and fresh.approved_at is None
    assert fresh.version == 0
    assert Slot.objects.count() == 0


def test_forbidden_change_via_instance_save(tt):
    def change(t):
        t.state = "APPROVATO"
        t.save()

    with pytest.raises(ForbiddenTimetableChange):
        apply_change(tt.pk, change)
    assert Timetable.objects.get(pk=tt.pk).state == "BOZZA"


def test_missing_timetable(db):
    spy = mock.Mock()
    with pytest.raises(Timetable.DoesNotExist):
        apply_change(999999, spy)
    spy.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_concurrent_changes():
    school = School.objects.create(
        name="A",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )
    t = Timetable.objects.create(school=school, week_start=D(2026, 9, 7))
    t.cls = Class.objects.create(school=school, name="1A")
    t.teacher = Teacher.objects.create(school=school, name="ROSSI", type="ASSUNTO")
    t.subject = Subject.objects.create(
        school=school, name="MAT", kind="TEORIA", cognitive_weight="ALTO"
    )
    n = 8
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker(i):
        try:
            barrier.wait(timeout=30)
            res = apply_change(
                t.pk, lambda locked: add_slot(locked, 8 + i % 6, "LUNEDI")
            )
            results.append(res.version)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    assert not errors
    assert sorted(results) == list(range(1, n + 1))
    assert db_version(t) == n
    assert Slot.objects.filter(timetable=t).count() == n

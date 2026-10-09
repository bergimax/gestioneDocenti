import datetime

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from availability import grid as g
from availability import services
from availability.models import Availability
from school.models import School, Teacher

D = datetime.date
W1, W2, W3, W4 = D(2026, 9, 7), D(2026, 9, 14), D(2026, 9, 21), D(2026, 9, 28)


@pytest.fixture
def school(db):
    return School.objects.create(
        name="A",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2026, 9, 28),
    )


@pytest.fixture
def other_school(db):
    return School.objects.create(
        name="B",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )


def teacher(school, name="ROSSI", **kw):
    kw.setdefault("type", "ASSUNTO")
    kw.setdefault("first_time", False)
    return Teacher.objects.create(school=school, name=name, **kw)


def grid_off(day="lunedi"):
    grid = g.default_grid()
    grid[day][0]["disponibile"] = False
    return grid


def make(t, week, grid=None):
    return Availability.objects.create(
        school=t.school, teacher=t, week_start=week, slots=grid or g.default_grid()
    )


def test_registered_in_admin():
    assert Availability in admin.site._registry


def test_valid_record_passes_full_clean(school):
    t = teacher(school)
    Availability(
        school=school, teacher=t, week_start=W1, slots=g.default_grid()
    ).full_clean()


def test_non_monday_rejected(school):
    t = teacher(school)
    a = Availability(
        school=school, teacher=t, week_start=D(2026, 9, 8), slots=g.default_grid()
    )
    with pytest.raises(ValidationError) as exc:
        a.full_clean()
    assert "week_start" in exc.value.message_dict


def test_invalid_grid_rejected(school):
    t = teacher(school)
    a = Availability(school=school, teacher=t, week_start=W1, slots={"lunedi": []})
    with pytest.raises(ValidationError) as exc:
        a.full_clean()
    assert "slots" in exc.value.message_dict


def test_teacher_from_other_school_rejected(school, other_school):
    t = teacher(other_school)
    a = Availability(school=school, teacher=t, week_start=W1, slots=g.default_grid())
    with pytest.raises(ValidationError) as exc:
        a.full_clean()
    assert "teacher" in exc.value.message_dict


def test_unique_teacher_week(school):
    t, t2 = teacher(school), teacher(school, "BIANCHI")
    make(t, W1)
    make(t, W2)
    make(t2, W1)
    with pytest.raises(IntegrityError), transaction.atomic():
        make(t, W1)


def test_service_stored(school):
    t = teacher(school, first_time=True)
    make(t, W1)
    make(t, W2, grid_off())
    r = services.resolve_teacher_grid(t, W2)
    assert r.source == g.STORED and r.grid == grid_off()


def test_service_first_time_default(school):
    t = teacher(school, first_time=True)
    make(t, W1, grid_off())
    r = services.resolve_teacher_grid(t, W2)
    assert r.source == g.DEFAULT and r.grid == g.default_grid()


def test_service_inherits_latest_earlier(school):
    t = teacher(school)
    make(t, W1, grid_off("lunedi"))
    make(t, W2, grid_off("martedi"))
    make(t, W4, grid_off("venerdi"))
    r = services.resolve_teacher_grid(t, W3)
    assert (r.source, r.from_week) == (g.INHERITED, W2)
    assert r.grid == grid_off("martedi")


@pytest.mark.parametrize("later", [False, True])
def test_service_default(school, later):
    t = teacher(school)
    if later:
        make(t, W3)
    r = services.resolve_teacher_grid(t, W2)
    assert r.source == g.DEFAULT and r.from_week is None


def test_service_never_writes_and_returns_copy(school):
    t = teacher(school)
    a = make(t, W1, grid_off())
    before = list(Availability.objects.values_list("pk", "updated_at"))
    for week in (W1, W2, W3):
        r = services.resolve_teacher_grid(t, week)
        r.grid["lunedi"].clear()
    t2 = teacher(school, "NUOVO", first_time=True)
    services.resolve_teacher_grid(t2, W1)
    assert list(Availability.objects.values_list("pk", "updated_at")) == before
    a.refresh_from_db()
    assert a.slots == grid_off()
    assert services.resolve_teacher_grid(t, W2).grid == grid_off()


def test_service_non_monday(school):
    with pytest.raises(ValueError):
        services.resolve_teacher_grid(teacher(school), D(2026, 9, 8))


def test_service_malformed_stored_grid_does_not_raise(school):
    t = teacher(school)
    Availability.objects.create(school=school, teacher=t, week_start=W1, slots=[1])
    r = services.resolve_teacher_grid(t, W2)
    assert r.source == g.INHERITED and r.errors


def test_status_counts(school, other_school):
    a, b, c = teacher(school, "B"), teacher(school, "A"), teacher(school, "C")
    teacher(school, "INATTIVO", active=False)
    teacher(school, "ASSENTE", active=False)
    x = teacher(other_school, "X")
    make(a, W1)
    make(x, W1)
    make(c, W2)  # other week: does not count
    s = services.week_status(school, W1)
    assert s["total_teachers"] == 3
    assert s["teachers_with_availability"] == 1
    assert [t.name for t in s["teachers_missing"]] == ["A", "C"]
    assert s["completion_percentage"] == 33.3
    assert s["week_complete"] is False and s["ready_to_generate"] is False
    assert b in s["teachers_missing"]


def test_status_inactive_record_not_counted(school):
    t = teacher(school, active=False)
    make(t, W1)
    s = services.week_status(school, W1)
    assert s["total_teachers"] == 0 and s["teachers_with_availability"] == 0


def test_status_inherited_not_counted_and_complete(school):
    t = teacher(school)
    make(t, W1)
    assert services.week_status(school, W2)["teachers_with_availability"] == 0
    s = services.week_status(school, W1)
    assert s["week_complete"] is True and s["ready_to_generate"] is True
    assert s["completion_percentage"] == 100.0


def test_status_zero_teachers(school):
    s = services.week_status(school, W1)
    assert s["total_teachers"] == 0
    assert s["completion_percentage"] == 0
    assert s["week_complete"] is False and s["ready_to_generate"] is False
    assert s["teachers_missing"] == []


def test_school_weeks(school):
    weeks = services.school_weeks(school)
    assert [w["start"] for w in weeks] == [W1, W2, W3, W4]


def test_save_creates_then_updates(school):
    t = teacher(school)
    a = services.save_availability(t, W1, g.default_grid())
    assert a.school_id == school.pk
    services.save_availability(t, W1, grid_off())
    assert Availability.objects.count() == 1
    assert Availability.objects.get().slots == grid_off()


def test_save_invalid_grid_writes_nothing(school):
    t = teacher(school)
    with pytest.raises(ValidationError):
        services.save_availability(t, W1, {"lunedi": []})
    assert Availability.objects.count() == 0
    make(t, W1)
    with pytest.raises(ValidationError):
        services.save_availability(t, W1, None)
    assert Availability.objects.get().slots == g.default_grid()


def test_save_non_monday_writes_nothing(school):
    t = teacher(school)
    with pytest.raises(ValueError):
        services.save_availability(t, D(2026, 9, 8), g.default_grid())
    assert Availability.objects.count() == 0

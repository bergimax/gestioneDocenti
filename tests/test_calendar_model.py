import datetime

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import Client

from accounts.models import User
from school.models import CalendarEntry, Class, School

D = datetime.date(2026, 10, 12)


def make_school(name):
    return School.objects.create(
        name=name,
        formative_year="2026-2027",
        year_start=datetime.date(2026, 9, 1),
        year_end=datetime.date(2027, 6, 30),
    )


@pytest.fixture
def school(db):
    return make_school("Scuola A")


@pytest.fixture
def c1(school):
    return Class.objects.create(school=school, name="1A")


@pytest.fixture
def c2(school):
    return Class.objects.create(school=school, name="1B")


def entry(school, **kw):
    kw.setdefault("date", D)
    return CalendarEntry(school=school, **kw)


def insert_fails(obj):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            obj.save()


def errors_of(obj):
    with pytest.raises(ValidationError) as e:
        obj.full_clean()
    return e.value.message_dict


def test_defaults(school):
    e = CalendarEntry.objects.create(school=school, date=D)
    e.refresh_from_db()
    assert (e.max_hours, e.is_closure, e.is_group_stage) == (6, False, False)
    assert e.group is None and e.start_hour_min is None and e.stage_class is None


@pytest.mark.parametrize("hours", [0, 8])
def test_max_hours_accepted(school, hours):
    entry(school, max_hours=hours).full_clean()


@pytest.mark.parametrize("hours", [-1, 9])
def test_max_hours_rejected(school, hours):
    assert "max_hours" in errors_of(entry(school, max_hours=hours))
    insert_fails(entry(school, max_hours=hours))


@pytest.mark.parametrize("hour", [8, 13, None])
def test_start_hour_accepted(school, hour):
    entry(school, start_hour_min=hour).full_clean()


@pytest.mark.parametrize("hour", [7, 14])
def test_start_hour_rejected(school, hour):
    assert "start_hour_min" in errors_of(entry(school, start_hour_min=hour))
    insert_fails(entry(school, start_hour_min=hour))


def test_group_values(school):
    for g in ("PRIME", "SECONDE", "TERZE", "QUARTE", "PRIMA4+2", None):
        entry(school, group=g).full_clean()
    assert "group" in errors_of(entry(school, group="QUINTE"))
    insert_fails(entry(school, group="QUINTE"))


def test_empty_group_rejected_everywhere(school):
    assert "group" in errors_of(entry(school, group=""))
    insert_fails(entry(school, group=""))


def test_unique_group_per_date(school):
    CalendarEntry.objects.create(school=school, date=D, group="PRIME")
    insert_fails(entry(school, group="PRIME"))


def test_unique_whole_school_per_date(school):
    CalendarEntry.objects.create(school=school, date=D)
    insert_fails(entry(school))


def test_different_groups_same_date_allowed(school):
    CalendarEntry.objects.create(school=school, date=D, group="PRIME")
    CalendarEntry.objects.create(school=school, date=D, group="TERZE")
    CalendarEntry.objects.create(school=school, date=D)


def test_same_date_group_other_school_allowed(school):
    other = make_school("Scuola B")
    CalendarEntry.objects.create(school=school, date=D, group="PRIME")
    CalendarEntry.objects.create(school=other, date=D, group="PRIME")
    CalendarEntry.objects.create(school=school, date=D)
    CalendarEntry.objects.create(school=other, date=D)


def test_class_stage_rows_coexist(school, c1, c2):
    CalendarEntry.objects.create(school=school, date=D, stage_class=c1, max_hours=0)
    CalendarEntry.objects.create(school=school, date=D, stage_class=c2, max_hours=0)
    CalendarEntry.objects.create(school=school, date=D)
    CalendarEntry.objects.create(school=school, date=D, group="PRIME")


def test_group_stage_needs_group(school):
    assert "is_group_stage" in errors_of(entry(school, is_group_stage=True))
    insert_fails(entry(school, is_group_stage=True))
    entry(school, is_group_stage=True, group="PRIME").full_clean()


def test_stage_class_and_group_stage_exclusive(school, c1):
    e = entry(school, group="PRIME", is_group_stage=True, stage_class=c1)
    assert "stage_class" in errors_of(e)
    insert_fails(e)


def test_stage_class_same_school(school, c1):
    other = make_school("Scuola B")
    foreign = Class.objects.create(school=other, name="9Z")
    assert "stage_class" in errors_of(entry(school, stage_class=foreign))
    entry(school, stage_class=c1).full_clean()


def test_protect_class(school, c1):
    CalendarEntry.objects.create(school=school, date=D, stage_class=c1)
    with pytest.raises(ProtectedError):
        c1.delete()


def test_protect_school(school):
    CalendarEntry.objects.create(school=school, date=D)
    with pytest.raises(ProtectedError):
        school.delete()


def test_admin_changelist_and_add_load(school):
    CalendarEntry.objects.create(school=school, date=D)
    User.objects.create_superuser("root@example.com", "pw-correct-1")
    c = Client()
    assert c.login(username="root@example.com", password="pw-correct-1")
    assert c.get("/admin/school/calendarentry/").status_code == 200
    assert c.get("/admin/school/calendarentry/add/").status_code == 200

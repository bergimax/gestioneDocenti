import datetime

import pytest

from school.assignment_hours import delivered_hours_map, set_assignment_total_hours
from school.models import Assignment, Class, School, Subject, Teacher


@pytest.fixture
def assignment(db):
    school = School.objects.create(
        name="S",
        formative_year="2026-2027",
        year_start=datetime.date(2026, 9, 1),
        year_end=datetime.date(2027, 6, 30),
    )
    return Assignment.objects.create(
        school=school,
        teacher=Teacher.objects.create(
            school=school, name="ROSSI MARIO", type="ASSUNTO"
        ),
        school_class=Class.objects.create(school=school, name="1A"),
        subject=Subject.objects.create(
            school=school, name="Mat", kind="TEORIA", cognitive_weight="ALTO"
        ),
        ore_totali=40,
    )


def test_delivered_map_is_empty_without_ledger_entries(assignment):
    assert delivered_hours_map(assignment.school) == {}


def test_delivered_map_sums_ledger_hours_in_one_query(
    assignment, django_assert_num_queries
):
    from ledger.models import LedgerEntry
    from scheduling.models import Timetable

    tt = Timetable.objects.create(
        school=assignment.school, week_start=datetime.date(2026, 10, 12)
    )
    for hours in (1, 1, 2):
        LedgerEntry.objects.create(
            assignment=assignment,
            timetable=tt,
            week=tt.week_start,
            hours=hours,
            kind="DELIVERED",
        )
    with django_assert_num_queries(1):
        assert delivered_hours_map(assignment.school) == {assignment.pk: 4}


def test_set_total_hours_saves_only_that_column(assignment):
    stale = Assignment.objects.get(pk=assignment.pk)
    result = set_assignment_total_hours(stale, 55)
    assert result is stale
    assert Assignment.objects.get(pk=assignment.pk).ore_totali == 55
    set_assignment_total_hours(stale, 0)
    assert Assignment.objects.get(pk=assignment.pk).ore_totali == 0

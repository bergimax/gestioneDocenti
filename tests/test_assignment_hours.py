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


def test_delivered_map_is_empty_and_runs_no_query(
    assignment, django_assert_num_queries
):
    with django_assert_num_queries(0):
        assert delivered_hours_map(assignment.school) == {}


def test_set_total_hours_saves_only_that_column(assignment):
    stale = Assignment.objects.get(pk=assignment.pk)
    result = set_assignment_total_hours(stale, 55)
    assert result is stale
    assert Assignment.objects.get(pk=assignment.pk).ore_totali == 55
    set_assignment_total_hours(stale, 0)
    assert Assignment.objects.get(pk=assignment.pk).ore_totali == 0

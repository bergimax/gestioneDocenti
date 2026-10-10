import ast
import datetime
from pathlib import Path

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from availability import grid as g
from availability.models import Availability
from scheduling import context as builder
from scheduling.context import build_week_context
from scheduling.solver.context import ScheduleContext
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
TUE = D(2026, 10, 13)
END = D(2026, 12, 21)  # 10 weeks after MON


@pytest.fixture
def school(db):
    return School.objects.create(
        name="A",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=END,
    )


@pytest.fixture
def subject(school):
    return Subject.objects.create(
        school=school, name="Mat", kind="TEORIA", cognitive_weight="MEDIO"
    )


@pytest.fixture
def klass(school):
    return make_class(school, "1A")


def make_class(school, name, group=""):
    return Class.objects.create(school=school, name=name, group=group)


def teacher(school, name="ROSSI", type="ASSUNTO", **kw):
    kw.setdefault("first_time", False)
    return Teacher.objects.create(school=school, name=name, type=type, **kw)


def assign(school, t, c, s, hours=20):
    return Assignment.objects.create(
        school=school, teacher=t, school_class=c, subject=s, ore_totali=hours
    )


def entry(school, date=MON, **kw):
    return CalendarEntry.objects.create(school=school, date=date, **kw)


def cells(*days_hours):
    return frozenset(days_hours)


def grid_with(off_day="lunedi"):
    grid = g.default_grid()
    for slot in grid[off_day]:
        slot["disponibile"] = False
    return grid


def save(t, week, grid):
    return Availability.objects.create(
        school=t.school, teacher=t, week_start=week, slots=grid
    )


# --- shape ----------------------------------------------------------------


def test_returns_schedule_context(school, klass, subject):
    ctx = build_week_context(MON, school)
    assert isinstance(ctx, ScheduleContext)


def test_solver_package_has_no_django_import():
    pkg = Path(builder.__file__).parent / "solver"
    for path in pkg.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any(n.split(".")[0] == "django" for n in names), path.name


def test_defaults_to_current_school(school, klass, subject):
    t = teacher(school)
    a = assign(school, t, klass, subject)
    ctx = build_week_context(MON)
    assert [x.id for x in ctx.assignments] == [a.pk]


@pytest.mark.parametrize("bad", [TUE, D(2026, 10, 18)])
def test_non_monday_rejected(school, bad):
    with pytest.raises(ValueError, match="lunedi"):
        build_week_context(bad, school)


def test_non_date_rejected(school):
    with pytest.raises(ValueError):
        build_week_context("2026-10-12", school)


def test_empty_week_has_no_assignments(school):
    ctx = build_week_context(MON, school)
    assert ctx.assignments == []
    assert ctx.targets == {}
    assert ctx.pairings == []


def test_other_school_data_excluded(school, klass, subject):
    other = School.objects.create(
        name="B", formative_year="x", year_start=D(2026, 9, 1), year_end=END
    )
    oc = make_class(other, "9Z")
    os_ = Subject.objects.create(
        school=other, name="X", kind="TEORIA", cognitive_weight="MEDIO"
    )
    assign(other, teacher(other, "ALTRO"), oc, os_)
    ctx = build_week_context(MON, school)
    assert ctx.assignments == []
    assert oc.pk not in ctx.class_groups


# --- assignments, hours, targets -------------------------------------------


def test_assignment_fields(school, klass, subject):
    t = teacher(school)
    pr = Subject.objects.create(
        school=school, name="Lab", kind="PRATICA", cognitive_weight="MEDIO"
    )
    a1 = assign(school, t, klass, subject, 20)
    a2 = assign(school, t, klass, pr, 12)
    ctx = build_week_context(MON, school)
    by_id = {a.id: a for a in ctx.assignments}
    one = by_id[a1.pk]
    assert (one.teacher_id, one.class_id, one.subject_id) == (
        t.pk,
        klass.pk,
        subject.pk,
    )
    assert one.residual_hours == 20
    assert one.subject_type == "TEORIA"
    assert by_id[a2.pk].subject_type == "PRATICA"


def test_remaining_is_total_minus_delivered(school, klass, subject, monkeypatch):
    t = teacher(school)
    a = assign(school, t, klass, subject, 20)
    monkeypatch.setattr(
        builder.assignment_hours, "delivered_hours_map", lambda s: {a.pk: 8}
    )
    ctx = build_week_context(MON, school)
    assert ctx.assignments[0].residual_hours == 12
    # 12 over 10 weeks -> 1; proves the target uses the remaining, not the total
    assert ctx.targets[a.pk] == 1


def test_overdelivered_has_zero_residual_and_no_target(
    school, klass, subject, monkeypatch
):
    t = teacher(school)
    a = assign(school, t, klass, subject, 20)
    monkeypatch.setattr(
        builder.assignment_hours, "delivered_hours_map", lambda s: {a.pk: 25}
    )
    ctx = build_week_context(MON, school)
    assert ctx.assignments[0].residual_hours == 0
    assert a.pk not in ctx.targets
    assert a.pk not in ctx.target_sources


def test_zero_hours_assignment_has_no_target(school, klass, subject):
    a = assign(school, teacher(school), klass, subject, 0)
    ctx = build_week_context(MON, school)
    assert ctx.assignments[0].residual_hours == 0
    assert a.pk not in ctx.targets


def test_target_is_hours_helper_result(school, klass, subject):
    a = assign(school, teacher(school), klass, subject, 20)
    ctx = build_week_context(MON, school)
    assert ctx.targets[a.pk] == 2
    assert ctx.target_sources[a.pk] == "residuo 20 ore su 10 settimane"


def test_target_two_weeks_and_floor(school, klass, subject):
    a = assign(school, teacher(school), klass, subject, 20)
    # 2 weeks left -> 10
    ctx = build_week_context(END - datetime.timedelta(days=14), school)
    assert ctx.targets[a.pk] == 10
    assert "su 2 settimane" in ctx.target_sources[a.pk]
    # past the end: weeks_left floors at 1
    late = build_week_context(END + datetime.timedelta(days=28 - 1 + 1), school)
    assert late.targets[a.pk] == 20
    assert "su 1 settimane" in late.target_sources[a.pk]


def test_small_remaining_target_is_at_least_one(school, klass, subject):
    a = assign(school, teacher(school), klass, subject, 3)
    assert build_week_context(MON, school).targets[a.pk] == 1


def test_contractor_target_is_not_reduced(school, klass, subject):
    a = assign(school, teacher(school, type="CONTRATTO"), klass, subject, 20)
    assert build_week_context(MON, school).targets[a.pk] == 2


def test_full_stage_week_class_gets_no_target(school, subject):
    staged = make_class(school, "1A")
    other = make_class(school, "1B")
    ta = teacher(school)
    a = assign(school, ta, staged, subject)
    b = assign(school, ta, other, subject)
    for i in range(5):
        entry(school, MON + datetime.timedelta(days=i), stage_class=staged)
    ctx = build_week_context(MON, school)
    assert a.pk not in ctx.targets
    assert a.pk not in ctx.target_sources
    assert b.pk in ctx.targets
    # the assignment itself is still passed on
    assert {x.id for x in ctx.assignments} == {a.pk, b.pk}


def test_class_with_one_lesson_day_keeps_target(school, subject):
    c = make_class(school, "1A")
    a = assign(school, teacher(school), c, subject)
    for i in range(4):
        entry(school, MON + datetime.timedelta(days=i), is_closure=True)
    assert a.pk in build_week_context(MON, school).targets


def test_closed_week_class_gets_no_target(school, subject):
    c = make_class(school, "1A")
    a = assign(school, teacher(school), c, subject)
    for i in range(5):
        entry(school, MON + datetime.timedelta(days=i), is_closure=True)
    assert a.pk not in build_week_context(MON, school).targets


def test_inactive_teacher_assignments_excluded(school, klass, subject):
    active = teacher(school, "A")
    gone = teacher(school, "B", active=False)
    a = assign(school, active, klass, subject)
    assign(school, gone, klass, subject)
    ctx = build_week_context(MON, school)
    assert [x.id for x in ctx.assignments] == [a.pk]


# --- calendar ---------------------------------------------------------------


def test_no_calendar_rows_default_six_hours(school, klass):
    ctx = build_week_context(MON, school)
    assert ctx.class_capacity[klass.pk] == {i: 6 for i in range(5)}
    assert ctx.class_min_start == {}
    assert ctx.stage_days == {}


def test_hours_capped_at_six_and_short_day(school, klass):
    entry(school, MON, max_hours=8)
    entry(school, TUE, max_hours=3)
    cap = build_week_context(MON, school).class_capacity[klass.pk]
    assert cap[0] == 6
    assert cap[1] == 3
    assert cap[2] == 6


def test_only_this_weeks_rows_are_used(school, klass):
    entry(school, MON + datetime.timedelta(days=7), is_closure=True)
    entry(school, MON - datetime.timedelta(days=7), is_closure=True)
    assert build_week_context(MON, school).class_capacity[klass.pk][0] == 6


def test_closure_is_zero_but_not_stage(school, klass):
    entry(school, TUE, is_closure=True)
    ctx = build_week_context(MON, school)
    assert ctx.class_capacity[klass.pk][1] == 0
    assert ctx.stage_days == {}


def test_start_hour_limit_passed(school, klass):
    entry(school, TUE, max_hours=4, start_hour_min=10)
    ctx = build_week_context(MON, school)
    assert ctx.class_min_start == {klass.pk: 10}


def test_start_hour_limit_only_for_that_group(school):
    prima = make_class(school, "1A", "PRIME")
    terza = make_class(school, "3A", "TERZE")
    entry(school, TUE, group="PRIME", max_hours=4, start_hour_min=9)
    ctx = build_week_context(MON, school)
    assert ctx.class_min_start == {prima.pk: 9}
    assert terza.pk not in ctx.class_min_start


def test_stage_of_class_is_zero_hours_on_that_day(school):
    a = make_class(school, "1A")
    b = make_class(school, "1B")
    entry(school, TUE, stage_class=a)
    ctx = build_week_context(MON, school)
    assert ctx.class_capacity[a.pk][1] == 0
    assert ctx.class_capacity[b.pk][1] == 6
    assert ctx.stage_days == {a.pk: frozenset({1})}


def test_group_stage_is_zero_hours_for_that_group(school):
    prima = make_class(school, "1A", "PRIME")
    seconda = make_class(school, "2A", "SECONDE")
    nogroup = make_class(school, "5A")
    entry(school, TUE, group="PRIME", is_group_stage=True, max_hours=0)
    ctx = build_week_context(MON, school)
    assert ctx.class_capacity[prima.pk][1] == 0
    assert ctx.class_capacity[seconda.pk][1] == 6
    assert ctx.class_capacity[nogroup.pk][1] == 6
    assert ctx.stage_days == {prima.pk: frozenset({1})}


def test_group_row_beats_school_row(school):
    prima = make_class(school, "1A", "PRIME")
    seconda = make_class(school, "2A", "SECONDE")
    entry(school, TUE, max_hours=5)
    entry(school, TUE, group="PRIME", max_hours=2)
    ctx = build_week_context(MON, school)
    assert ctx.class_capacity[prima.pk][1] == 2
    assert ctx.class_capacity[seconda.pk][1] == 5


def test_school_row_beats_default(school, klass):
    entry(school, TUE, max_hours=4)
    assert build_week_context(MON, school).class_capacity[klass.pk][1] == 4


def test_class_stage_beats_group_row(school):
    prima = make_class(school, "1A", "PRIME")
    entry(school, TUE, group="PRIME", max_hours=5)
    entry(school, TUE, stage_class=prima)
    assert build_week_context(MON, school).class_capacity[prima.pk][1] == 0


def test_stage_class_row_does_not_affect_start_limit_of_others(school):
    a = make_class(school, "1A")
    b = make_class(school, "1B")
    entry(school, TUE, stage_class=a, start_hour_min=11)
    assert b.pk not in build_week_context(MON, school).class_min_start


# --- availability ----------------------------------------------------------


def test_permanent_teacher_available_all_week_despite_grid(school, klass, subject):
    t = teacher(school, type="ASSUNTO")
    assign(school, t, klass, subject)
    save(t, MON, grid_with("lunedi"))
    ctx = build_week_context(MON, school)
    assert ctx.availability[t.pk] == frozenset(
        (d, h) for d in range(5) for h in range(6)
    )


def test_availability_is_always_a_dict_even_without_grids(school):
    ctx = build_week_context(MON, school)
    assert ctx.availability == {}
    assert ctx.availability is not None


def test_availability_is_a_dict_with_teachers_but_no_grids(school, klass, subject):
    t = teacher(school, type="CONTRATTO", first_time=True)
    assign(school, t, klass, subject)
    ctx = build_week_context(MON, school)
    assert isinstance(ctx.availability, dict)
    assert t.pk in ctx.availability


def test_contractor_stored_grid_used(school):
    t = teacher(school, type="CONTRATTO")
    save(t, MON, grid_with("martedi"))
    save(t, MON - datetime.timedelta(days=7), g.default_grid())
    ctx = build_week_context(MON, school)
    expected = frozenset((d, h) for d in range(5) if d != 1 for h in range(6))
    assert ctx.availability[t.pk] == expected


def test_contractor_partial_slots_map_to_hour_indexes(school):
    t = teacher(school, type="CONTRATTO")
    grid = grid_with("lunedi")
    grid["lunedi"] = [
        {"ora_inizio": "09:00", "ora_fine": "11:00", "disponibile": True},
        {"ora_inizio": "11:00", "ora_fine": "12:30", "disponibile": False},
        {"ora_inizio": "12:30", "ora_fine": "14:00", "disponibile": True},
    ]
    save(t, MON, grid)
    got = {h for d, h in build_week_context(MON, school).availability[t.pk] if d == 0}
    assert got == {1, 2, 4, 5}


def test_contractor_slots_outside_school_hours_clipped(school):
    t = teacher(school, type="CONTRATTO")
    grid = g.default_grid()
    grid["lunedi"] = [{"ora_inizio": "06:00", "ora_fine": "16:00", "disponibile": True}]
    save(t, MON, grid)
    got = {h for d, h in build_week_context(MON, school).availability[t.pk] if d == 0}
    assert got == {0, 1, 2, 3, 4, 5}


def test_contractor_inherits_most_recent_previous_week(school):
    t = teacher(school, type="CONTRATTO")
    save(t, MON - datetime.timedelta(days=21), g.default_grid())
    save(t, MON - datetime.timedelta(days=7), grid_with("mercoledi"))
    ctx = build_week_context(MON, school)
    expected = frozenset((d, h) for d in range(5) if d != 2 for h in range(6))
    assert ctx.availability[t.pk] == expected


def test_contractor_ignores_future_grid(school):
    t = teacher(school, type="CONTRATTO", first_time=True)
    save(t, MON + datetime.timedelta(days=7), grid_with("lunedi"))
    ctx = build_week_context(MON, school)
    assert len(ctx.availability[t.pk]) == 30


def test_first_year_contractor_defaults_to_all_available(school):
    t = teacher(school, type="CONTRATTO", first_time=True)
    ctx = build_week_context(MON, school)
    assert ctx.availability[t.pk] == frozenset(
        (d, h) for d in range(5) for h in range(6)
    )


def test_contractor_without_any_record_is_unavailable(school):
    t = teacher(school, type="CONTRATTO", first_time=False)
    ctx = build_week_context(MON, school)
    assert ctx.availability[t.pk] == frozenset()


def test_malformed_stored_grid_is_unavailable(school):
    t = teacher(school, type="CONTRATTO")
    Availability.objects.create(
        school=school, teacher=t, week_start=MON, slots={"lunedi": []}
    )
    assert build_week_context(MON, school).availability[t.pk] == frozenset()


# --- read-only --------------------------------------------------------------


def test_building_writes_no_rows(school, klass, subject):
    t = teacher(school, type="CONTRATTO")
    save(t, MON - datetime.timedelta(days=7), g.default_grid())
    teacher(school, "NEW", type="CONTRATTO", first_time=True)
    assign(school, t, klass, subject)
    entry(school, TUE, max_hours=3)
    models = (Availability, Assignment, CalendarEntry, Class, Teacher, ClassPairing)

    def counts():
        return [m.objects.count() for m in models]

    before = counts()
    build_week_context(MON, school)
    build_week_context(MON, school)
    assert counts() == before
    assert not Availability.objects.filter(week_start=MON).exists()


def test_building_issues_only_selects(school, klass, subject):
    t = teacher(school, type="CONTRATTO")
    assign(school, t, klass, subject)
    with CaptureQueriesContext(connection) as ctx:
        build_week_context(MON, school)
    assert ctx.captured_queries
    assert all(q["sql"].lstrip().upper().startswith("SELECT") for q in ctx)


# --- pairings and maps ------------------------------------------------------


def test_pairings_with_and_without_teacher(school, subject):
    a, b, c, d = (make_class(school, n) for n in ("1A", "1B", "1C", "1D"))
    t = teacher(school)
    ClassPairing.objects.create(
        school=school, class_a=a, class_b=b, subject=subject, teacher=t
    )
    ClassPairing.objects.create(
        school=school, class_a=c, class_b=d, subject=subject, teacher=None
    )
    ClassPairing.objects.create(
        school=school, class_a=a, class_b=c, subject=subject, teacher=t
    )
    ctx = build_week_context(MON, school)
    got = {(p.class_a, p.class_b, p.subject_id, p.teacher_id) for p in ctx.pairings}
    assert got == {
        (a.pk, b.pk, subject.pk, t.pk),
        (c.pk, d.pk, subject.pk, None),
        (a.pk, c.pk, subject.pk, t.pk),
    }


def test_group_of_four_classes(school, subject):
    cl = [make_class(school, n) for n in ("1A", "1B", "1C", "1D")]
    t = teacher(school)
    ClassPairing.objects.create(
        school=school, class_a=cl[0], class_b=cl[1], subject=subject, teacher=t
    )
    ClassPairing.objects.create(
        school=school, class_a=cl[2], class_b=cl[3], subject=subject, teacher=t
    )
    assert len(build_week_context(MON, school).pairings) == 2


def test_type_and_group_maps(school, subject):
    grouped = make_class(school, "1A", "PRIME")
    plain = make_class(school, "5A")
    ta = teacher(school, "A", type="ASSUNTO")
    tc = teacher(school, "B", type="CONTRATTO")
    assign(school, ta, grouped, subject)
    assign(school, tc, plain, subject)
    ctx = build_week_context(MON, school)
    assert ctx.teacher_types == {ta.pk: "ASSUNTO", tc.pk: "CONTRATTO"}
    assert ctx.class_groups == {grouped.pk: "PRIME", plain.pk: None}
    assert set(ctx.class_capacity) == {grouped.pk, plain.pk}


# --- queries ----------------------------------------------------------------


def test_query_count_bounded(school, subject, django_assert_max_num_queries):
    classes = [make_class(school, f"C{i}") for i in range(5)]
    teachers = [
        teacher(school, f"T{i}", type="CONTRATTO" if i % 2 else "ASSUNTO")
        for i in range(6)
    ]
    for i in range(24):
        assign(
            school,
            teachers[i % 6],
            classes[i % 5],
            Subject.objects.create(
                school=school, name=f"S{i}", kind="TEORIA", cognitive_weight="MEDIO"
            ),
        )
    for t in teachers:
        save(t, MON, g.default_grid())
    entry(school, TUE, max_hours=4)

    with CaptureQueriesContext(connection) as small:
        build_week_context(MON, school)
    n = len(small)

    for i in range(24, 48):
        assign(
            school,
            teachers[i % 6],
            classes[i % 5],
            Subject.objects.create(
                school=school, name=f"S{i}", kind="PRATICA", cognitive_weight="MEDIO"
            ),
        )
    with django_assert_max_num_queries(n):
        ctx = build_week_context(MON, school)
    assert len(ctx.assignments) == 48
    assert n <= 8

"""Calendar CSV with 0-hour closures and 8-hour group stages, and the weekly
target that ignores the weeks a class spends in stage (#149)."""

import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from test_calendar_hours_rule import html, message, others
from test_setup_wizard import CSVS, HX, correct, db_counts, login, save, upload

from scheduling.context import build_week_context
from school.hours import weekly_hours_target
from school.importers import parse_calendar
from school.models import (
    Assignment,
    CalendarEntry,
    Class,
    ParkedUpload,
    School,
    Subject,
    Teacher,
)

D = datetime.date
HEAD = "data,ore_max_giornata,flag_chiusura,stage_classe_id,gruppo\n"


@pytest.fixture
def admin(db):
    return login("ADMIN")


def rows_of(admin):
    return ParkedUpload.objects.get(file_type="calendar").rows


# --- CSV: closure with 0 hours ---------------------------------------------------


@pytest.mark.parametrize("group", ["", "PRIME"])
@pytest.mark.parametrize("hours", [0, 4, 5, 6])
def test_closure_row_accepts_zero_and_lesson_hours(hours, group):
    result = parse_calendar(f"{HEAD}2026-12-25,{hours},true,,{group}\n")
    assert result.messages == []
    assert result.rows[0]["ore_max_giornata"] == hours
    assert result.rows[0]["flag_stage_gruppo"] is False


def test_closure_with_zero_hours_is_saved(admin):
    upload(admin, "calendar", HEAD + "2026-09-14,6,false,,\n2026-12-25,0,true,,\n")
    others(admin)
    r = save(admin)
    assert r.status_code == 302, html(r)
    entry = CalendarEntry.objects.get(date=D(2026, 12, 25))
    assert entry.is_closure and entry.max_hours == 0


# --- CSV: 8 hours is a group stage -----------------------------------------------


def test_eight_with_group_is_a_group_stage_without_new_column():
    result = parse_calendar(
        "data,ore_max_giornata,flag_chiusura,stage_classe_id,gruppo\n"
        "2026-11-10,8,false,,QUARTE\n"
    )
    assert result.messages == []
    assert result.rows[0]["flag_stage_gruppo"] is True
    # a CSV with only the three original columns is still accepted
    old = parse_calendar("data,ore_max_giornata,flag_chiusura\n2026-11-10,6,false\n")
    assert old.messages == [] and old.rows[0]["flag_stage_gruppo"] is False


def test_stage_row_shows_badge_and_is_saved(admin):
    r = upload(
        admin,
        "calendar",
        HEAD + "2026-09-14,6,false,,\n2026-11-10,8,false,,QUARTE\n",
        **HX,
    )
    assert 'cal-badge">stage<' in html(r)
    others(admin)
    assert save(admin).status_code == 302
    entry = CalendarEntry.objects.get(date=D(2026, 11, 10))
    assert entry.is_group_stage and entry.group == "QUARTE"
    assert entry.stage_class is None and entry.max_hours == 8


# --- CSV: refused values ---------------------------------------------------------


@pytest.mark.parametrize(
    "line,shown",
    [
        ("2026-11-10,0,false,,", "0"),
        ("2026-11-10,8,false,,", "8"),
        ("2026-11-10,8,true,,QUARTE", "8"),
        ("2026-11-10,8,false,1A,QUARTE", "8"),
        ("2026-11-10,1,false,,", "1"),
        ("2026-11-10,2,false,,", "2"),
        ("2026-11-10,3,false,,", "3"),
        ("2026-11-10,7,false,,", "7"),
        ("2026-11-10,9,false,,", "9"),
        ("2026-11-10,-1,false,,", "-1"),
        ("2026-11-10,abc,false,,", "abc"),
        ("2026-11-10,5.5,false,,", "5.5"),
    ],
)
def test_refused_hours_block_save(admin, line, shown):
    r = upload(admin, "calendar", HEAD + "2026-09-13,6,false,,\n" + line + "\n", **HX)
    assert f"Riga 3: {message(shown)}" in html(r)
    others(admin)
    before = db_counts()
    assert save(admin).status_code == 422
    assert db_counts() == before
    assert not CalendarEntry.objects.exists()


# --- manual date form ------------------------------------------------------------


def add(admin, **data):
    return admin.post("/setup/add-date/", {"date": "2026-10-01", **data}, **HX)


def test_manual_date_zero_with_closure_is_accepted(admin):
    upload(admin, "calendar", CSVS["calendar"])
    assert add(admin, max_hours="0", is_closure="on").status_code == 200
    new = [r for r in rows_of(admin) if r["data"] == "2026-10-01"][0]
    assert new["ore_max_giornata"] == 0 and new["flag_chiusura"] is True


@pytest.mark.parametrize(
    "data",
    [
        {"max_hours": "0"},
        {"max_hours": "8"},
        {"max_hours": "8", "is_closure": "on"},
    ],
)
def test_manual_date_refuses_zero_without_closure_and_eight(admin, data):
    upload(admin, "calendar", CSVS["calendar"])
    r = add(admin, **data)
    assert r.status_code == 422
    assert message(data["max_hours"]) in html(r)
    assert len(rows_of(admin)) == 3


@pytest.mark.parametrize("hours", ["4", "5", "6"])
def test_manual_date_lesson_hours_as_before(admin, hours):
    upload(admin, "calendar", CSVS["calendar"])
    assert add(admin, max_hours=hours).status_code == 200


# --- live correction -------------------------------------------------------------


GROUPED = HEAD + "2026-11-10,5,false,,QUARTE\n2026-11-11,6,false,,\n"


def test_correction_to_eight_sets_and_clears_the_stage(admin):
    upload(admin, "calendar", GROUPED)
    assert correct(admin, "calendar", 2, "ore_max_giornata", "8").status_code == 200
    assert rows_of(admin)[0]["flag_stage_gruppo"] is True
    assert rows_of(admin)[0]["ore_max_giornata"] == 8
    assert correct(admin, "calendar", 2, "ore_max_giornata", "5").status_code == 200
    assert rows_of(admin)[0]["flag_stage_gruppo"] is False


def test_correction_to_eight_without_group_is_refused(admin):
    upload(admin, "calendar", GROUPED)
    r = correct(admin, "calendar", 3, "ore_max_giornata", "8")
    assert r.status_code == 422
    assert message("8") in html(r)
    assert rows_of(admin)[1]["ore_max_giornata"] == 6


def test_removing_the_group_of_a_stage_row_is_refused(admin):
    upload(admin, "calendar", HEAD + "2026-11-10,8,false,,QUARTE\n")
    r = correct(admin, "calendar", 2, "gruppo", "")
    assert r.status_code == 422
    assert rows_of(admin)[0]["gruppo"] == "QUARTE"
    assert rows_of(admin)[0]["flag_stage_gruppo"] is True


def test_eight_and_zero_survive_editing_another_cell(admin):
    upload(
        admin,
        "calendar",
        HEAD + "2026-11-10,8,false,,QUARTE\n2026-12-25,0,true,,\n",
    )
    assert correct(admin, "calendar", 2, "gruppo", "PRIME").status_code == 200
    assert correct(admin, "calendar", 3, "data", "2026-12-26").status_code == 200
    rows = rows_of(admin)
    assert rows[0]["ore_max_giornata"] == 8 and rows[0]["gruppo"] == "PRIME"
    assert rows[0]["flag_stage_gruppo"] is True
    assert rows[1]["ore_max_giornata"] == 0


# --- target: builder -------------------------------------------------------------


MON = D(2026, 10, 12)
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


def klass(school, name, group):
    return Class.objects.create(school=school, name=name, group=group)


def assign(school, teacher, school_class, subject, hours=60):
    return Assignment.objects.create(
        school=school,
        teacher=teacher,
        school_class=school_class,
        subject=subject,
        ore_totali=hours,
    )


@pytest.fixture
def teacher(school):
    return Teacher.objects.create(
        school=school, name="ROSSI", type="ASSUNTO", first_time=False
    )


def stage_week(school, week, days=5, **kw):
    for i in range(days):
        CalendarEntry.objects.create(
            school=school, date=MON + datetime.timedelta(weeks=week, days=i), **kw
        )


def test_group_stage_weeks_are_not_counted(school, subject, teacher):
    a = assign(school, teacher, klass(school, "1A", "PRIME"), subject)
    b = assign(school, teacher, klass(school, "2A", "SECONDE"), subject)
    for week in (1, 2, 3, 4):
        stage_week(school, week, is_group_stage=True, group="PRIME", max_hours=8)
    ctx = build_week_context(MON, school)
    assert ctx.targets[a.pk] == 60 // (10 - 4)
    assert ctx.targets[b.pk] == 60 // 10
    assert ctx.target_sources[a.pk] == "residuo 60 ore su 6 settimane"
    assert ctx.target_sources[b.pk] == "residuo 60 ore su 10 settimane"


def test_single_class_stage_weeks_are_not_counted(school, subject, teacher):
    c = klass(school, "1A", "PRIME")
    a = assign(school, teacher, c, subject)
    b = assign(school, teacher, klass(school, "1B", "PRIME"), subject)
    for week in (1, 2, 3, 4):
        stage_week(school, week, stage_class=c)
    ctx = build_week_context(MON, school)
    assert ctx.targets[a.pk] == 60 // 6
    assert ctx.target_sources[a.pk] == "residuo 60 ore su 6 settimane"
    assert ctx.targets[b.pk] == 60 // 10


def test_week_split_between_class_and_group_stage_is_a_stage_week(
    school, subject, teacher
):
    c = klass(school, "1A", "PRIME")
    a = assign(school, teacher, c, subject)
    stage_week(school, 1, days=2, stage_class=c)
    for i in (2, 3, 4):
        CalendarEntry.objects.create(
            school=school,
            date=MON + datetime.timedelta(weeks=1, days=i),
            is_group_stage=True,
            group="PRIME",
            max_hours=8,
        )
    assert build_week_context(MON, school).targets[a.pk] == 60 // 9


def test_partial_stage_week_is_a_normal_week(school, subject, teacher):
    a = assign(school, teacher, klass(school, "1A", "PRIME"), subject)
    stage_week(school, 1, days=3, is_group_stage=True, group="PRIME", max_hours=8)
    assert build_week_context(MON, school).targets[a.pk] == 60 // 10


def closed(school, week, days=range(5), group=None, **kw):
    kw = {"is_closure": True, "max_hours": 0, **kw}
    for i in days:
        CalendarEntry.objects.create(
            school=school,
            date=MON + datetime.timedelta(weeks=week, days=i),
            group=group,
            **kw,
        )


def one(school, teacher, subject, group="PRIME", hours=60):
    return assign(school, teacher, klass(school, "1A", group), subject, hours)


def test_closure_week_is_removed(school, subject, teacher):
    a = one(school, teacher, subject)
    closed(school, 1)
    ctx = build_week_context(MON, school)
    assert ctx.targets[a.pk] == 60 // 9
    assert ctx.target_sources[a.pk] == "residuo 60 ore su 9 settimane"


def test_two_closure_weeks(school, subject, teacher):
    a = one(school, teacher, subject)
    closed(school, 1)
    closed(school, 5)
    ctx = build_week_context(MON, school)
    assert ctx.targets[a.pk] == 60 // 8
    assert ctx.target_sources[a.pk] == "residuo 60 ore su 8 settimane"


def test_four_closed_days_of_five_stay_counted(school, subject, teacher):
    a = one(school, teacher, subject)
    closed(school, 1, days=range(4))
    assert build_week_context(MON, school).targets[a.pk] == 60 // 10


def test_closure_with_one_lesson_row_stays_counted(school, subject, teacher):
    a = one(school, teacher, subject)
    closed(school, 1, days=range(4))
    CalendarEntry.objects.create(
        school=school, date=MON + datetime.timedelta(weeks=1, days=4), max_hours=6
    )
    assert build_week_context(MON, school).targets[a.pk] == 60 // 10


def test_weekend_closure_rows_never_remove_a_week(school, subject, teacher):
    a = one(school, teacher, subject)
    closed(school, 1, days=(5, 6))
    assert build_week_context(MON, school).targets[a.pk] == 60 // 10


def test_weekend_lesson_row_does_not_prevent_removal(school, subject, teacher):
    a = one(school, teacher, subject)
    closed(school, 1)
    CalendarEntry.objects.create(
        school=school, date=MON + datetime.timedelta(weeks=1, days=5), max_hours=6
    )
    assert build_week_context(MON, school).targets[a.pk] == 60 // 9


def test_group_closure_removes_the_week_for_that_group_only(school, subject, teacher):
    a = one(school, teacher, subject)
    b = assign(school, teacher, klass(school, "2A", "SECONDE"), subject)
    closed(school, 1, group="PRIME")
    ctx = build_week_context(MON, school)
    assert ctx.targets[a.pk] == 60 // 9
    assert ctx.targets[b.pk] == 60 // 10


def test_group_lesson_row_overrides_the_school_closure(school, subject, teacher):
    a = one(school, teacher, subject)
    b = assign(school, teacher, klass(school, "2A", "SECONDE"), subject)
    closed(school, 1)
    CalendarEntry.objects.create(
        school=school,
        date=MON + datetime.timedelta(weeks=1, days=2),
        group="PRIME",
        max_hours=6,
    )
    ctx = build_week_context(MON, school)
    assert ctx.targets[a.pk] == 60 // 10
    assert ctx.targets[b.pk] == 60 // 9


def test_zero_hours_without_closure_flag_is_a_lesson_day(school, subject, teacher):
    a = one(school, teacher, subject)
    closed(school, 1, is_closure=False)
    assert build_week_context(MON, school).targets[a.pk] == 60 // 10


def test_stage_row_of_another_class_closes_nothing(school, subject, teacher):
    a = one(school, teacher, subject)
    other = klass(school, "1B", "PRIME")
    stage_week(school, 1, stage_class=other)
    assert build_week_context(MON, school).targets[a.pk] == 60 // 10


def test_school_closure_and_group_stage_on_same_days_count_once(
    school, subject, teacher
):
    a = one(school, teacher, subject)
    closed(school, 1)
    stage_week(school, 1, is_group_stage=True, group="PRIME", max_hours=8)
    assert build_week_context(MON, school).targets[a.pk] == 60 // 9


def test_stage_and_closure_mix_is_removed_once(school, subject, teacher):
    c = klass(school, "1A", "PRIME")
    a = assign(school, teacher, c, subject)
    stage_week(school, 1, days=2, stage_class=c)
    closed(school, 1, days=(2, 3, 4))
    assert build_week_context(MON, school).targets[a.pk] == 60 // 9


def test_stage_closure_and_a_day_without_row_stays_counted(school, subject, teacher):
    c = klass(school, "1A", "PRIME")
    a = assign(school, teacher, c, subject)
    stage_week(school, 1, days=3, stage_class=c)
    closed(school, 1, days=(3,))
    assert build_week_context(MON, school).targets[a.pk] == 60 // 10


def test_stage_closure_and_mixed_weeks_combine(school, subject, teacher):
    c = klass(school, "1A", "PRIME")
    a = assign(school, teacher, c, subject)
    for week in (1, 2, 3, 4):
        stage_week(school, week, is_group_stage=True, group="PRIME", max_hours=8)
    closed(school, 5)
    closed(school, 6)
    stage_week(school, 7, days=2, stage_class=c)
    closed(school, 7, days=(2, 3, 4))
    ctx = build_week_context(MON, school)
    assert ctx.targets[a.pk] == 60 // 3
    assert ctx.target_sources[a.pk] == "residuo 60 ore su 3 settimane"


def test_all_remaining_weeks_closed_leaves_one_week(school, subject, teacher):
    a = one(school, teacher, subject)
    for week in range(1, 10):
        closed(school, week)
    ctx = build_week_context(MON, school)
    assert ctx.targets[a.pk] == 60 // 1
    assert ctx.target_sources[a.pk] == "residuo 60 ore su 1 settimane"


def test_current_week_closed_has_no_target_and_future_count_is_unchanged(
    school, subject, teacher
):
    from scheduling.context import _stage_weeks

    c = klass(school, "1A", "PRIME")
    a = assign(school, teacher, c, subject)
    closed(school, 0)
    assert a.pk not in build_week_context(MON, school).targets
    assert _stage_weeks(school, MON, [c]) == {c.pk: 0}
    closed(school, 1)
    assert _stage_weeks(school, MON, [c]) == {c.pk: 1}


def test_closure_block_equals_a_calendar_with_fewer_weeks(school, subject, teacher):
    a = one(school, teacher, subject, hours=47)
    for week in (1, 2, 3):
        closed(school, week)
    with_closure = build_week_context(MON, school)
    school.year_end = END - datetime.timedelta(weeks=3)
    school.save()
    CalendarEntry.objects.all().delete()
    shorter = build_week_context(MON, school)
    assert with_closure.targets[a.pk] == shorter.targets[a.pk]
    assert with_closure.target_sources[a.pk] == "residuo 47 ore su 7 settimane"


def test_closure_rows_do_not_add_queries(school, subject, teacher):
    def queries(n):
        for i in range(n):
            assign(school, teacher, klass(school, f"C{n}{i}", "PRIME"), subject)
        with CaptureQueriesContext(connection) as ctx:
            build_week_context(MON, school)
        return len(ctx)

    closed(school, 1)
    closed(school, 2, group="PRIME")
    assert queries(4) == queries(2)


def test_current_week_in_stage_still_has_no_target(school, subject, teacher):
    a = assign(school, teacher, klass(school, "1A", "PRIME"), subject)
    stage_week(school, 0, is_group_stage=True, group="PRIME", max_hours=8)
    assert a.pk not in build_week_context(MON, school).targets


def test_all_remaining_weeks_in_stage_leaves_one_week(school, subject, teacher):
    a = assign(school, teacher, klass(school, "1A", "PRIME"), subject)
    for week in range(1, 10):
        stage_week(school, week, is_group_stage=True, group="PRIME", max_hours=8)
    ctx = build_week_context(MON, school)
    assert ctx.targets[a.pk] == 60 // 1
    assert ctx.target_sources[a.pk] == "residuo 60 ore su 1 settimane"


def test_stage_weeks_cost_one_query_whatever_the_classes(school, subject, teacher):
    def queries(n):
        for i in range(n):
            assign(school, teacher, klass(school, f"C{n}{i}", "PRIME"), subject)
        with CaptureQueriesContext(connection) as ctx:
            build_week_context(MON, school)
        return len(ctx)

    stage_week(school, 1, is_group_stage=True, group="PRIME", max_hours=8)
    two = queries(2)
    assert queries(4) == two
    # one more than a school with no stage helper: the stage rows query
    assert two >= 1


def test_stage_query_is_the_only_extra_one(school, subject, teacher, monkeypatch):
    from scheduling import context as builder

    assign(school, teacher, klass(school, "1A", "PRIME"), subject)
    with CaptureQueriesContext(connection) as with_helper:
        build_week_context(MON, school)
    monkeypatch.setattr(
        builder, "_stage_weeks", lambda s, w, classes: {c.pk: 0 for c in classes}
    )
    with CaptureQueriesContext(connection) as without:
        build_week_context(MON, school)
    assert len(with_helper) == len(without) + 1


def test_stage_block_equals_a_calendar_with_fewer_weeks(school, subject, teacher):
    a = assign(school, teacher, klass(school, "1A", "PRIME"), subject, 47)
    for week in (1, 2, 3):
        stage_week(school, week, is_group_stage=True, group="PRIME", max_hours=8)
    with_stage = build_week_context(MON, school)
    school.year_end = END - datetime.timedelta(weeks=3)
    school.save()
    CalendarEntry.objects.all().delete()
    shorter = build_week_context(MON, school)
    assert with_stage.targets[a.pk] == shorter.targets[a.pk]
    assert with_stage.target_sources[a.pk] == ("residuo 47 ore su 7 settimane")
    assert (
        weekly_hours_target(47, MON, END, "ASSUNTO", stage_weeks=3)
        == (shorter.targets[a.pk])
    )

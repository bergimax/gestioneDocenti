import datetime
from unittest import mock

import pytest
from django.db import IntegrityError, transaction

from accounts.models import User
from ledger.models import LedgerEntry
from school.assignment_hours import (
    AdjustmentReasonRequired,
    NegativeDelivered,
    adjust_delivered_hours,
    delivered_hours_map,
    set_assignment_total_hours,
)
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


# --- adjust_delivered_hours (#37) -----------------------------------------


@pytest.fixture
def admin_user(db):
    return User.objects.create_user("boss@example.com", "pw-long-enough", "ADMIN")


def test_adjust_writes_one_entry_with_delta(assignment, admin_user):
    with mock.patch("scheduling.audit.record_audit") as audit_mock:
        result = adjust_delivered_hours(assignment, 5, admin_user, " carryover ")
    assert result == {"changed": True, "old": 0, "new": 5, "hours": 5}
    entry = LedgerEntry.objects.get()
    assert (entry.hours, entry.kind, entry.reason, entry.created_by) == (
        5,
        "ADJUSTMENT",
        "carryover",
        admin_user,
    )
    assert entry.timetable_id is None and entry.week is None
    assert delivered_hours_map(assignment.school) == {assignment.pk: 5}
    audit_mock.assert_called_once()
    assert audit_mock.call_args.args[0] == "HOURS_ADJUSTED"
    assert audit_mock.call_args.args[2] is None
    params = audit_mock.call_args.args[3]
    assert params["assegnazione"] == assignment.pk and params["ore"] == 5
    assert params == {
        "assegnazione": assignment.pk,
        "voce": entry.pk,
        "ore_erogate_prima": 0,
        "ore_erogate_dopo": 5,
        "ore": 5,
    }
    assert "carryover" not in str(params)


def test_adjust_negative_delta_and_above_total(assignment, admin_user):
    adjust_delivered_hours(assignment, 50, admin_user, "sopra il monte ore")
    adjust_delivered_hours(assignment, 20, admin_user, "giù")
    assert sorted(LedgerEntry.objects.values_list("hours", flat=True)) == [-30, 50]
    assert delivered_hours_map(assignment.school) == {assignment.pk: 20}


def test_adjust_unchanged_writes_nothing(assignment, admin_user):
    with mock.patch("scheduling.audit.record_audit") as audit_mock:
        result = adjust_delivered_hours(assignment, 0, admin_user, "")
    assert not result["changed"] and not LedgerEntry.objects.exists()
    audit_mock.assert_not_called()


def test_adjust_refusals_write_nothing(assignment, admin_user):
    with mock.patch("scheduling.audit.record_audit") as audit_mock:
        with pytest.raises(AdjustmentReasonRequired):
            adjust_delivered_hours(assignment, 3, admin_user, "   ")
        with pytest.raises(NegativeDelivered):
            adjust_delivered_hours(assignment, -1, admin_user, "x")
    assert not LedgerEntry.objects.exists()
    audit_mock.assert_not_called()


def test_adjust_second_call_computes_delta_from_current(assignment, admin_user):
    adjust_delivered_hours(assignment, 5, admin_user, "uno")
    adjust_delivered_hours(assignment, 8, admin_user, "due")
    assert sorted(LedgerEntry.objects.values_list("hours", flat=True)) == [3, 5]


def test_adjust_changes_next_week_target(assignment, admin_user):
    from scheduling.context import build_week_context

    week = datetime.date(2026, 10, 19)
    before = build_week_context(week, assignment.school)
    adjust_delivered_hours(assignment, 30, admin_user, "pregresso")
    after = build_week_context(week, assignment.school)
    by_id = {a.id: a for a in after.assignments}
    assert by_id[assignment.pk].residual_hours == 10
    assert before.target_sources[assignment.pk] != after.target_sources[assignment.pk]


@pytest.mark.parametrize("kind", ["ADJUSTMENT", "REVERSAL"])
def test_db_rejects_adjustment_without_reason(assignment, kind):
    with pytest.raises(IntegrityError), transaction.atomic():
        LedgerEntry.objects.create(assignment=assignment, hours=1, kind=kind)
    LedgerEntry.objects.create(assignment=assignment, hours=1, kind=kind, reason="ok")


def test_old_delivered_rows_keep_empty_reason(assignment):
    from scheduling.models import Timetable

    tt = Timetable.objects.create(
        school=assignment.school, week_start=datetime.date(2026, 10, 12), version=1
    )
    e = LedgerEntry.objects.create(
        assignment=assignment,
        timetable=tt,
        week=tt.week_start,
        hours=1,
        kind="DELIVERED",
    )
    assert e.reason == "" and e.created_by_id is None
    with pytest.raises(NotImplementedError):
        e.delete()

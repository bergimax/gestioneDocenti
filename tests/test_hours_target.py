import ast
import datetime
from pathlib import Path

import pytest

from school.hours import weekly_hours_target
from school.models import TeacherType

END = datetime.date(2027, 6, 30)


def start(weeks):
    return END - datetime.timedelta(weeks=weeks)


def test_floor_division():
    assert weekly_hours_target(60, start(30), END, "ASSUNTO") == 2


def test_floor_raised_to_one():
    assert weekly_hours_target(10, start(30), END, "ASSUNTO") == 1


@pytest.mark.parametrize(
    "week_start",
    [END, END + datetime.timedelta(days=14), END - datetime.timedelta(days=6)],
)
def test_last_week_target_is_remaining(week_start):
    assert weekly_hours_target(7, week_start, END, "ASSUNTO") == 7


@pytest.mark.parametrize("remaining", [0, -5])
def test_no_target_when_nothing_remains(remaining):
    assert weekly_hours_target(remaining, start(30), END, "ASSUNTO") is None


@pytest.mark.parametrize(
    "remaining,max_hours,expected",
    [(6, 4, 4), (3, 4, 3), (6, None, 3), (1, None, 1), (6, 0, 1)],
)
def test_contractor_reduction(remaining, max_hours, expected):
    got = weekly_hours_target(
        remaining,
        END,
        END,
        "CONTRATTO",
        reduce_contract_hours=True,
        max_hours=max_hours,
    )
    assert got == expected


def test_reduction_applied_after_floor():
    got = weekly_hours_target(
        60, start(30), END, "CONTRATTO", reduce_contract_hours=True
    )
    assert got == 1


def test_reduction_off_changes_nothing():
    assert weekly_hours_target(6, END, END, "CONTRATTO", False, 2) == 6


def test_reduction_ignored_for_assunto():
    got = weekly_hours_target(6, END, END, "ASSUNTO", True, 2)
    assert got == 6


def test_teacher_type_enum_members_work():
    assert weekly_hours_target(6, END, END, TeacherType.CONTRATTO, True) == 3


def test_helper_imports_nothing():
    tree = ast.parse(Path("school/hours.py").read_text())
    assert not [
        n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
    ]


def test_stage_weeks_default_changes_nothing():
    assert weekly_hours_target(60, start(30), END, "ASSUNTO", stage_weeks=0) == (
        weekly_hours_target(60, start(30), END, "ASSUNTO")
    )


def test_stage_weeks_are_not_counted_in_weeks_left():
    assert weekly_hours_target(60, start(30), END, "ASSUNTO", stage_weeks=8) == (
        60 // (30 - 8)
    )


@pytest.mark.parametrize("stage_weeks", [30, 31, 100])
def test_stage_weeks_never_divide_by_less_than_one(stage_weeks):
    assert (
        weekly_hours_target(7, start(30), END, "ASSUNTO", stage_weeks=stage_weeks) == 7
    )

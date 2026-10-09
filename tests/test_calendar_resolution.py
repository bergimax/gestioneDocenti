import ast
import datetime
from pathlib import Path
from types import SimpleNamespace

from school.calendar import resolve_hours, resolve_start_hour

D = datetime.date(2026, 10, 12)
OTHER = datetime.date(2026, 10, 13)


def row(
    group=None,
    max_hours=6,
    is_closure=False,
    start_hour_min=None,
    stage_class_id=None,
    is_group_stage=False,
    date=D,
):
    return SimpleNamespace(
        date=date,
        group=group,
        max_hours=max_hours,
        is_closure=is_closure,
        start_hour_min=start_hour_min,
        stage_class_id=stage_class_id,
        is_group_stage=is_group_stage,
    )


def hours(rows, class_id=1, group="PRIME"):
    return resolve_hours(rows, D, class_id, group)


def test_module_imports_nothing_from_django():
    tree = ast.parse(Path("school/calendar.py").read_text())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    assert not [n for n in names if n.split(".")[0] in ("django", "school")]


# One test per precedence rule


def test_rule1_class_stage_beats_group_row():
    rows = [row(group="PRIME", max_hours=5), row(stage_class_id=1)]
    assert hours(rows) == 0


def test_rule2_group_stage_zero_only_for_that_group():
    rows = [row(group="PRIME", is_group_stage=True, max_hours=8)]
    assert hours(rows, group="PRIME") == 0
    assert hours(rows, group="TERZE") == 6


def test_rule3_group_row_hours_and_closure():
    assert hours([row(group="PRIME", max_hours=4)]) == 4
    assert hours([row(group="PRIME", max_hours=5, is_closure=True)]) == 0


def test_rule4_whole_school_row_and_closure():
    assert hours([row(max_hours=3)]) == 3
    assert hours([row(max_hours=5, is_closure=True)]) == 0


def test_rule5_no_rows():
    assert hours([]) == 6


# Conflicts


def test_group_row_beats_whole_school_row():
    assert hours([row(max_hours=5), row(group="PRIME", max_hours=4)]) == 4


def test_whole_school_closure_does_not_override_group_row():
    rows = [row(is_closure=True, max_hours=0), row(group="PRIME", max_hours=5)]
    assert hours(rows) == 5


def test_group_stage_beats_group_row():
    rows = [row(group="PRIME", max_hours=5), row(group="PRIME", is_group_stage=True)]
    assert hours(rows) == 0


def test_class_stage_beats_group_stage():
    rows = [row(group="PRIME", is_group_stage=True), row(stage_class_id=1)]
    assert hours(rows) == 0


# No group


def test_class_without_group_uses_stage_then_school_then_default():
    for g in ("", None):
        rows = [row(group="PRIME", max_hours=2), row(max_hours=4)]
        assert hours(rows, group=g) == 4
        assert hours([row(group="PRIME", max_hours=2)], group=g) == 6
        assert hours(rows + [row(stage_class_id=1)], group=g) == 0
        assert hours([row(group="PRIME", is_group_stage=True)], group=g) == 6


# Cap


def test_cap():
    assert hours([row(group="PRIME", max_hours=8)]) == 6
    assert hours([row(group="PRIME", max_hours=6)]) == 6
    assert hours([row(group="PRIME", max_hours=4)]) == 4
    assert hours([row(max_hours=8)]) == 6
    assert hours([row(group="PRIME", max_hours=0)]) == 0


# Ignored rows


def test_other_date_ignored():
    rows = [
        row(date=OTHER, group="PRIME", max_hours=1),
        row(date=OTHER, stage_class_id=1),
        row(date=OTHER, is_closure=True),
    ]
    assert hours(rows) == 6


def test_other_class_stage_row_ignored():
    rows = [row(stage_class_id=2, max_hours=0, is_closure=True)]
    assert hours(rows, class_id=1) == 6
    assert hours(rows + [row(max_hours=4)], class_id=1) == 4
    assert hours(rows, class_id=2) == 0


def test_class_stage_row_with_group_not_used_as_group_row():
    rows = [row(group="PRIME", stage_class_id=2, max_hours=0, is_closure=True)]
    assert hours(rows, class_id=1) == 6


def test_rows_can_be_any_iterable():
    assert hours(iter([row(max_hours=3)])) == 3


# Start hour


def start(rows, group="PRIME"):
    return resolve_start_hour(rows, D, group)


def test_start_group_beats_school():
    rows = [row(start_hour_min=9), row(group="PRIME", start_hour_min=10)]
    assert start(rows) == 10


def test_start_school_used_when_group_row_has_none():
    rows = [row(start_hour_min=9), row(group="PRIME")]
    assert start(rows) == 9


def test_start_none_when_nothing():
    assert start([]) is None
    assert start([row(group="PRIME")]) is None


def test_start_class_without_group_uses_school_only():
    rows = [row(start_hour_min=9), row(group="PRIME", start_hour_min=11)]
    for g in ("", None):
        assert start(rows, group=g) == 9
        assert start([row(group="PRIME", start_hour_min=11)], group=g) is None


def test_start_other_date_and_class_stage_ignored():
    rows = [
        row(date=OTHER, start_hour_min=12),
        row(stage_class_id=1, start_hour_min=13),
    ]
    assert start(rows) is None


def test_start_stored_on_closed_and_stage_days():
    assert start([row(is_closure=True, max_hours=0, start_hour_min=9)]) == 9
    rows = [row(group="PRIME", is_group_stage=True, start_hour_min=10)]
    assert start(rows) == 10

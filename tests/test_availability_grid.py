import copy
import datetime

import pytest

from availability import grid as g

D = datetime.date
W1, W2, W3, W4 = D(2026, 9, 7), D(2026, 9, 14), D(2026, 9, 21), D(2026, 9, 28)


def grid_with(**changes):
    grid = g.default_grid()
    grid.update(changes)
    return grid


def slot(**kw):
    s = {"ora_inizio": "08:00", "ora_fine": "09:00", "disponibile": True}
    s.update(kw)
    return s


def test_default_grid_valid():
    g.validate_grid(g.default_grid())
    assert g.grid_errors(g.default_grid()) == []


def test_grid_with_false_slot_valid():
    grid = g.default_grid()
    grid["lunedi"][0]["disponibile"] = False
    g.validate_grid(grid)


def test_default_grid_shape():
    grid = g.default_grid()
    assert list(grid) == ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi"]
    for slots in grid.values():
        assert len(slots) == 6
        assert slots[0]["ora_inizio"] == "08:00"
        assert slots[0]["ora_fine"] == "09:00"
        assert slots[-1]["ora_inizio"] == "13:00"
        assert slots[-1]["ora_fine"] == "14:00"
        assert all(s["disponibile"] is True for s in slots)


def test_default_grid_is_fresh_each_time():
    a = g.default_grid()
    a["lunedi"][0]["disponibile"] = False
    a["sabato"] = []
    assert g.default_grid()["lunedi"][0]["disponibile"] is True
    assert "sabato" not in g.default_grid()


def test_any_non_empty_slot_list_accepted():
    g.validate_grid(grid_with(lunedi=[slot(ora_inizio="10:00", ora_fine="09:00")]))


@pytest.mark.parametrize("bad", [[], None, "x", 3, [("lunedi", [])]])
def test_not_a_dict_rejected(bad):
    with pytest.raises(ValueError):
        g.validate_grid(bad)


def test_missing_day_rejected():
    grid = g.default_grid()
    del grid["venerdi"]
    with pytest.raises(ValueError, match="venerdi"):
        g.validate_grid(grid)


@pytest.mark.parametrize("extra", ["sabato", "lunedì"])
def test_unknown_day_rejected(extra):
    grid = g.default_grid()
    grid[extra] = [slot()]
    with pytest.raises(ValueError):
        g.validate_grid(grid)


@pytest.mark.parametrize("empty", [[], None, {}, "x"])
def test_empty_or_bad_day_rejected(empty):
    with pytest.raises(ValueError, match="lunedi"):
        g.validate_grid(grid_with(lunedi=empty))


@pytest.mark.parametrize("bad", [None, "x", 1, ["a"], [slot(), 5]])
def test_slot_not_dict_rejected(bad):
    with pytest.raises(ValueError):
        g.validate_grid(grid_with(lunedi=bad if isinstance(bad, list) else [bad]))


@pytest.mark.parametrize("key", ["ora_inizio", "ora_fine", "disponibile"])
def test_slot_missing_key_rejected(key):
    s = slot()
    del s[key]
    with pytest.raises(ValueError, match=key):
        g.validate_grid(grid_with(lunedi=[s]))


def test_slot_extra_key_rejected():
    with pytest.raises(ValueError):
        g.validate_grid(grid_with(lunedi=[slot(nota="x")]))


@pytest.mark.parametrize("bad", [1, 0, "true", None, "false", 1.0])
def test_disponibile_must_be_bool(bad):
    with pytest.raises(ValueError, match="disponibile"):
        g.validate_grid(grid_with(lunedi=[slot(disponibile=bad)]))


@pytest.mark.parametrize(
    "bad",
    ["8:00", "25:00", "08:60", "08.00", "", "08:00 ", "08:00\n", 800, None, ["08:00"]],
)
@pytest.mark.parametrize("key", ["ora_inizio", "ora_fine"])
def test_bad_time_rejected(key, bad):
    with pytest.raises(ValueError, match=key):
        g.validate_grid(grid_with(lunedi=[slot(**{key: bad})]))


def test_grid_errors_never_raises_on_garbage():
    for junk in [None, 1, [], {"lunedi": 1}, {"lunedi": [None]}, {1: 2}]:
        assert g.grid_errors(junk)


def test_resolve_stored_even_first_time_and_earlier_records():
    mine = grid_with(lunedi=[slot(disponibile=False)])
    r = g.resolve_grid(W2, {W1: g.default_grid(), W2: mine}, True)
    assert (r.source, r.from_week, r.grid) == (g.STORED, None, mine)


def test_resolve_first_time_gets_default_despite_earlier():
    stored = {W1: grid_with(lunedi=[slot(disponibile=False)])}
    r = g.resolve_grid(W2, stored, True)
    assert r.source == g.DEFAULT and r.grid == g.default_grid()


def test_resolve_inherits_latest_earlier_ignores_future():
    a = grid_with(lunedi=[slot(disponibile=False)])
    b = grid_with(martedi=[slot(disponibile=False)])
    c = grid_with(venerdi=[slot(disponibile=False)])
    r = g.resolve_grid(W3, {W1: a, W2: b, W4: c}, False)
    assert (r.source, r.from_week, r.grid) == (g.INHERITED, W2, b)


def test_resolve_same_week_is_stored_not_inherited():
    r = g.resolve_grid(W2, {W2: g.default_grid()}, False)
    assert r.source == g.STORED


@pytest.mark.parametrize("stored", [{}, {W3: g.default_grid()}])
def test_resolve_default_when_nothing_earlier(stored):
    r = g.resolve_grid(W2, stored, False)
    assert r.source == g.DEFAULT and r.from_week is None
    assert r.grid == g.default_grid()


def test_resolve_returns_copy():
    stored = {W1: g.default_grid()}
    snapshot = copy.deepcopy(stored)
    r = g.resolve_grid(W1, stored, False)
    r.grid["lunedi"][0]["disponibile"] = False
    assert stored == snapshot
    r = g.resolve_grid(W2, stored, False)
    r.grid["lunedi"].clear()
    assert stored == snapshot


def test_resolve_non_monday_raises():
    with pytest.raises(ValueError):
        g.resolve_grid(D(2026, 9, 8), {}, False)
    with pytest.raises(ValueError):
        g.resolve_grid("2026-09-07", {}, False)


def test_resolve_malformed_stored_grid_reports_errors_without_raising():
    r = g.resolve_grid(W2, {W1: {"lunedi": None}}, False)
    assert r.source == g.INHERITED and r.errors
    r = g.resolve_grid(W1, {W1: "junk"}, True)
    assert r.source == g.STORED and r.errors
    assert g.resolve_grid(W1, {}, True).errors == []


def test_check_monday():
    g.check_monday(W1)
    with pytest.raises(ValueError):
        g.check_monday(D(2026, 9, 13))


@pytest.mark.parametrize(
    "total, filled, expected", [(3, 1, 33.3), (3, 2, 66.7), (4, 4, 100.0), (0, 0, 0)]
)
def test_completion_percentage(total, filled, expected):
    assert g.completion_percentage(total, filled) == expected


def test_weeks_start_on_monday():
    weeks = g.list_weeks(D(2026, 9, 7), D(2026, 9, 27))
    assert [w["start"] for w in weeks] == [W1, W2, W3]
    assert weeks[0]["end"] == D(2026, 9, 11)
    assert [w["week_num"] for w in weeks] == [1, 2, 3]


def test_weeks_start_on_tuesday_rolls_to_next_monday():
    weeks = g.list_weeks(D(2026, 9, 1), D(2026, 9, 20))
    assert weeks[0]["start"] == W1 and weeks[0]["week_num"] == 1


def test_weeks_year_end_on_monday_included_and_last_is_last_monday():
    weeks = g.list_weeks(D(2026, 9, 1), W2)
    assert weeks[-1]["start"] == W2
    weeks = g.list_weeks(D(2026, 9, 1), D(2026, 9, 20))
    assert weeks[-1]["start"] == W2


def test_weeks_no_monday_returns_empty():
    assert g.list_weeks(D(2026, 9, 8), D(2026, 9, 13)) == []


def test_weeks_end_is_friday():
    assert all(
        w["end"].weekday() == 4 for w in g.list_weeks(D(2026, 9, 1), D(2027, 6, 30))
    )


def test_module_does_not_import_django():
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(g.__file__).read_text())
    names = [
        n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in getattr(n, "names", [None])
    ]
    assert not [n for n in names if n and n.split(".")[0] == "django"]

import pytest

from school.importers import validate_all

T = [{"nome": "Rossi Mario", "tipo": "ASSUNTO", "email": None, "row": 2}]
C = [
    {"nome": "1A", "n_studenti": None, "gruppo": None, "row": 2},
    {"nome": "1B", "n_studenti": None, "gruppo": None, "row": 3},
]
S = [{"nome": "Mat", "tipo": "TEORIA", "peso_cognitivo": "ALTO", "row": 2}]


def asg(t="Rossi Mario", c="1A", s="Mat", row=2):
    return {
        "docente_nome": t,
        "classe_nome": c,
        "materia_nome": s,
        "ore_anno": 30,
        "row": row,
    }


def pair(a="1A", b="1B", s="Mat", row=2):
    return {"classe_a": a, "classe_b": b, "materia_nome": s, "note": None, "row": row}


A_OK = [asg(), asg(c="1B", row=3)]


def full():
    return {
        "calendar": [{"data": None, "stage_classe_id": "1A", "row": 2}],
        "teachers": T,
        "classes": C,
        "subjects": S,
        "assignments": A_OK,
        "pairings": [pair()],
    }


def test_fully_consistent():
    r = validate_all(full())
    assert r.errors == [] and r.warnings == [] and r.can_proceed


@pytest.mark.parametrize(
    ("kwargs", "name"),
    [({"t": "Nessuno"}, "Nessuno"), ({"c": "9Z"}, "9Z"), ({"s": "Fis"}, "Fis")],
)
def test_e1_each_entity(kwargs, name):
    data = {
        "teachers": T,
        "classes": C,
        "subjects": S,
        "assignments": [asg(row=7, **kwargs)],
    }
    r = validate_all(data)
    e = [m for m in r.errors if name in m.text]
    assert len(e) == 1
    assert e[0].file_type == "assignments" and e[0].row == 7
    assert not r.can_proceed


def test_name_normalisation_matches():
    for spelling in ("ROSSI MARIO", " rossi mario ", "Rossi   Mario"):
        r = validate_all({"teachers": T, "assignments": [asg(t=spelling)]})
        assert r.errors == []


@pytest.mark.parametrize(
    ("p", "name"),
    [
        (pair(a="9Z", row=4), "9Z"),
        (pair(b="9Z", row=4), "9Z"),
        (pair(s="Fis", row=4), "Fis"),
    ],
)
def test_e2(p, name):
    r = validate_all({"classes": C, "subjects": S, "pairings": [p]})
    e = [m for m in r.errors if name in m.text]
    assert len(e) == 1 and e[0].file_type == "pairings" and e[0].row == 4


def test_e3_lacks_assignment():
    for assignments, lacking in (
        ([asg(c="1B")], "1A"),
        ([asg(c="1A")], "1B"),
    ):
        r = validate_all({"assignments": assignments, "pairings": [pair(row=5)]})
        assert len(r.errors) == 1
        assert f"'{lacking}'" in r.errors[0].text and r.errors[0].row == 5
        assert not r.can_proceed
    assert validate_all({"assignments": A_OK, "pairings": [pair()]}).errors == []


def test_e4():
    cal = [
        {"stage_classe_id": "9Z", "row": 6},
        {"stage_classe_id": None, "row": 7},
        {"stage_classe_id": " 1a", "row": 8},
    ]
    r = validate_all({"calendar": cal, "classes": C})
    assert len(r.errors) == 1
    assert r.errors[0].file_type == "calendar" and r.errors[0].row == 6
    assert "9Z" in r.errors[0].text and not r.can_proceed


def test_warnings():
    t = T + [{"nome": "Idle", "tipo": "ASSUNTO", "email": None, "row": 3}]
    c = C + [{"nome": "1C", "n_studenti": None, "gruppo": None, "row": 4}]
    s = S + [{"nome": "Fis", "tipo": "TEORIA", "peso_cognitivo": "ALTO", "row": 3}]
    r = validate_all({"teachers": t, "classes": c, "subjects": s, "assignments": A_OK})
    got = {(w.file_type, w.row) for w in r.warnings}
    assert got == {("teachers", 3), ("classes", 4), ("subjects", 3)}
    assert r.errors == [] and r.can_proceed and r.warnings


@pytest.mark.parametrize("kind", ["teachers", "classes", "subjects"])
def test_each_warning_alone(kind):
    data = {"teachers": T, "classes": C, "subjects": S, "assignments": A_OK}
    extra = {"nome": "Extra", "row": 9, "tipo": "ASSUNTO", "peso_cognitivo": "ALTO"}
    data[kind] = data[kind] + [extra]
    r = validate_all(data)
    assert [(w.file_type, w.row) for w in r.warnings] == [(kind, 9)]


def test_partial_input_skips_rules():
    for data in (
        {"teachers": T},
        {"assignments": [asg()]},
        {},
        {"assignments": [asg()], "teachers": T},
        {"classes": C, "subjects": S},
        {"pairings": [pair()]},
        {"calendar": [{"stage_classe_id": "9Z", "row": 2}]},
    ):
        r = validate_all(data)
        assert r.errors == [] and r.warnings == [] and r.can_proceed, data


def test_partial_assignments_teachers_only_checks_teachers():
    r = validate_all({"assignments": [asg(t="Nessuno")], "teachers": T})
    assert len(r.errors) == 1 and "Nessuno" in r.errors[0].text
    assert [w.file_type for w in r.warnings] == ["teachers"]

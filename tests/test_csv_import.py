import ast
import datetime
from pathlib import Path

import pytest

from school.importers import (
    parse_assignments,
    parse_calendar,
    parse_classes,
    parse_pairings,
    parse_subjects,
    parse_teachers,
)

CAL = "data,ore_max_giornata,flag_chiusura,stage_classe_id,gruppo\n"


def errs(res):
    return [m for m in res.messages if m.severity == "error"]


def cal(line):
    line += "," * (4 - line.count(","))
    return parse_calendar(CAL + line + "\n")


# --- valid files ------------------------------------------------------------


def test_valid_calendar_row():
    res = parse_calendar(CAL + "2026-09-14,6,no,,PRIME\n2026-09-15,4,true,1A,\n")
    assert res.messages == []
    assert res.rows == [
        {
            "data": datetime.date(2026, 9, 14),
            "ore_max_giornata": 6,
            "flag_chiusura": False,
            "stage_classe_id": None,
            "gruppo": "PRIME",
            "row": 2,
        },
        {
            "data": datetime.date(2026, 9, 15),
            "ore_max_giornata": 4,
            "flag_chiusura": True,
            "stage_classe_id": "1A",
            "gruppo": None,
            "row": 3,
        },
    ]


def test_valid_teachers():
    res = parse_teachers(
        "nome,email,tipo\nRossi Mario,r@x.it,assunto\nBianchi,,CONTRATTO\n"
    )
    assert res.messages == []
    assert res.rows == [
        {"nome": "Rossi Mario", "tipo": "ASSUNTO", "email": "r@x.it", "row": 2},
        {"nome": "Bianchi", "tipo": "CONTRATTO", "email": None, "row": 3},
    ]


def test_valid_classes():
    res = parse_classes("nome,n_studenti,gruppo\n1A,25,prime\n2B,,\n")
    assert res.messages == []
    assert res.rows == [
        {"nome": "1A", "n_studenti": 25, "gruppo": "PRIME", "row": 2},
        {"nome": "2B", "n_studenti": None, "gruppo": None, "row": 3},
    ]


def test_valid_subjects():
    res = parse_subjects("nome,tipo,peso_cognitivo\nMat,teoria,alto\n")
    assert res.rows == [
        {"nome": "Mat", "tipo": "TEORIA", "peso_cognitivo": "ALTO", "row": 2}
    ]


def test_valid_assignments():
    res = parse_assignments(
        "docente_nome,classe_nome,materia_nome,ore_anno\nRossi,1A,Mat,30\n"
    )
    assert res.rows == [
        {
            "docente_nome": "Rossi",
            "classe_nome": "1A",
            "materia_nome": "Mat",
            "ore_anno": 30,
            "row": 2,
        }
    ]


def test_valid_pairings_with_and_without_note():
    res = parse_pairings(
        "classe_a,classe_b,materia_nome,note\n1A,1B,Mat,x\n1A,1C,Mat,\n"
    )
    assert res.messages == []
    assert [r["note"] for r in res.rows] == ["x", None]
    res = parse_pairings("classe_a,classe_b,materia_nome\n1A,1B,Mat\n")
    assert res.rows[0]["note"] is None


def test_output_keeps_trimmed_original_spelling():
    res = parse_teachers("nome,tipo\n  Rossi   Mario ,ASSUNTO\n")
    assert res.rows[0]["nome"] == "Rossi   Mario"


# --- headers ----------------------------------------------------------------


def test_header_ignores_case_and_spaces():
    res = parse_calendar(" Data , ORE_MAX_GIORNATA \n2026-09-14,5\n")
    assert res.messages == []
    assert res.rows[0]["ore_max_giornata"] == 5


def test_calendar_missing_required_column():
    res = parse_calendar("data\n2026-09-14\n")
    assert res.rows == []
    assert len(errs(res)) == 1
    assert "ore_max_giornata" in res.messages[0].text


def test_calendar_missing_optional_columns_ok():
    res = parse_calendar("data,ore_max_giornata\n2026-09-14,6\n")
    assert res.messages == []
    assert res.rows[0]["flag_chiusura"] is False
    assert res.rows[0]["gruppo"] is None


def test_calendar_repeated_column():
    res = parse_calendar("data,data,ore_max_giornata\n2026-09-14,2026-09-14,6\n")
    assert res.rows == []
    assert "'data'" in res.messages[0].text


def test_calendar_unknown_column_ignored():
    res = parse_calendar("data,ore_max_giornata,extra\n2026-09-14,6,zzz\n")
    assert res.messages == []
    assert "extra" not in res.rows[0]


@pytest.mark.parametrize(
    ("parser", "header", "missing"),
    [
        (parse_teachers, "nome\nx\n", "tipo"),
        (parse_classes, "n_studenti\n3\n", "nome"),
        (parse_subjects, "nome,tipo\nx,TEORIA\n", "peso_cognitivo"),
        (
            parse_assignments,
            "docente_nome,classe_nome,materia_nome\na,b,c\n",
            "ore_anno",
        ),
        (parse_pairings, "classe_a,classe_b\na,b\n", "materia_nome"),
    ],
)
def test_other_files_missing_column(parser, header, missing):
    res = parser(header)
    assert res.rows == []
    assert any(missing in m.text for m in errs(res))


# --- calendar values --------------------------------------------------------


@pytest.mark.parametrize("hours", ["4", "5", "6"])
def test_calendar_hours_accepted(hours):
    assert cal(f"2026-09-14,{hours}").messages == []


@pytest.mark.parametrize("hours", ["3", "7", "8", "0", "abc", ""])
def test_calendar_hours_rejected(hours):
    res = cal(f"2026-09-14,{hours}")
    assert res.rows == []
    assert errs(res)[0].row == 2


@pytest.mark.parametrize(
    "date", ["2026-13-01", "14/09/2026", "2026-9-14", "", "2026-02-30"]
)
def test_calendar_bad_dates(date):
    res = cal(f"{date},6")
    assert res.rows == []
    assert errs(res)[0].row == 2


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "Yes"])
def test_closure_true(value):
    assert cal(f"2026-09-14,6,{value}").rows[0]["flag_chiusura"] is True


@pytest.mark.parametrize("value", ["no", "", "maybe"])
def test_closure_false(value):
    assert cal(f"2026-09-14,6,{value}").rows[0]["flag_chiusura"] is False


def test_closure_row_still_needs_hours():
    assert cal("2026-09-14,,true").rows == []


@pytest.mark.parametrize("group", ["PRIME", "seconde", "Terze", "QUARTE", "prima4+2"])
def test_calendar_group_valid(group):
    assert cal(f"2026-09-14,6,,,{group}").rows[0]["gruppo"] == group.upper()


def test_calendar_group_empty_and_invalid():
    assert cal("2026-09-14,6,,,").rows[0]["gruppo"] is None
    res = cal("2026-09-14,6,,,QUINTE")
    assert res.rows == [] and errs(res)[0].row == 2


def test_calendar_stage_class_kept_as_string():
    assert cal("2026-09-14,6,,1A,").rows[0]["stage_classe_id"] == "1A"


# --- teachers, classes, subjects, assignments, pairings -----------------------


def test_teacher_tipo_and_name():
    assert parse_teachers("nome,tipo\nA,assunto\nB,contratto\n").messages == []
    res = parse_teachers("nome,tipo\nA,DOCENTE\n , ASSUNTO\n")
    assert res.rows == []
    assert [m.row for m in errs(res)] == [2, 3]


def test_teacher_email_absent_or_blank():
    assert parse_teachers("nome,tipo\nA,ASSUNTO\n").messages == []
    assert parse_teachers("nome,tipo,email\nA,ASSUNTO,\n").messages == []


@pytest.mark.parametrize("value", ["25", "0", ""])
def test_class_students_ok(value):
    assert parse_classes(f"nome,n_studenti\n1A,{value}\n").messages == []


@pytest.mark.parametrize("value", ["-1", "2.5", "abc"])
def test_class_students_bad(value):
    res = parse_classes(f"nome,n_studenti\n1A,{value}\n")
    assert res.rows == [] and errs(res)[0].row == 2


def test_class_group_and_name():
    assert parse_classes("nome,gruppo\n1A,QUINTE\n").rows == []
    assert parse_classes("nome,gruppo\n1A,\n").rows[0]["gruppo"] is None
    assert parse_classes("nome,gruppo\n ,PRIME\n").rows == []


def test_subjects_enums():
    for tipo in ("TEORIA", "PRATICA"):
        for peso in ("ALTO", "MEDIO", "BASSO"):
            assert parse_subjects(f"nome,tipo,peso_cognitivo\nM,{tipo},{peso}\n").rows
    res = parse_subjects("nome,tipo,peso_cognitivo\nM,LAB,ALTO\nN,TEORIA,ALTISSIMO\n")
    assert res.rows == []
    assert [m.row for m in errs(res)] == [2, 3]


A_HEAD = "docente_nome,classe_nome,materia_nome,ore_anno\n"


def test_assignment_hours():
    assert parse_assignments(A_HEAD + "a,b,c,30\n").rows
    for bad in ("0", "-5", "abc", "2.5", ""):
        res = parse_assignments(A_HEAD + f"a,b,c,{bad}\n")
        assert res.rows == [] and errs(res)[0].row == 2


@pytest.mark.parametrize("line", [",b,c,3", "a,,c,3", "a,b,,3"])
def test_assignment_blank_names(line):
    assert parse_assignments(A_HEAD + line + "\n").rows == []


P_HEAD = "classe_a,classe_b,materia_nome\n"


def test_pairing_same_class_rejected():
    assert parse_pairings(P_HEAD + "1A,1A,M\n").rows == []
    assert parse_pairings(P_HEAD + '"1A"," 1a ",M\n').rows == []


@pytest.mark.parametrize("line", [",1B,M", "1A,,M", "1A,1B,"])
def test_pairing_blank_names(line):
    assert parse_pairings(P_HEAD + line + "\n").rows == []


# --- malformed input --------------------------------------------------------


def test_empty_and_whitespace_only():
    for content in ("", "   \n\n  "):
        res = parse_calendar(content)
        assert res.rows == []
        assert len(res.messages) == 1 and res.messages[0].row is None


def test_header_only():
    res = parse_calendar(CAL)
    assert res.rows == [] and res.messages == []


def test_blank_lines_between_rows_keep_line_numbers():
    res = parse_calendar("data,ore_max_giornata\n\n2026-09-14,6\n\n2026-09-15,6\n")
    assert [r["row"] for r in res.rows] == [3, 5]


def test_bom_bytes_and_str():
    body = "data,ore_max_giornata\n2026-09-14,6\n"
    assert parse_calendar(("﻿" + body).encode("utf-8")).messages == []
    assert parse_calendar("﻿" + body).rows


def test_not_utf8():
    res = parse_teachers("nome,tipo\nCaffè,ASSUNTO\n".encode("cp1252"))
    assert res.rows == [] and len(errs(res)) == 1 and res.messages[0].row is None


def test_semicolon_delimiter_not_guessed():
    res = parse_calendar("data;ore_max_giornata\n2026-09-14;6\n")
    assert res.rows == [] and "data" in res.messages[0].text


def test_wrong_field_count():
    res = parse_calendar(
        "data,ore_max_giornata\n2026-09-14,6,7\n2026-09-15\n2026-09-16,6\n"
    )
    assert [m.row for m in errs(res)] == [2, 3]
    assert [r["row"] for r in res.rows] == [4]


def test_quoted_comma_and_newline():
    res = parse_teachers(
        'nome,tipo\n"Rossi, Mario",ASSUNTO\n"Bia\nnchi",ASSUNTO\nVerdi,ASSUNTO\n'
    )
    assert res.messages == []
    assert [r["nome"] for r in res.rows] == ["Rossi, Mario", "Bia\nnchi", "Verdi"]
    assert [r["row"] for r in res.rows] == [2, 3, 5]


def test_nul_byte_does_not_raise():
    parse_calendar("data,ore_max_giornata\n2026-09-14\x00,6\n")


# --- duplicates -------------------------------------------------------------


def test_calendar_duplicate_names_both_rows():
    res = parse_calendar(CAL + "2026-09-14,6,,,PRIME\n2026-09-14,5,,,prime\n")
    assert len(res.rows) == 1 and res.rows[0]["row"] == 2
    assert errs(res)[0].row == 3 and "2" in errs(res)[0].text


def test_calendar_same_date_different_stage_class_not_duplicate():
    res = parse_calendar(CAL + "2026-09-14,6,,1A,\n2026-09-14,6,,1B,\n")
    assert len(res.rows) == 2 and res.messages == []


def test_calendar_stage_and_school_row_not_duplicate():
    res = parse_calendar(CAL + "2026-09-14,6,,,\n2026-09-14,6,,1A,\n")
    assert len(res.rows) == 2 and res.messages == []


def test_calendar_same_stage_class_duplicate():
    res = parse_calendar(CAL + "2026-09-14,6,,1A,\n2026-09-14,6,,1a,\n")
    assert len(res.rows) == 1 and len(errs(res)) == 1


@pytest.mark.parametrize(
    ("parser", "head", "a", "b"),
    [
        (
            parse_teachers,
            "nome,tipo",
            "Rossi Mario,ASSUNTO",
            " rossi  mario ,CONTRATTO",
        ),
        (parse_classes, "nome", "1A", " 1a"),
        (
            parse_subjects,
            "nome,tipo,peso_cognitivo",
            "Mat,TEORIA,ALTO",
            "MAT,PRATICA,BASSO",
        ),
    ],
)
def test_name_duplicates(parser, head, a, b):
    res = parser(f"{head}\n{a}\n{b}\n")
    assert len(res.rows) == 1
    assert errs(res)[0].row == 3 and "2" in errs(res)[0].text


def test_assignment_duplicate_triple():
    res = parse_assignments(A_HEAD + "R,1A,M,3\n r ,1a,m,9\nR,1A,N,3\n")
    assert len(res.rows) == 2 and errs(res)[0].row == 3


def test_pairing_duplicates():
    res = parse_pairings(P_HEAD + "A,B,x\nB,A,x\nA,B,y\n")
    assert [r["row"] for r in res.rows] == [2, 4]
    assert errs(res)[0].row == 3


# --- purity -----------------------------------------------------------------


def test_importers_import_only_stdlib_and_siblings():
    import sys

    root = Path(__file__).resolve().parent.parent / "school" / "importers"
    files = list(root.glob("*.py"))
    assert len(files) >= 3
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import inside the package
                    continue
                mods = [node.module]
            else:
                continue
            for mod in mods:
                top = mod.split(".")[0]
                assert top in sys.stdlib_module_names, f"{path.name} imports {mod}"


# --- over-long digit strings never raise ------------------------------------


@pytest.mark.parametrize("digits", ["9" * 5000, "0" * 5000 + "5"])
def test_calendar_hours_overlong_digits_is_an_error(digits):
    res = cal(f"2026-09-14,{digits}")
    assert res.rows == [] and errs(res)[0].row == 2


@pytest.mark.parametrize("digits", ["9" * 5000, "0" * 5000 + "5"])
def test_class_students_overlong_digits_is_an_error(digits):
    res = parse_classes(f"nome,n_studenti\n1A,{digits}\n")
    assert res.rows == [] and errs(res)[0].row == 2


@pytest.mark.parametrize("digits", ["9" * 5000, "0" * 5000 + "5"])
def test_assignment_hours_overlong_digits_is_an_error(digits):
    res = parse_assignments(A_HEAD + f"a,b,c,{digits}\n")
    assert res.rows == [] and errs(res)[0].row == 2

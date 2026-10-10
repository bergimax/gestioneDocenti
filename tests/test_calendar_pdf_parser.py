import datetime
import io

import pytest
from reportlab.pdfgen import canvas

from school.importers import (
    CalendarPdfError,
    CalendarPdfNoDatesError,
    parse_calendar_pdf,
)
from school.importers.calendar_pdf import (
    CALENDARIO_PDF_GRUPPI_COLONNE,
    STAGE_ORE_GIORNATA,
)

GROUPS = ["PRIME", "SECONDE", "TERZE", "QUARTE", "PRIMA4+2"]


def make_pdf(pages):
    """pages: list of lists of (x, y, text)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(600, 800))
    for items in pages:
        c.setFont("Helvetica", 7)  # small enough for a date to end before x=140
        for x, y, text in items:
            c.drawString(x, y, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def row_items(y, label, cells=()):
    items = [(20, y, label)]
    for group, value in cells:
        lo, _ = CALENDARIO_PDF_GRUPPI_COLONNE[group]
        items.append((lo + 10, y, str(value)))
    return items


def by_key(res):
    return {(r["data"], r["gruppo"]): r for r in res.rows}


def test_happy_path():
    pdf = make_pdf(
        [
            row_items(
                700,
                "lunedì 14 settembre 2026",
                [(g, 6) for g in GROUPS],
            )
            + row_items(680, "martedì 15 settembre 2026", [("PRIME", 4)])
        ]
    )
    res = parse_calendar_pdf(pdf)
    rows = by_key(res)
    assert len(res.rows) == 10
    assert res.rows == sorted(
        res.rows, key=lambda r: (r["data"], GROUPS.index(r["gruppo"]))
    )
    r = rows[(datetime.date(2026, 9, 14), "TERZE")]
    assert r == {
        "data": datetime.date(2026, 9, 14),
        "gruppo": "TERZE",
        "ore_max_giornata": 6,
        "flag_chiusura": False,
        "stage_classe_id": None,
        "flag_stage_gruppo": False,
    }
    assert rows[(datetime.date(2026, 9, 15), "PRIME")]["ore_max_giornata"] == 4


def test_path_input(tmp_path):
    path = tmp_path / "c.pdf"
    path.write_bytes(make_pdf([row_items(700, "lunedì 14 settembre 2026")]))
    assert len(parse_calendar_pdf(path).rows) == 5


def test_stage_threshold():
    assert STAGE_ORE_GIORNATA == 8
    pdf = make_pdf(
        [
            row_items(
                700,
                "lunedì 14 settembre 2026",
                [("PRIME", 4), ("SECONDE", 5), ("TERZE", 7), ("QUARTE", 8)]
                + [("PRIMA4+2", 9)],
            )
        ]
    )
    rows = by_key(parse_calendar_pdf(pdf))
    d = datetime.date(2026, 9, 14)
    # 7 is not a stage but is returned as a number so the admin can see it.
    assert (
        rows[(d, "PRIME")]["ore_max_giornata"],
        rows[(d, "PRIME")]["flag_stage_gruppo"],
    ) == (4, False)
    assert rows[(d, "SECONDE")]["flag_stage_gruppo"] is False
    assert rows[(d, "TERZE")]["ore_max_giornata"] == 7
    assert rows[(d, "TERZE")]["flag_stage_gruppo"] is False
    assert rows[(d, "QUARTE")]["flag_stage_gruppo"] is True
    assert rows[(d, "PRIMA4+2")]["ore_max_giornata"] == 9
    assert rows[(d, "PRIMA4+2")]["flag_stage_gruppo"] is True


def test_empty_cell():
    pdf = make_pdf([row_items(700, "lunedì 14 settembre 2026", [("PRIME", 6)])])
    r = by_key(parse_calendar_pdf(pdf))[(datetime.date(2026, 9, 14), "SECONDE")]
    assert r["ore_max_giornata"] == 0
    assert r["flag_stage_gruppo"] is False


def test_missing_weekdays_become_closures():
    # Fri 18 Sep, then Mon 21 (Sat/Sun skipped), Tue 22 missing, Wed 23 present.
    pdf = make_pdf(
        [
            row_items(700, "venerdì 18 settembre 2026")
            + row_items(680, "lunedì 21 settembre 2026")
            + row_items(660, "mercoledì 23 settembre 2026")
        ]
    )
    res = parse_calendar_pdf(pdf)
    closures = [r for r in res.rows if r["flag_chiusura"]]
    assert closures == [
        {
            "data": datetime.date(2026, 9, 22),
            "gruppo": None,
            "ore_max_giornata": 0,
            "flag_chiusura": True,
            "stage_classe_id": None,
            "flag_stage_gruppo": False,
        }
    ]
    assert not any(r["data"].weekday() >= 5 for r in res.rows)


def test_side_table_ignored():
    pdf = make_pdf(
        [
            row_items(700, "lunedì 14 settembre 2026", [("PRIME", 6)])
            + [(420, 700, "6"), (450, 650, "martedì 15 settembre 2026")]
            + [(430, 600, "lunedì 28 settembre 2026")]
        ]
    )
    res = parse_calendar_pdf(pdf)
    assert {r["data"] for r in res.rows} == {datetime.date(2026, 9, 14)}
    assert by_key(res)[(datetime.date(2026, 9, 14), "PRIME")]["ore_max_giornata"] == 6


def test_all_months_and_unreadable_date_skipped():
    names = [
        "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
        "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
    ]  # fmt: skip
    items = [(20, 780 - 15 * i, f"giovedì 1 {m} 2026") for i, m in enumerate(names)]
    items.append((20, 560, "lunedì 31 foobar 2026"))
    items.append((20, 540, "lunedì 31 febbraio 2026"))
    res = parse_calendar_pdf(make_pdf([items]))
    assert {r["data"].month for r in res.rows if not r["flag_chiusura"]} == set(
        range(1, 13)
    )


def test_repeated_date_reported_not_duplicated():
    pdf = make_pdf(
        [
            row_items(700, "lunedì 14 settembre 2026", [("PRIME", 6)])
            + row_items(680, "lunedì 14 settembre 2026", [("PRIME", 5)])
        ]
    )
    res = parse_calendar_pdf(pdf)
    keys = [(r["data"], r["gruppo"]) for r in res.rows]
    assert len(keys) == len(set(keys)) == 5
    assert [m.severity for m in res.messages] == ["warning"]


def test_multi_page_continues():
    pdf = make_pdf(
        [
            row_items(700, "venerdì 18 settembre 2026"),
            row_items(700, "lunedì 21 settembre 2026"),
        ]
    )
    res = parse_calendar_pdf(pdf)
    assert {r["data"] for r in res.rows} == {
        datetime.date(2026, 9, 18),
        datetime.date(2026, 9, 21),
    }


def test_no_dates_found():
    with pytest.raises(CalendarPdfNoDatesError, match="parser diverso"):
        parse_calendar_pdf(make_pdf([[(20, 700, "nessuna data qui")]]))
    with pytest.raises(CalendarPdfNoDatesError):
        parse_calendar_pdf(make_pdf([[]]))


@pytest.mark.parametrize("data", [b"not a pdf", b""])
def test_non_pdf_input(data):
    with pytest.raises(CalendarPdfError) as exc:
        parse_calendar_pdf(data)
    assert not isinstance(exc.value, CalendarPdfNoDatesError)


def test_no_django_models_imported():
    import school.importers.calendar_pdf as mod

    source = open(mod.__file__).read()
    assert "models" not in source.replace("Django models", "")
    assert "django" not in source.lower().replace("django models", "")

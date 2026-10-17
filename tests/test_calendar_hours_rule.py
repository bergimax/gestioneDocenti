"""One hours rule for every parked calendar row, whatever its source (#76)."""

import html as htmllib

import pytest
from test_calendar_ai import AI_ON, ai_rows, calendar_upload, read_ai
from test_setup_wizard import CSVS, HX, correct, db_counts, login, save, upload

from school.importers import parse_calendar
from school.importers.parsers import calendar_hours_error
from school.models import CalendarEntry, ParkedUpload


@pytest.fixture
def admin(db):
    return login("ADMIN")


def html(response):
    return htmllib.unescape(response.content.decode())


def message(value):
    return f"ore_max_giornata '{value}' non valido: ammessi 4, 5 o 6"


def others(admin):
    for file_type, content in CSVS.items():
        if file_type != "calendar":
            upload(admin, file_type, content)


def csv_calendar(hours, group=""):
    return f"data,ore_max_giornata,gruppo\n2026-09-13,6,\n2026-09-14,{hours},{group}\n"


def errors_of(source, hours, admin):
    """The preview's error list for one lesson-day row of `source`."""
    if source == "csv":
        r = upload(admin, "calendar", csv_calendar(hours), **HX)
    else:
        rows = ai_rows(("2026-09-13", "PRIME", 6, False))
        rows += ai_rows(("2026-09-14", "PRIME", hours, False))
        r, _ = read_ai(admin, rows, **HX)
    return r, html(r)


@AI_ON
@pytest.mark.parametrize("source", ["csv", "ai"])
@pytest.mark.parametrize("hours", [1, 2, 3, 7, 9])
def test_bad_hours_same_message_and_blocking(admin, source, hours):
    r, page = errors_of(source, hours, admin)
    # CSV rows are numbered by file line (header included), AI rows from 1.
    line = 3 if source == "csv" else 2
    assert f"Riga {line}: {message(hours)}" in page
    others(admin)
    before = db_counts()
    assert save(admin).status_code == 422
    assert db_counts() == before
    assert not CalendarEntry.objects.exists()


@AI_ON
@pytest.mark.parametrize("hours", [-1, 3.5, "abc", None, True])
def test_ai_unusable_hours_is_an_error_not_a_crash(admin, hours):
    r, _ = read_ai(admin, ai_rows(("2026-09-14", "PRIME", 6, False)), **HX)
    rows = ParkedUpload.objects.get(file_type="calendar").rows
    rows[0]["ore_max_giornata"] = hours
    ParkedUpload.objects.filter(file_type="calendar").update(rows=rows)
    others(admin)
    page = html(admin.get("/setup/validate/", **HX))
    assert f"Riga 1: {message(hours)}" in page
    assert save(admin).status_code == 422
    assert not CalendarEntry.objects.exists()


@AI_ON
def test_ai_valid_values_pass_and_save(admin):
    rows = ai_rows(
        ("2026-09-14", "PRIME", 4, False),
        ("2026-09-14", "SECONDE", 5, False),
        ("2026-09-14", "TERZE", 6, False),
        ("2026-09-14", "QUARTE", 0, False),
        ("2026-09-14", "PRIMA4+2", 8, False),
        ("2026-12-25", None, 0, True),
    )
    read_ai(admin, rows, **HX)
    others(admin)
    assert "non valido" not in html(admin.get("/setup/validate/", **HX))
    assert save(admin).status_code == 302
    assert CalendarEntry.objects.count() == 6
    assert CalendarEntry.objects.filter(is_group_stage=True).count() == 1


@AI_ON
@pytest.mark.parametrize(
    "spec",
    [
        ("2026-09-14", None, 0, False),
        ("2026-09-14", None, 8, False),
    ],
)
def test_ai_zero_or_eight_without_group_or_closure(admin, spec):
    r, _ = read_ai(admin, ai_rows(spec), **HX)
    others(admin)
    page = html(admin.get("/setup/validate/", **HX))
    assert f"Riga 1: {message(spec[2])}" in page
    assert save(admin).status_code == 422


@AI_ON
def test_stage_flag_with_wrong_hours_is_an_error(admin):
    rows = ai_rows(("2026-09-14", "PRIME", 6, False))
    rows[0]["flag_stage_gruppo"] = True
    read_ai(admin, rows, **HX)
    others(admin)
    page = html(admin.get("/setup/validate/", **HX))
    assert "Riga 1: lo stage di gruppo richiede 8 ore" in page
    assert save(admin).status_code == 422


@pytest.mark.parametrize("hours", [0, 8])
def test_csv_still_refuses_zero_and_eight(admin, hours):
    result = parse_calendar(csv_calendar(hours))
    assert [m.text for m in result.messages] == [message(hours)]
    assert [r["ore_max_giornata"] for r in result.rows] == [6]


def test_csv_closure_with_normal_hours_passes():
    result = parse_calendar("data,ore_max_giornata,flag_chiusura\n2026-12-25,4,true\n")
    assert result.messages == []


@pytest.mark.parametrize("hours", ["0", "8"])
def test_manual_date_still_refuses_zero_and_eight(admin, hours):
    upload(admin, "calendar", CSVS["calendar"])
    r = admin.post("/setup/add-date/", {"date": "2026-10-01", "hours": hours}, **HX)
    assert r.status_code == 422
    assert len(calendar_upload().rows) == 3


def test_rule_function_cases():
    def err(hours, gruppo=None, chiusura=False, stage=False, structured=True):
        return calendar_hours_error(
            hours, gruppo=gruppo, chiusura=chiusura, stage=stage, structured=structured
        )

    assert err(0, chiusura=True) is None
    assert err(0, gruppo="PRIME") is None
    assert err(8, gruppo="PRIME", stage=True) is None
    assert err(8, gruppo="PRIME") == message(8)
    assert err(0, structured=False, gruppo="PRIME") == message(0)
    assert err(6, gruppo="PRIME", stage=True) == "lo stage di gruppo richiede 8 ore"


# --- live correction -----------------------------------------------------------


@AI_ON
@pytest.mark.parametrize("value", ["3", "7", "-1", "x", "1.5"])
def test_correction_refuses_bad_hours_and_keeps_the_row(admin, value):
    read_ai(admin, ai_rows(("2026-09-14", "PRIME", 6, False)))
    r = correct(admin, "calendar", 1, "ore_max_giornata", value)
    assert r.status_code == 422
    assert message(value) in html(r)
    assert calendar_upload().rows[0]["ore_max_giornata"] == 6


@AI_ON
def test_correction_recomputes_stage_flag(admin):
    read_ai(admin, ai_rows(("2026-09-14", "PRIME", 9, False)))
    assert correct(admin, "calendar", 1, "ore_max_giornata", "8").status_code == 200
    assert calendar_upload().rows[0]["flag_stage_gruppo"] is True
    assert correct(admin, "calendar", 1, "ore_max_giornata", "6").status_code == 200
    assert calendar_upload().rows[0]["flag_stage_gruppo"] is False
    assert calendar_upload().rows[0]["ore_max_giornata"] == 6


@AI_ON
def test_correction_to_eight_on_a_closure_row_is_refused(admin):
    read_ai(admin, ai_rows(("2026-12-25", None, 0, True)))
    r = correct(admin, "calendar", 1, "ore_max_giornata", "8")
    assert r.status_code == 422
    assert calendar_upload().rows[0]["ore_max_giornata"] == 0


def test_csv_correction_to_zero_is_refused(admin):
    upload(admin, "calendar", CSVS["calendar"])
    r = correct(admin, "calendar", 1, "ore_max_giornata", "0")
    assert r.status_code == 422
    assert calendar_upload().rows[0]["ore_max_giornata"] == 6

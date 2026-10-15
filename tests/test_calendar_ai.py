"""The AI reading of the calendar PDF: ai/ function, Draft lifecycle, wizard."""

import datetime
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import anthropic
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from test_setup_wizard import (
    CSVS,
    HX,
    correct,
    db_counts,
    html,
    login,
    make_pdf,
    pdf_row,
    save,
    upload,
)

from ai import calendar_pdf
from ai.calendar_pdf import AiError, read_calendar_pdf
from config.env import get_optional_str
from school.models import CalendarEntry, Draft, ParkedUpload


@pytest.fixture
def admin(db):
    return login("ADMIN")


@pytest.fixture
def seg(db):
    return login("SEGRETERIA")


KEY = "sk-ant-test-secret-key"
PDF = make_pdf([(20, 700, "SENTINEL-PDF-CONTENT")])
READ = "school.setup_service.read_calendar_pdf"
AI_ON = override_settings(ANTHROPIC_API_KEY=KEY)


def ai_rows(*specs):
    """Rows as `read_calendar_pdf` returns them: (data, gruppo, ore, chiusura)."""
    return [
        {
            "data": data,
            "gruppo": group,
            "ore_max_giornata": hours,
            "flag_chiusura": closure,
            "stage_classe_id": None,
            "flag_stage_gruppo": group is not None and hours >= 8,
        }
        for data, group, hours, closure in specs
    ]


GOOD = ai_rows(
    ("2026-09-14", "PRIME", 6, False),
    ("2026-09-14", "SECONDE", 5, False),
    ("2026-12-25", None, 0, True),
)


def read_ai(client, rows=GOOD, name="cal.pdf", content=PDF, **extra):
    with mock.patch(READ, return_value=rows) as fn:
        r = client.post(
            "/setup/upload/calendar/",
            {"file": SimpleUploadedFile(name, content), "ai": "1"},
            **extra,
        )
    return r, fn


def calendar_upload():
    return ParkedUpload.objects.get(file_type="calendar")


# --- configuration --------------------------------------------------------------


def test_optional_str_env():
    assert get_optional_str({}, "K") == ""
    assert get_optional_str({"K": "  "}, "K", "d") == "d"
    assert get_optional_str({"K": " v "}, "K", "d") == "v"
    example = Path(".env.example").read_text()
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_TIMEOUT_SECONDS"):
        assert f"\n{name}=" in example


def test_app_starts_without_a_key():
    from test_settings import _check

    assert _check({}, drop=("ANTHROPIC_API_KEY",)).returncode == 0
    assert _check({"ANTHROPIC_API_KEY": ""}).returncode == 0
    assert _check({"ANTHROPIC_TIMEOUT_SECONDS": "0"}).returncode != 0


def test_ai_package_has_no_models_or_views():
    root = Path("ai")
    assert not (root / "models.py").exists()
    assert not (root / "views.py").exists()
    for path in root.glob("*.py"):
        text = path.read_text()
        assert "django.db" not in text
        assert "school.models" not in text


# --- the ai/ function -----------------------------------------------------------


def fake_client(text=None, raises=None):
    client = mock.Mock()
    if raises:
        client.messages.create.side_effect = raises
    else:
        block = SimpleNamespace(type="text", text=text)
        client.messages.create.return_value = SimpleNamespace(content=[block])
    return client


DAYS = json.dumps(
    {
        "giorni": [
            {"data": "2026-09-14", "chiusura": False, "ore": {"PRIME": 6, "TERZE": 8}},
            {"data": "2026-12-25", "chiusura": True, "ore": {}},
        ]
    }
)


@override_settings(ANTHROPIC_API_KEY=KEY, ANTHROPIC_MODEL="m-test")
def test_read_calendar_pdf_returns_plain_rows():
    client = fake_client(f"```json\n{DAYS}\n```")
    with mock.patch.object(calendar_pdf, "build_client", return_value=client):
        rows = read_calendar_pdf(PDF)
    assert rows == [
        {
            "data": "2026-09-14",
            "gruppo": "PRIME",
            "ore_max_giornata": 6,
            "flag_chiusura": False,
            "stage_classe_id": None,
            "flag_stage_gruppo": False,
        },
        {
            "data": "2026-09-14",
            "gruppo": "TERZE",
            "ore_max_giornata": 8,
            "flag_chiusura": False,
            "stage_classe_id": None,
            "flag_stage_gruppo": True,
        },
        {
            "data": "2026-12-25",
            "gruppo": None,
            "ore_max_giornata": 0,
            "flag_chiusura": True,
            "stage_classe_id": None,
            "flag_stage_gruppo": False,
        },
    ]
    client.messages.create.assert_called_once()
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["model"] == "m-test"
    document = kwargs["messages"][0]["content"][0]
    assert document["source"]["media_type"] == "application/pdf"


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"giorni": "x"}',
        '{"giorni": [{"data": 5}]}',
        '{"giorni": [{"data": "2026-09-14", "ore": {"PRIME": "six"}}]}',
        '{"giorni": [{"data": "2026-09-14", "ore": {"PRIME": true}}]}',
        '{"giorni": [{"data": "2026-09-14", "chiusura": "si"}]}',
    ],
)
def test_malformed_model_output_raises(text):
    with mock.patch.object(
        calendar_pdf, "build_client", return_value=fake_client(text)
    ):
        with pytest.raises(AiError):
            read_calendar_pdf(PDF)


def test_api_errors_and_timeouts_raise_ai_error(caplog):
    request = mock.Mock()
    for error in (
        anthropic.APITimeoutError(request=request),
        anthropic.APIConnectionError(message=f"boom {KEY}", request=request),
    ):
        client = fake_client(raises=error)
        with mock.patch.object(calendar_pdf, "build_client", return_value=client):
            with caplog.at_level(logging.DEBUG):
                with pytest.raises(AiError):
                    read_calendar_pdf(PDF)
    assert KEY not in caplog.text


def test_a_real_client_is_never_built_in_tests():
    with pytest.raises(AssertionError, match="real Anthropic client"):
        calendar_pdf.build_client()


# --- no key: the feature is absent -----------------------------------------------


def test_without_a_key_nothing_ai_is_shown_or_run(admin):
    assert html(admin.get("/setup/")).count("Leggi con AI") == 0
    assert "bozza AI" not in html(admin.get("/setup/"))
    r, fn = read_ai(admin)
    assert r.status_code == 422
    assert "non è disponibile" in html(r)
    fn.assert_not_called()
    assert Draft.objects.count() == 0
    assert ParkedUpload.objects.count() == 0


@override_settings(ANTHROPIC_API_KEY="")
def test_without_a_key_the_deterministic_flow_is_unchanged(admin):
    pdf = make_pdf(pdf_row(700, "lunedì 14 settembre 2026"))
    assert upload(admin, "calendar", pdf, name="c.pdf", **HX).status_code == 200
    assert calendar_upload().source == "pdf"
    blank = make_pdf([(20, 700, "nothing here")])
    r = upload(admin, "calendar", blank, name="b.pdf", **HX)
    assert r.status_code == 422
    assert "Leggi con AI" not in html(r)


# --- with a key ---------------------------------------------------------------------


@AI_ON
def test_button_is_shown_beside_carica(admin):
    page = html(admin.get("/setup/"))
    assert page.count("Leggi con AI") >= 1
    assert page.count('name="ai"') == 1
    assert page.count(">Carica<") == 6


@AI_ON
def test_no_rows_error_suggests_the_ai(admin):
    r = upload(admin, "calendar", make_pdf([(20, 700, "x")]), name="b.pdf", **HX)
    assert r.status_code == 422
    assert "Puoi riprovare con «Leggi con AI»" in html(r)


@AI_ON
def test_plain_carica_never_calls_the_ai(admin):
    with mock.patch(READ) as fn:
        upload(admin, "calendar", make_pdf([(20, 700, "x")]), name="b.pdf", **HX)
        upload(admin, "calendar", CSVS["calendar"], **HX)
    fn.assert_not_called()


@AI_ON
def test_ai_read_creates_one_draft_and_shows_marked_rows(admin):
    before = db_counts()
    r, fn = read_ai(admin, **HX)
    assert r.status_code == 200
    fn.assert_called_once_with(PDF)
    draft = Draft.objects.get()
    assert draft.kind == Draft.Kind.CALENDAR_ROWS
    assert draft.source == "cal.pdf"
    assert draft.status == Draft.Status.DRAFT
    assert draft.payload == GOOD
    up = calendar_upload()
    assert up.source == "ai"
    assert len(up.rows) == 3
    page = html(r)
    assert page.count("bozza AI") == 3
    assert "2026-09-14" in page
    assert db_counts() == before


@AI_ON
def test_ai_rows_take_the_same_cell_corrections(admin):
    read_ai(admin)
    r = correct(admin, "calendar", 2, "ore_max_giornata", "4")
    assert r.status_code == 200
    assert calendar_upload().rows[1]["ore_max_giornata"] == 4
    r = correct(admin, "calendar", 2, "gruppo", "nope")
    assert r.status_code == 422
    assert calendar_upload().rows[1]["gruppo"] == "SECONDE"


@AI_ON
def test_invalid_ai_rows_are_listed_and_block_saving(admin):
    rows = ai_rows(
        ("2026-13-45", "PRIME", 6, False),
        ("2026-09-15", "PRIME", 9, False),
        ("2026-09-16", "MEDIE", 6, False),
        ("2026-09-17", "PRIME", 6, False),
    )
    r, _ = read_ai(admin, rows, **HX)
    page = html(r)
    assert "Riga 1: data" in page
    assert "Riga 2: ore_max_giornata" in page
    assert "Riga 3: gruppo" in page
    assert "Riga 4" not in page.split("Errori")[1].split("</ul>")[0]
    for ft, content in CSVS.items():
        if ft != "calendar":
            upload(admin, ft, content)
    assert save(admin).status_code == 422
    assert School_count() == 0
    # Corrected in the preview, the rows pass.
    assert correct(admin, "calendar", 1, "data", "2026-09-18").status_code == 200
    assert correct(admin, "calendar", 2, "ore_max_giornata", "5").status_code == 200
    assert correct(admin, "calendar", 3, "gruppo", "terze").status_code == 200
    assert save(admin).status_code == 302
    assert CalendarEntry.objects.count() == 4


def School_count():
    from school.models import School

    return School.objects.count()


@AI_ON
def test_duplicate_ai_rows_are_errors(admin):
    rows = ai_rows(("2026-09-14", "PRIME", 6, False), ("2026-09-14", "PRIME", 5, False))
    r, _ = read_ai(admin, rows, **HX)
    assert "duplicate of row 1" in html(r)


@AI_ON
def test_empty_result_changes_nothing(admin):
    upload(admin, "calendar", CSVS["calendar"])
    before = calendar_upload().rows
    r, fn = read_ai(admin, rows=[], **HX)
    assert r.status_code == 422
    assert "nessuna riga letta" in html(r)
    fn.assert_called_once()
    assert Draft.objects.count() == 0
    assert calendar_upload().rows == before
    assert calendar_upload().source == "csv"
    assert CalendarEntry.objects.count() == 0


@AI_ON
@pytest.mark.parametrize(
    "failure",
    [
        AiError("Il servizio AI non è raggiungibile: riprova più tardi."),
        RuntimeError(KEY),
    ],
)
def test_ai_failure_is_a_message_and_parks_nothing(admin, failure, caplog):
    with mock.patch(READ, side_effect=failure):
        with caplog.at_level(logging.DEBUG):
            r = admin.post(
                "/setup/upload/calendar/",
                {"file": SimpleUploadedFile("c.pdf", PDF), "ai": "1"},
                **HX,
            )
    assert r.status_code == 422
    assert KEY not in html(r) + caplog.text
    assert 'id="setup-wizard"' in html(r)
    assert Draft.objects.count() == 0
    assert ParkedUpload.objects.count() == 0


@AI_ON
def test_bad_json_from_the_client_is_a_message(admin):
    client = fake_client("this is not json")
    with mock.patch.object(calendar_pdf, "build_client", return_value=client):
        r = admin.post(
            "/setup/upload/calendar/",
            {"file": SimpleUploadedFile("c.pdf", PDF), "ai": "1"},
            **HX,
        )
    assert r.status_code == 422
    assert "non è valida: nessuna riga" in html(r)
    assert Draft.objects.count() == 0
    assert ParkedUpload.objects.count() == 0


@AI_ON
def test_timeout_from_the_client_is_a_message(admin):
    request = mock.Mock()
    client = fake_client(raises=anthropic.APITimeoutError(request=request))
    with mock.patch.object(calendar_pdf, "build_client", return_value=client):
        r = admin.post(
            "/setup/upload/calendar/",
            {"file": SimpleUploadedFile("c.pdf", PDF), "ai": "1"},
            **HX,
        )
    assert r.status_code == 422
    assert "in tempo" in html(r)
    assert Draft.objects.count() == 0


@AI_ON
@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("c.csv", b"data\n", "Estensione"),
        ("c.pdf", b"", "vuoto"),
        ("c.pdf", b"   \n", "vuoto"),
        ("c.pdf", b"not a pdf at all", "non è un PDF"),
    ],
)
def test_unusable_uploads_never_reach_the_ai(admin, name, content, expected):
    r, fn = read_ai(admin, name=name, content=content, **HX)
    assert r.status_code == 422
    assert expected in html(r)
    fn.assert_not_called()
    assert Draft.objects.count() == 0
    assert ParkedUpload.objects.count() == 0


@AI_ON
@override_settings(SETUP_MAX_UPLOAD_BYTES=100)
def test_oversized_upload_never_reaches_the_ai(admin):
    r, fn = read_ai(admin, content=PDF + b" " * 200, **HX)
    assert r.status_code == 422
    assert "dimensione massima" in html(r)
    fn.assert_not_called()


@AI_ON
def test_zero_page_pdf_never_reaches_the_ai(admin):
    empty = mock.MagicMock()
    empty.__enter__.return_value = SimpleNamespace(pages=[])
    with mock.patch("school.setup_service.pdfplumber.open", return_value=empty):
        r, fn = read_ai(admin, **HX)
    assert r.status_code == 422
    assert "pagine" in html(r)
    fn.assert_not_called()


@AI_ON
def test_ai_for_another_file_type_is_refused(admin):
    with mock.patch(READ) as fn:
        r = admin.post(
            "/setup/upload/teachers/",
            {"file": SimpleUploadedFile("t.pdf", PDF), "ai": "1"},
            **HX,
        )
    assert r.status_code == 422
    fn.assert_not_called()


# --- the draft lifecycle -----------------------------------------------------------


@AI_ON
def test_replacing_the_calendar_discards_the_previous_draft(admin):
    read_ai(admin)
    first = Draft.objects.get()
    upload(admin, "calendar", CSVS["calendar"])
    first.refresh_from_db()
    assert first.status == Draft.Status.DISCARDED
    read_ai(admin)
    read_ai(admin, rows=GOOD[:1])
    statuses = list(Draft.objects.order_by("id").values_list("status", flat=True))
    assert statuses == ["DISCARDED", "DISCARDED", "DRAFT"]


@AI_ON
def test_save_confirms_the_draft(admin):
    read_ai(admin)
    for ft, content in CSVS.items():
        if ft != "calendar":
            upload(admin, ft, content)
    assert save(admin).status_code == 302
    assert Draft.objects.get().status == Draft.Status.CONFIRMED
    assert CalendarEntry.objects.count() == 3


@AI_ON
def test_a_failed_save_leaves_the_draft(admin):
    read_ai(admin)
    assert save(admin).status_code == 422
    assert Draft.objects.get().status == Draft.Status.DRAFT
    for ft, content in CSVS.items():
        if ft != "calendar":
            upload(admin, ft, content)
    with mock.patch(
        "school.setup_service._create_pairings", side_effect=RuntimeError("boom")
    ):
        assert save(admin).status_code == 500
    assert Draft.objects.get().status == Draft.Status.DRAFT
    assert CalendarEntry.objects.count() == 0


# --- privacy and access --------------------------------------------------------------


@AI_ON
def test_bytes_and_key_are_never_stored_logged_or_rendered(admin, caplog):
    with caplog.at_level(logging.DEBUG):
        r, _ = read_ai(admin, **HX)
        page = html(r) + html(admin.get("/setup/"))
    assert "SENTINEL-PDF-CONTENT" not in page + caplog.text
    assert KEY not in page + caplog.text
    stored = json.dumps(list(ParkedUpload.objects.values("rows", "messages")))
    stored += json.dumps(list(Draft.objects.values("payload", "source")))
    assert "SENTINEL" not in stored
    assert "PDF" not in stored.replace("pdf", "")  # no raw bytes marker


@AI_ON
def test_segreteria_is_denied(seg):
    r, fn = read_ai(seg)
    assert r.status_code == 403
    fn.assert_not_called()
    assert Draft.objects.count() == 0


@AI_ON
def test_anonymous_is_sent_to_login(client):
    r, fn = read_ai(client)
    assert r.status_code == 302
    assert "login" in r["Location"]
    fn.assert_not_called()


def test_draft_defaults(db):
    d = Draft.objects.create(kind=Draft.Kind.CALENDAR_ROWS, source="x")
    assert d.status == "DRAFT"
    assert d.payload == []
    assert isinstance(d.created_at, datetime.datetime)

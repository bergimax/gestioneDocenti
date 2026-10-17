import datetime
import io
import re
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from reportlab.pdfgen import canvas

from accounts.models import User
from school.importers.calendar_pdf import CALENDARIO_PDF_GRUPPI_COLONNE
from school.models import (
    Assignment,
    CalendarEntry,
    Class,
    ClassPairing,
    ParkedUpload,
    School,
    Subject,
    Teacher,
)

PW = "correct-horse"
HX = {"HTTP_HX_REQUEST": "true"}
SETUP = "/setup/"
SAVE = "/setup/save/"

CSVS = {
    "calendar": "data,ore_max_giornata,flag_chiusura\n"
    "2026-09-14,6,false\n2026-09-15,5,false\n2026-12-25,4,true\n",
    "teachers": "nome,email,tipo\nROSSI MARIO,m@example.com,ASSUNTO\n"
    "BIANCHI LUCA,,CONTRATTO\n",
    "classes": "nome,n_studenti,gruppo\n1A,20,PRIME\n2A,18,SECONDE\n",
    "subjects": "nome,tipo,peso_cognitivo\nMatematica,TEORIA,ALTO\n"
    "Laboratorio,PRATICA,BASSO\n",
    "assignments": "docente_nome,classe_nome,materia_nome,ore_anno\n"
    "ROSSI MARIO,1A,Matematica,60\nROSSI MARIO,2A,Matematica,50\n"
    "BIANCHI LUCA,1A,Laboratorio,30\nBIANCHI LUCA,2A,Laboratorio,30\n",
    "pairings": "classe_a,classe_b,materia_nome,note\n1A,2A,Matematica,insieme\n",
}
COUNTS = {
    "calendar": 3,
    "teachers": 2,
    "classes": 2,
    "subjects": 2,
    "assignments": 4,
    "pairings": 1,
}


def login(role):
    User.objects.create_user(f"{role.lower()}@example.com", PW, role)
    c = Client()
    c.force_login(User.objects.get(email=f"{role.lower()}@example.com"))
    return c


@pytest.fixture
def admin(db):
    return login("ADMIN")


@pytest.fixture
def seg(db):
    return login("SEGRETERIA")


def upload(client, file_type, content, name=None, **extra):
    if isinstance(content, str):
        content = content.encode()
    name = name or f"{file_type}.csv"
    return client.post(
        f"/setup/upload/{file_type}/",
        {"file": SimpleUploadedFile(name, content)},
        **extra,
    )


def upload_all(client, **override):
    for file_type, content in {**CSVS, **override}.items():
        assert upload(client, file_type, content).status_code == 200


def correct(client, file_type, row, field, value):
    return client.post(
        "/setup/correction/",
        {"file_type": file_type, "row": row, "field": field, "value": value},
        **HX,
    )


def save(client, **extra):
    data = {"school_name": "ITS Test", "school_year": "2026-2027"}
    data.update(extra)
    return client.post(SAVE, data)


def make_pdf(items):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(600, 800))
    c.setFont("Helvetica", 7)
    for x, y, text in items:
        c.drawString(x, y, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def pdf_row(y, label, hours=6):
    items = [(20, y, label)]
    for lo, _ in CALENDARIO_PDF_GRUPPI_COLONNE.values():
        items.append((lo + 10, y, str(hours)))
    return items


def db_counts():
    return {
        "school": School.objects.count(),
        "classes": Class.objects.count(),
        "teachers": Teacher.objects.count(),
        "subjects": Subject.objects.count(),
        "calendar": CalendarEntry.objects.count(),
        "assignments": Assignment.objects.count(),
        "pairings": ClassPairing.objects.count(),
    }


def html(response):
    return response.content.decode()


# --- access -------------------------------------------------------------------

URLS = [
    ("get", "/setup/"),
    ("post", "/setup/upload/teachers/"),
    ("get", "/setup/validate/"),
    ("post", "/setup/correction/"),
    ("post", "/setup/add-date/"),
    ("post", "/setup/save/"),
]


@pytest.mark.parametrize(("method", "path"), URLS)
def test_segreteria_is_denied_everywhere(seg, method, path):
    assert getattr(seg, method)(path).status_code == 403


@pytest.mark.parametrize(("method", "path"), URLS)
def test_anonymous_is_sent_to_login(client, method, path):
    r = getattr(client, method)(path)
    assert r.status_code == 302
    assert r.url.startswith("/login/?next=")


def test_denied_upload_parks_nothing(seg):
    upload(seg, "teachers", CSVS["teachers"])
    assert ParkedUpload.objects.count() == 0


def test_admin_sees_page_with_seven_upload_controls(admin):
    page = html(admin.get(SETUP))
    assert page.count('type="file"') == 6  # the calendar takes CSV or PDF
    for file_type in CSVS:
        assert f"/setup/upload/{file_type}/" in page
    assert ".csv,.pdf" in page
    assert "<h1>Setup</h1>" in page


def test_unknown_file_type_is_404(admin):
    assert upload(admin, "nonsense", "a,b\n").status_code == 404


# --- upload and preview ---------------------------------------------------------


def test_upload_shows_preview_and_count_via_htmx(admin):
    r = upload(admin, "teachers", CSVS["teachers"], **HX)
    page = html(r)
    assert r.status_code == 200
    assert "<html" not in page  # a partial, no full page reload
    assert "2 righe" in page
    assert 'value="ROSSI MARIO"' in page
    assert ParkedUpload.objects.get(file_type="teachers").source == "csv"


def test_calendar_pdf_upload(admin):
    pdf = make_pdf(pdf_row(700, "lunedì 14 settembre 2026"))
    r = upload(admin, "calendar", pdf, name="cal.pdf", **HX)
    assert r.status_code == 200
    assert "5 righe" in html(r)
    assert ParkedUpload.objects.get(file_type="calendar").source == "pdf"


@pytest.mark.parametrize(
    ("file_type", "name", "content", "expected"),
    [
        ("teachers", "t.csv", b"", "vuoto"),
        ("teachers", "t.csv", b"  \n \n", "vuoto"),
        ("teachers", "t.txt", b"nome,tipo\nA,ASSUNTO\n", "Estensione non valida"),
        ("teachers", "t.pdf", b"%PDF-1.4", "Estensione non valida"),
        ("teachers", "t.csv", b"nome,altro\nA,B\n", "missing required column"),
        (
            "calendar",
            "c.pdf",
            make_pdf([(20, 700, "nessuna data qui")]),
            "Nessuna riga",
        ),
        ("calendar", "c.pdf", b"not a pdf at all", "PDF"),
    ],
)
def test_upload_edge_cases_park_nothing(admin, file_type, name, content, expected):
    r = upload(admin, file_type, content, name=name, **HX)
    assert r.status_code == 422
    assert expected in html(r)
    assert ParkedUpload.objects.count() == 0


def test_file_over_size_limit_is_refused(admin):
    with override_settings(SETUP_MAX_UPLOAD_BYTES=40):
        r = upload(admin, "teachers", CSVS["teachers"], **HX)
    assert r.status_code == 422
    assert "dimensione massima" in html(r)
    assert ParkedUpload.objects.count() == 0


def test_size_limit_setting_reads_env_with_default():
    from config.env import DEFAULT_SETUP_MAX_UPLOAD_BYTES, get_positive_int

    assert (
        get_positive_int({}, "SETUP_MAX_UPLOAD_BYTES", DEFAULT_SETUP_MAX_UPLOAD_BYTES)
        == 5 * 1024 * 1024
    )
    assert "SETUP_MAX_UPLOAD_BYTES" in open(".env.example").read()


def test_missing_file_is_a_message(admin):
    r = admin.post("/setup/upload/teachers/", {}, **HX)
    assert r.status_code == 422
    assert "Seleziona un file" in html(r)


def test_reupload_replaces_previous(admin):
    upload(admin, "teachers", CSVS["teachers"])
    upload(admin, "teachers", "nome,tipo\nVERDI ANNA,ASSUNTO\n")
    parked = ParkedUpload.objects.filter(file_type="teachers")
    assert parked.count() == 1
    assert [r["nome"] for r in parked.get().rows] == ["VERDI ANNA"]


def test_rejected_reupload_keeps_the_previous_one(admin):
    upload(admin, "teachers", CSVS["teachers"])
    upload(admin, "teachers", b"")
    assert len(ParkedUpload.objects.get(file_type="teachers").rows) == 2


# --- persistence ----------------------------------------------------------------


def test_uploads_survive_a_restart(admin):
    upload_all(admin)
    fresh = login_again()
    page = html(fresh.get(SETUP))
    assert "ROSSI MARIO" in page
    assert "Matematica" in page
    assert save(fresh).status_code == 302
    assert db_counts()["school"] == 1


def login_again():
    c = Client()
    c.force_login(User.objects.get(email="admin@example.com"))
    return c


# --- issues -------------------------------------------------------------------


def test_row_errors_are_listed_and_block_save(admin):
    upload_all(admin, teachers="nome,tipo\nROSSI MARIO,ASSUNTO\nX,WRONG\n")
    page = html(admin.get(SETUP))
    assert "Errori (" in page
    assert "Riga 3" in page
    assert re.search(r"<button type=\"submit\" disabled>Salva", page)
    r = save(admin)
    assert r.status_code == 422
    assert db_counts()["school"] == 0


def test_cross_file_errors_and_warnings(admin):
    upload(admin, "teachers", CSVS["teachers"])
    assert "Errori" not in html(admin.get(SETUP))  # one file: no cross-file checks
    upload(admin, "classes", CSVS["classes"])
    upload(admin, "subjects", CSVS["subjects"])
    r = upload(
        admin,
        "assignments",
        "docente_nome,classe_nome,materia_nome,ore_anno\nGHOST,1A,Matematica,10\n",
    )
    page = html(r)
    assert "teacher &#x27;GHOST&#x27; does not exist" in page
    assert "Avvisi (" in page  # unused teachers, classes, subjects
    assert "has no assignments" in page


def test_warnings_do_not_block_save(admin):
    upload_all(admin, teachers=CSVS["teachers"] + "NESSUNO ANNA,,ASSUNTO\n")
    page = html(admin.get(SETUP))
    assert "Avvisi (1)" in page
    assert "disabled" not in page
    assert save(admin).status_code == 302


def test_save_is_refused_until_every_file_is_present(admin):
    upload(admin, "teachers", CSVS["teachers"])
    r = save(admin)
    assert r.status_code == 422
    assert "Mancano ancora i file" in html(r)
    assert db_counts()["school"] == 0


# --- live correction ------------------------------------------------------------


def test_correction_revalidates_immediately(admin):
    upload_all(admin, assignments=CSVS["assignments"].replace("BIANCHI", "BIANCO"))
    page = html(admin.get(SETUP))
    assert "teacher &#x27;BIANCO LUCA&#x27; does not exist" in page
    r = correct(admin, "assignments", 4, "docente_nome", "BIANCHI LUCA")
    assert r.status_code == 200
    assert "BIANCO" in html(r)  # row 5 still has it
    r = correct(admin, "assignments", 5, "docente_nome", "BIANCHI LUCA")
    assert "does not exist" not in html(r)
    assert "disabled" not in html(r)


def test_invalid_correction_is_rejected_and_not_stored(admin):
    upload_all(admin)
    before = ParkedUpload.objects.get(file_type="assignments").rows
    r = correct(admin, "assignments", 2, "ore_anno", "abc")
    assert r.status_code == 422
    assert "Valore non valido" in html(r)
    assert ParkedUpload.objects.get(file_type="assignments").rows == before
    r = correct(admin, "teachers", 3, "nome", "rossi   mario")  # duplicate
    assert r.status_code == 422
    assert "duplicate" in html(r)
    r = correct(admin, "teachers", 2, "tipo", "NOPE")
    assert r.status_code == 422
    assert ParkedUpload.objects.get(file_type="teachers").rows[0]["tipo"] == "ASSUNTO"


def test_valid_correction_is_stored_and_saved(admin):
    upload_all(admin)
    assert correct(admin, "classes", 2, "n_studenti", "25").status_code == 200
    assert correct(admin, "calendar", 2, "ore_max_giornata", "4").status_code == 200
    assert correct(admin, "calendar", 2, "ore_max_giornata", "7").status_code == 422
    save(admin)
    assert Class.objects.get(name="1A").student_count == 25
    assert CalendarEntry.objects.get(date=datetime.date(2026, 9, 14)).max_hours == 4


def test_correction_of_unknown_row_or_field(admin):
    upload_all(admin)
    assert correct(admin, "classes", 99, "nome", "X").status_code == 422
    assert correct(admin, "classes", 2, "bogus", "X").status_code == 422
    assert correct(admin, "classes", "x", "nome", "X").status_code == 422
    assert correct(admin, "bogus", 2, "nome", "X").status_code == 422


# --- manual date ------------------------------------------------------------------


def add_date(client, **data):
    data = {"date": "2026-09-16", "max_hours": "5", **data}
    return client.post("/setup/add-date/", data, **HX)


def test_add_manual_date(admin):
    upload_all(admin)
    r = add_date(admin, is_closure="on", stage_class="1a")
    assert r.status_code == 200
    rows = ParkedUpload.objects.get(file_type="calendar").rows
    new = [r for r in rows if r["data"] == "2026-09-16"][0]
    assert new["ore_max_giornata"] == 5
    assert new["flag_chiusura"] is True
    assert new["stage_classe_id"] == "1A"
    assert new["gruppo"] is None
    save(admin)
    assert CalendarEntry.objects.filter(
        date=datetime.date(2026, 9, 16), stage_class__name="1A"
    ).exists()


@pytest.mark.parametrize(
    "data",
    [
        {"date": "2026-02-30"},
        {"date": "oggi"},
        {"date": ""},
        {"max_hours": "7"},
        {"max_hours": "3"},
        {"max_hours": "x"},
        {"stage_class": "9Z"},
        {"date": "2026-09-14"},  # already in the calendar
    ],
)
def test_invalid_manual_date_is_rejected(admin, data):
    upload_all(admin)
    before = ParkedUpload.objects.get(file_type="calendar").rows
    assert add_date(admin, **data).status_code == 422
    assert ParkedUpload.objects.get(file_type="calendar").rows == before


def test_manual_date_needs_calendar_and_classes(admin):
    assert add_date(admin).status_code == 422
    upload(admin, "calendar", CSVS["calendar"])
    assert add_date(admin, stage_class="1A").status_code == 422
    assert add_date(admin).status_code == 200


# --- save -------------------------------------------------------------------------


def test_save_creates_everything(admin):
    upload_all(admin)
    r = save(admin)
    assert r.status_code == 302
    assert r.url == "/setup/completato/"
    assert db_counts() == {
        "school": 1,
        "classes": COUNTS["classes"],
        "teachers": COUNTS["teachers"],
        "subjects": COUNTS["subjects"],
        "calendar": COUNTS["calendar"],
        "assignments": COUNTS["assignments"],
        "pairings": COUNTS["pairings"],
    }
    school = School.objects.get()
    assert (school.name, school.formative_year) == ("ITS Test", "2026-2027")
    assert school.year_start == datetime.date(2026, 9, 14)  # calendar min
    assert school.year_end == datetime.date(2026, 12, 25)  # calendar max
    a = Assignment.objects.get(teacher__name="ROSSI MARIO", school_class__name="1A")
    assert a.ore_totali == 60
    assert Teacher.objects.get(name="BIANCHI LUCA").email == ""
    assert ClassPairing.objects.get().note == "insieme"
    assert CalendarEntry.objects.get(date=datetime.date(2026, 12, 25)).is_closure
    assert ParkedUpload.objects.count() == 0
    done = admin.get("/setup/completato/")
    assert done.status_code == 200
    assert "Setup completato" in html(done)


def test_save_via_htmx_redirects(admin):
    upload_all(admin)
    r = admin.post(SAVE, {"school_name": "S", "school_year": "2026"}, **HX)
    assert r["HX-Redirect"] == "/setup/completato/"


def test_save_with_explicit_dates(admin):
    upload_all(admin)
    save(admin, year_start="2026-09-01", year_end="2027-06-30")
    school = School.objects.get()
    assert school.year_start == datetime.date(2026, 9, 1)
    assert school.year_end == datetime.date(2027, 6, 30)


def test_save_defaults_only_the_missing_date(admin):
    upload_all(admin)
    save(admin, year_start="2026-09-01")
    school = School.objects.get()
    assert school.year_start == datetime.date(2026, 9, 1)
    assert school.year_end == datetime.date(2026, 12, 25)


@pytest.mark.parametrize(
    "data",
    [
        {"school_name": ""},
        {"school_year": ""},
        {"year_start": "2027-01-01", "year_end": "2026-01-01"},
        {"year_start": "2027-01-01", "year_end": "2027-01-01"},
        {"year_start": "2027-01-01"},  # after the last calendar date
        {"year_start": "not a date"},
    ],
)
def test_invalid_save_form(admin, data):
    upload_all(admin)
    assert save(admin, **data).status_code == 422
    assert db_counts()["school"] == 0
    assert ParkedUpload.objects.count() == 6


def test_rollback_on_failure_partway(admin):
    upload_all(admin)
    with mock.patch(
        "school.setup_service._create_pairings", side_effect=RuntimeError("boom")
    ):
        r = save(admin)
    assert r.status_code == 500
    assert "nessun dato è stato scritto" in html(r)
    assert all(n == 0 for n in db_counts().values())
    assert ParkedUpload.objects.count() == 6
    assert save(admin).status_code == 302  # and a retry works


def test_rollback_on_database_rejection(admin):
    upload_all(admin, teachers="nome,email,tipo\n" + "A" * 300 + ",,ASSUNTO\n")
    assert save(admin).status_code == 422  # surfaced as an error, nothing written
    assert all(n == 0 for n in db_counts().values())
    assert ParkedUpload.objects.count() == 6


def test_save_over_existing_school_needs_the_confirmation(admin):
    School.objects.create(
        name="Old",
        formative_year="2025",
        year_start=datetime.date(2025, 9, 1),
        year_end=datetime.date(2026, 6, 30),
    )
    upload_all(admin)
    assert "esiste già una scuola" in html(admin.get(SETUP))
    r = save(admin)
    assert r.status_code == 422
    assert "Scrivi il nome attuale" in html(r)
    assert db_counts()["classes"] == 0
    assert School.objects.get().name == "Old"
    assert ParkedUpload.objects.count() == 6


def test_double_submit_creates_no_duplicates(admin):
    upload_all(admin)
    first = save(admin)
    second = save(admin)
    assert first.status_code == 302
    assert second.status_code == 422
    assert db_counts()["school"] == 1
    assert db_counts()["classes"] == COUNTS["classes"]
    assert db_counts()["assignments"] == COUNTS["assignments"]


def test_save_from_pdf_calendar(admin):
    pdf = make_pdf(
        pdf_row(700, "lunedì 14 settembre 2026")
        + pdf_row(680, "mercoledì 16 settembre 2026", 4)
    )
    upload_all(admin)
    upload(admin, "calendar", pdf, name="c.pdf")
    assert save(admin).status_code == 302
    # 2 dates x 5 groups + the closure row for the missing Tuesday
    assert CalendarEntry.objects.count() == 11
    assert CalendarEntry.objects.filter(is_closure=True).count() == 1

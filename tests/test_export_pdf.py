import datetime
import io
import subprocess
import sys

import pdfplumber
import pytest
from django.test import Client

from accounts.models import User
from export import layout as lay
from export.layout import build_layout, class_sort_key, parse_class_name, week_title
from export.pdf import render_pdf
from scheduling import views
from scheduling.models import Slot, Timetable, TimetableState
from school.models import CalendarEntry, Class, School, Subject, Teacher

D = datetime.date
MON = D(2026, 10, 12)
URL = f"/orario/{MON:%Y-%m-%d}/export-pdf/"


def login(role):
    user = User.objects.create_user(
        f"{role.lower()}@example.com", "pw-long-enough", role
    )
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(views, "today", lambda: D(2026, 10, 14))


@pytest.fixture
def school(db):
    return School.objects.create(
        name="Agenzia Formativa don Angelo Tedoldi",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )


@pytest.fixture
def world(school):
    class W:
        pass

    w = W()
    w.school = school
    # created out of order on purpose, plus a name nobody recognises
    w.itc = Class.objects.create(school=school, name="I I.T.C.", group="PRIME")
    w.zeta = Class.objects.create(school=school, name="Zeta", group="PRIME")
    w.el2 = Class.objects.create(school=school, name="II  elettricisti")
    w.op1 = Class.objects.create(school=school, name="I OP. INFORM.")
    w.el4 = Class.objects.create(school=school, name="IV ELETTRICISTI")
    w.rossi = Teacher.objects.create(
        school=school, name="ROSSI MARIO", type="ASSUNTO", first_time=False
    )
    w.single = Teacher.objects.create(
        school=school, name="VERDI", type="ASSUNTO", first_time=False
    )
    w.mat = Subject.objects.create(
        school=school, name="Matematica", kind="TEORIA", cognitive_weight="MEDIO"
    )
    w.tt = Timetable.objects.create(school=school, week_start=MON, version=5)
    return w


def add(w, klass, teacher, day="LUNEDI", hour=8, **kw):
    return Slot.objects.create(
        timetable=w.tt,
        school_class=klass,
        teacher=teacher,
        subject=w.mat,
        day=day,
        start_hour=hour,
        end_hour=hour + 1,
        **kw,
    )


# ---- layout model (no PDF) ---------------------------------------------------


def test_layout_module_does_not_import_the_renderer():
    code = (
        "import django, os, sys;"
        "os.environ['DJANGO_SETTINGS_MODULE']='config.settings_test';django.setup();"
        "import export.layout;"
        "bad = [m for m in sys.modules if m.startswith('weasyprint')];"
        "sys.exit(bool(bad) or 'export.pdf' in sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


@pytest.mark.parametrize(
    "name,expected",
    [
        ("II ELETTRICISTI", ("ELETTRICISTI", 2)),
        ("  iii   pan. e  past ", ("PAN. E PAST", 3)),
        ("I I.T.C.", ("I.T.C.", 1)),
        ("IV ESTETISTE", ("ESTETISTE", 4)),
        ("V ELETTRICISTI", (None, None)),
        ("1A", (None, None)),
        ("II SARTORIA", (None, None)),
    ],
)
def test_parse_class_name(name, expected):
    assert parse_class_name(name) == expected


def test_class_order_is_course_then_year_unknown_last():
    names = ["Zeta", "I I.T.C.", "III ESTETISTE", "II ELETTRICISTI", "Alfa"]
    names += ["I ELETTRICISTI", "IV OP. INFORM.", "I OP. INFORM.", "II PAN. E PAST"]
    assert sorted(names, key=class_sort_key) == [
        "I OP. INFORM.",
        "IV OP. INFORM.",
        "I ELETTRICISTI",
        "II ELETTRICISTI",
        "III ESTETISTE",
        "II PAN. E PAST",
        "I I.T.C.",
        "Alfa",
        "Zeta",
    ]


def test_week_title_one_and_two_months():
    assert week_title(D(2026, 10, 5)) == "Orario scolastico dal 5 al 9 Ottobre"
    assert week_title(D(2026, 9, 28)) == (
        "Orario scolastico dal 28 Settembre al 2 Ottobre"
    )


def test_layout_model(world):
    w = world
    add(w, w.op1, w.rossi, hour=8)
    add(w, w.el4, w.single, hour=9)
    add(w, w.itc, w.rossi, day="VENERDI", hour=8)  # I.T.C. I
    add(w, w.el2, w.rossi, day="MARTEDI", hour=10)
    CalendarEntry.objects.create(
        school=w.school, date=MON, stage_class=w.zeta, group=None
    )
    layout = build_layout(w.school, MON, w.tt)
    assert layout.title_lines == (
        w.school.name,
        "Orario scolastico dal 12 al 16 Ottobre",
    )
    assert [c.name for c in layout.classes] == [
        "I OP. INFORM.",
        "II  elettricisti",
        "IV ELETTRICISTI",
        "I I.T.C.",
        "Zeta",
    ]
    assert layout.header == ("DATA", "Orario", *(c.name for c in layout.classes))
    assert [d.label for d in layout.days] == [
        "lunedì",
        "martedì",
        "mercoledì",
        "giovedì",
        "venerdì",
    ]
    assert [d.background for d in layout.days] == [
        "#e46c0a",
        "#0070c0",
        "#e46c0a",
        "#0070c0",
        "#e46c0a",
    ]
    mon = layout.days[0]
    assert [r.label for r in mon.rows] == ["8-9", "9-10"]  # trailing rows omitted
    first, second = mon.rows
    assert (first.cells[0].text, first.cells[0].background) == ("ROSSI", "#ddd9c4")
    assert first.cells[0].bold
    assert second.cells[2].text == "VERDI"  # single word kept
    assert second.cells[2].background == "#ffffff"  # IV year
    assert first.cells[1].kind == "empty" and first.cells[1].background == "#d9d9d9"
    assert first.cells[1].text == ""
    assert first.cells[3].background == "#d9d9d9"  # I.T.C. empty
    stage = first.cells[4]
    assert (stage.kind, stage.text, stage.background) == ("stage", "STAGE", "#ffff00")
    assert layout.days[1].rows[-1].label == "10-11"
    assert layout.days[4].rows[0].cells[3].background == "#ffff99"  # I.T.C. I
    assert layout.days[2].rows[0].label == "8-9"  # no lesson: one row kept


def test_colour_fallbacks():
    assert lay.lesson_background("I.T.C.", 2) == "#ffffff"
    assert lay.lesson_background("ESTETISTE", 4) == "#ffffff"
    assert lay.lesson_background(None, None) == "#ffffff"
    assert lay.lesson_background("ESTETISTE", 3) == "#ff99cc"


def test_closed_day_is_grey_not_stage(world):
    w = world
    add(w, w.op1, w.rossi, day="MARTEDI", hour=8)
    CalendarEntry.objects.create(
        school=w.school, date=MON, is_closure=True, max_hours=0, group=None
    )
    day = build_layout(w.school, MON, w.tt).days[0]
    assert {c.kind for r in day.rows for c in r.cells} == {"empty"}


def test_paired_lesson_in_both_classes(world):
    w = world
    add(w, w.op1, w.rossi, paired=True, paired_class=w.el2)
    row = build_layout(w.school, MON, w.tt).days[0].rows[0]
    assert row.cells[0].text == row.cells[1].text == "ROSSI"


# ---- PDF --------------------------------------------------------------------


def pages(data):
    return pdfplumber.open(io.BytesIO(data)).pages


def test_pdf_content(world):
    w = world
    add(w, w.op1, w.rossi)
    add(w, w.el4, w.single, hour=9)
    CalendarEntry.objects.create(
        school=w.school, date=MON, stage_class=w.zeta, group=None
    )
    data = render_pdf(build_layout(w.school, MON, w.tt))
    (page,) = pages(data)
    assert round(page.width) == 842 and round(page.height) == 595
    text = page.extract_text()
    assert "Agenzia Formativa don Angelo Tedoldi" in text
    assert "Orario scolastico dal 12 al 16 Ottobre" in text
    assert "ROSSI" in text and "VERDI" in text and "STAGE" in text
    header = page.extract_words()
    names = [x["text"] for x in header if x["text"] in ("OP.", "ELETTRICISTI", "Zeta")]
    assert names.index("OP.") < names.index("ELETTRICISTI") < names.index("Zeta")
    margin = 8 / 25.4 * 72
    assert min(x["x0"] for x in header) >= margin - 1
    assert max(x["x1"] for x in header) <= page.width - margin + 1


def test_largest_week_stays_on_one_page(school):
    courses = ["OP. INFORM.", "ELETTRICISTI", "ESTETISTE", "PAN. E PAST", "I.T.C."]
    classes = [
        Class.objects.create(school=school, name=f"{y} {c}")
        for c in courses
        for y in ("I", "II", "III", "IV")
    ]
    teacher = Teacher.objects.create(
        school=school, name="BIANCHETTI ANNA", type="ASSUNTO", first_time=False
    )
    subject = Subject.objects.create(
        school=school, name="M", kind="TEORIA", cognitive_weight="MEDIO"
    )
    tt = Timetable.objects.create(school=school, week_start=MON)
    for klass in classes:
        for day in ("LUNEDI", "MARTEDI", "MERCOLEDI", "GIOVEDI", "VENERDI"):
            for hour in range(8, 14):
                Slot.objects.create(
                    timetable=tt,
                    school_class=klass,
                    teacher=teacher,
                    subject=subject,
                    day=day,
                    start_hour=hour,
                    end_hour=hour + 1,
                )
    assert len(pages(render_pdf(build_layout(school, MON, tt)))) == 1


def test_empty_timetable_renders(world):
    data = render_pdf(build_layout(world.school, MON, world.tt))
    assert len(pages(data)) == 1


# ---- view -------------------------------------------------------------------


@pytest.mark.parametrize("role", ["ADMIN", "SEGRETERIA"])
@pytest.mark.parametrize("state", [TimetableState.BOZZA, TimetableState.APPROVATO])
def test_download(world, role, state, django_assert_num_queries):
    Timetable.objects.filter(pk=world.tt.pk).update(state=state)
    add(world, world.op1, world.rossi)
    r = login(role).get(URL)
    assert r.status_code == 200
    assert r["Content-Type"] == "application/pdf"
    assert 'filename="orario_2026-10-12.pdf"' in r["Content-Disposition"]
    assert len(pages(r.content)) == 1


def test_download_changes_nothing(world, caplog):
    before = Timetable.objects.get(pk=world.tt.pk).version
    with caplog.at_level("INFO"):
        assert login("ADMIN").get(URL).status_code == 200
    assert Timetable.objects.get(pk=world.tt.pk).version == before
    assert "audit" not in caplog.text


def test_anonymous_goes_to_login(world, client):
    r = client.get(URL)
    assert r.status_code == 302 and r.url.startswith("/login/")


def test_other_methods_are_405(world):
    admin = login("ADMIN")
    assert admin.post(URL).status_code == 405
    assert admin.put(URL).status_code == 405


def test_week_without_timetable_is_404_with_message(school):
    r = login("ADMIN").get(URL)
    assert r.status_code == 404
    assert "Nessun orario per questa settimana" in r.content.decode()


def test_week_outside_year_is_404(world):
    admin = login("ADMIN")
    assert admin.get("/orario/2020-01-06/export-pdf/").status_code == 404
    assert admin.get("/orario/boh/export-pdf/").status_code == 404


def test_page_shows_download_button(world):
    r = login("SEGRETERIA").get(f"/orario/?settimana={MON:%Y-%m-%d}")
    assert URL in r.content.decode()

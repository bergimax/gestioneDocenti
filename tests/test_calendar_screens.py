import datetime
import html as htmllib
import pathlib
import re

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from school import calendar as calendar_rules
from school import forms, setup_service
from school.importers.parsers import parse_calendar
from school.models import CalendarEntry, Class, School

PW = "correct-horse"
MONTH = "/dati-scuola/calendario/"
CREATE = "/dati-scuola/calendario/nuova/"
HX = {"HTTP_HX_REQUEST": "true"}
TODAY = datetime.date(2026, 10, 9)
GROUPS = ["PRIME", "SECONDE", "TERZE", "QUARTE", "PRIMA4+2"]


def make_school(name="Scuola"):
    return School.objects.create(
        name=name,
        formative_year="2026-2027",
        year_start=datetime.date(2026, 9, 1),
        year_end=datetime.date(2027, 6, 30),
    )


def login(role, **client_kw):
    User.objects.create_user(f"{role.lower()}@example.com", PW, role)
    c = Client(**client_kw)
    c.force_login(User.objects.get(email=f"{role.lower()}@example.com"))
    return c


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr("school.selectors.today", lambda: TODAY)


@pytest.fixture
def school(db):
    return make_school()


@pytest.fixture
def classes(school):
    """One class per group: 1A PRIME, 2A SECONDE, ..."""
    return {
        g: Class.objects.create(school=school, name=f"{i + 1}A", group=g)
        for i, g in enumerate(GROUPS)
    }


@pytest.fixture
def admin(db):
    return login("ADMIN")


@pytest.fixture
def seg(db):
    return login("SEGRETERIA")


def d(day, month=10, year=2026):
    return datetime.date(year, month, day)


def entry(school, day, **kw):
    kw.setdefault("date", d(day) if isinstance(day, int) else day)
    return CalendarEntry.objects.create(school=school, **kw)


def edit(e):
    return f"/dati-scuola/calendario/{e.pk}/modifica/"


def delete(e):
    return f"/dati-scuola/calendario/{e.pk}/elimina/"


def page(client, query="", **kw):
    r = client.get(MONTH + query, **kw)
    assert r.status_code == 200
    return htmllib.unescape(r.content.decode())


def cells(html):
    """{aria-label: (css class, inner html)} for every weekday cell."""
    found = re.findall(
        r'<td class="cal-cell ([^"]*)" aria-label="([^"]*)">(.*?)</td>', html, re.S
    )
    return {label: (css, inner) for css, label, inner in found}


def cell(html, prefix):
    for label, value in cells(html).items():
        if label.startswith(prefix):
            return value
    raise AssertionError(f"no cell {prefix}")


def text(inner):
    return " ".join(re.sub(r"<[^>]+>", " ", inner).split())


def valid(**kw):
    data = {"date": "2026-10-12", "group": "", "max_hours": "6"}
    data.update(kw)
    return data


# --- month selection ----------------------------------------------------


def test_default_month_is_today_and_grid_is_monday_friday(school, admin):
    html = page(admin)
    assert ">Ottobre 2026<" in html
    assert re.findall(r"<th scope=\"col\">(\w+)</th>", html) == [
        "Lun",
        "Mar",
        "Mer",
        "Gio",
        "Ven",
    ]
    assert 'aria-labelledby="calendar-month-title"' in html
    assert 'id="calendar-month-title"' in html
    labels = list(cells(html))
    assert len(labels) == 22  # weekdays of October 2026
    assert labels[0].startswith("Giovedì 1 ottobre")
    assert labels[-1].startswith("Sabato") is False


def test_default_month_when_today_outside_year(school, admin, monkeypatch):
    monkeypatch.setattr("school.selectors.today", lambda: d(1, 7, 2026))
    assert ">Settembre 2026<" in page(admin)


def test_explicit_and_clamped_months(school, admin):
    assert ">Novembre 2026<" in page(admin, "?mese=2026-11")
    for raw in ("abc", "2026-13", "2026-1", "", "2026-00", "20-10-01"):
        assert ">Ottobre 2026<" in page(admin, f"?mese={raw}")
    assert ">Giugno 2027<" in page(admin, "?mese=2030-01")
    assert ">Settembre 2026<" in page(admin, "?mese=2020-05")


def test_prev_next_links(school, admin):
    html = page(admin)
    assert 'href="/dati-scuola/calendario/?mese=2026-09"' in html
    assert 'href="/dati-scuola/calendario/?mese=2026-11"' in html
    assert 'hx-get="/dati-scuola/calendario/?mese=2026-11"' in html
    assert 'hx-push-url="true"' in html
    first = page(admin, "?mese=2026-09")
    assert "Mese precedente" not in first and "Mese successivo" in first
    last = page(admin, "?mese=2027-06")
    assert "Mese successivo" not in last and "Mese precedente" in last


def test_htmx_navigation_returns_partial(school, admin):
    html = page(admin, "?mese=2026-11", **HX)
    assert "<html" not in html and 'id="calendar-section"' in html
    assert ">Novembre 2026<" in html
    restored = page(
        admin, "?mese=2026-11", **HX, HTTP_HX_HISTORY_RESTORE_REQUEST="true"
    )
    assert "<html" in restored


# --- cells: resolution --------------------------------------------------


def test_resolvers_are_called(school, classes, admin, monkeypatch):
    calls = []
    real = calendar_rules.resolve_hours

    def spy(rows, day, class_id, class_group):
        calls.append((day, class_id, class_group))
        return real(rows, day, class_id, class_group)

    monkeypatch.setattr(calendar_rules, "resolve_hours", spy)
    page(admin)
    assert (d(8), None, "TERZE") in calls
    assert len(calls) == 22 * 5


def test_no_precedence_logic_in_screens():
    body = pathlib.Path("school/views/calendar.py").read_text()
    assert "max_hours" not in body and "is_closure" not in body
    for f in pathlib.Path("templates/school/calendar").glob("*.html"):
        assert "max_hours" not in f.read_text(), f
    mine = pathlib.Path("school/selectors.py").read_text().split("# --- annual")[1]
    assert "min(" in mine  # only the month clamp
    assert "resolve_hours" in mine and "resolve_start_hour" in mine
    assert "max_hours" not in mine.replace("{entry.max_hours}", "")


def lines(html, prefix):
    return text(cell(html, prefix)[1])


def test_default_six_and_group_without_class(school, admin):
    Class.objects.create(school=school, name="1A", group="PRIME")
    out = lines(page(admin), "Giovedì 8 ottobre")
    assert "Prime: 6" in out
    assert "Seconde: -" in out and "Prima 4+2: -" in out
    assert cell(page(admin), "Giovedì 8 ottobre")[0] == "cal-normal"


def test_whole_school_row_and_group_row_override(school, classes, admin):
    entry(school, 8, max_hours=5)
    entry(school, 8, group="TERZE", max_hours=4)
    out = lines(page(admin), "Giovedì 8 ottobre")
    assert "Prime: 5" in out and "Terze: 4" in out and "Seconde: 5" in out


def test_cap_at_six_for_stored_eight(school, classes, admin):
    entry(school, 8, max_hours=8)
    assert "Prime: 6" in lines(page(admin), "Giovedì 8 ottobre")


def test_group_stage(school, classes, admin):
    entry(school, 8, group="TERZE", is_group_stage=True)
    css, inner = cell(page(admin), "Giovedì 8 ottobre")
    out = text(inner)
    assert "Terze: 0" in out and "Prime: 6" in out
    assert css == "cal-stage" and "Stage" in out


def test_class_stage_lists_class_group_unchanged(school, classes, admin):
    entry(school, 8, stage_class=classes["TERZE"])
    entry(school, 8, stage_class=classes["PRIME"])
    css, inner = cell(page(admin), "Giovedì 8 ottobre")
    out = text(inner)
    assert "Stage: 1A, 3A" in out
    assert "Terze: 6" in out and "Prime: 6" in out
    assert css == "cal-stage"


def test_start_hour_and_multiple_rows(school, classes, admin):
    entry(school, 8, start_hour_min=10)
    entry(school, 8, group="PRIME", max_hours=4)
    inner = cell(page(admin), "Giovedì 8 ottobre")[1]
    out = text(inner)
    assert "Seconde: 6 dalle 10" in out and "Prime: 4 dalle 10" in out
    assert inner.count("Tutta la scuola:") == 1 and inner.count("Prime:") >= 1
    assert len(re.findall(r"Modifica riga del 8 ottobre", inner)) == 2


# --- cell types ---------------------------------------------------------


def test_closure_cell(school, classes, admin):
    entry(school, 8, is_closure=True)
    css, inner = cell(page(admin), "Giovedì 8 ottobre")
    out = text(inner)
    assert css == "cal-closure" and "Chiusura" in out
    assert all(f"{n}: 0" in out for n in ("Prime", "Seconde", "Terze"))


def test_closure_overridden_by_group_rows_is_short_day(school, classes, admin):
    entry(school, 8, is_closure=True)
    for g in GROUPS:
        entry(school, 8, group=g, max_hours=4)
    css, inner = cell(page(admin), "Giovedì 8 ottobre")
    assert css == "cal-short"
    assert "Giornata corta" in inner and ">Chiusura<" not in inner


def test_short_day_and_normal(school, classes, admin):
    entry(school, 8, group="PRIME", max_hours=4)
    html = page(admin)
    css, inner = cell(html, "Giovedì 8 ottobre")
    assert css == "cal-short" and "Giornata corta" in inner
    css, inner = cell(html, "Venerdì 9 ottobre")
    assert css == "cal-normal" and "cal-badge" not in inner
    assert "Giovedì 8 ottobre: Giornata corta" in html


def test_priority_closure_over_stage_over_short(school, classes, admin):
    entry(school, 8, is_closure=True)
    entry(school, 8, stage_class=classes["PRIME"])
    css, inner = cell(page(admin), "Giovedì 8 ottobre")
    assert css == "cal-closure"
    assert ">Chiusura<" in inner and ">Stage<" in inner
    entry(school, 12, group="PRIME", max_hours=4)
    entry(school, 12, group="TERZE", is_group_stage=True)
    css, inner = cell(page(admin), "Lunedì 12 ottobre")
    assert css == "cal-stage" and "Giornata corta" in inner


def test_closure_for_group_without_classes_is_not_closure(school, admin):
    Class.objects.create(school=school, name="1A", group="PRIME")
    entry(school, 8, group="TERZE", is_closure=True)
    assert cell(page(admin), "Giovedì 8 ottobre")[0] == "cal-normal"


def test_legend(school, admin):
    html = page(admin)
    for label in ("Chiusura", "Stage", "Giornata corta", "Giorno normale"):
        assert f"</span> {label}</li>" in html


def test_weekend_rows_listed(school, classes, admin):
    sat = entry(school, 3, max_hours=4)
    html = page(admin)
    assert "Righe in giorni non lavorativi" in html
    assert "3 ottobre" in html.split("Righe in giorni non lavorativi")[1]
    assert edit(sat) in html and delete(sat) in html
    assert "Righe in giorni non lavorativi" not in page(admin, "?mese=2026-11")


def test_queries_constant(school, classes, admin):
    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert admin.get(MONTH).status_code == 200
        return len(ctx)

    entry(school, 8, max_hours=5)
    count()
    before = count()
    for i in range(5):
        c = Class.objects.create(school=school, name=f"S{i}", group="PRIME")
        entry(school, 12 + i, stage_class=c)
    for day in range(13, 30, 3):
        entry(school, day, group="TERZE", max_hours=4)
    for day in range(1, 10):
        entry(school, day, group="QUARTE", max_hours=5)
    for day in range(1, 12):
        entry(school, d(day, 11), max_hours=5)
        entry(school, d(day, 12), max_hours=5)
    assert CalendarEntry.objects.count() >= 30
    assert count() == before
    with CaptureQueriesContext(connection) as ctx:
        admin.get(MONTH)
    assert sum("school_calendarentry" in q["sql"] for q in ctx) == 1


# --- empty states / authorization ---------------------------------------


def test_no_school(db, admin, seg):
    for client in (admin, seg):
        html = page(client)
        assert "Nessuna scuola configurata: esegui prima il setup." in html
        assert "Aggiungi" not in html
    for method in (admin.get, admin.post):
        r = method(CREATE, valid())
        assert r.status_code == 200
        assert "Nessuna scuola configurata" in r.content.decode()
    assert CalendarEntry.objects.count() == 0


def test_school_without_rows(school, classes, admin, seg):
    html = page(admin)
    assert "Nessuna riga di calendario per questo mese." in html
    assert "Aggiungi riga" in html and len(cells(html)) == 22
    assert "Prime: 6" in lines(html, "Giovedì 8 ottobre")
    assert "Aggiungi" not in page(seg)


def test_secretary_read_only(school, classes, seg):
    e = entry(school, 8, max_hours=4)
    html = page(seg)
    assert "Prime: 4" in lines(html, "Giovedì 8 ottobre")
    for forbidden in ("Aggiungi", "Modifica", "Elimina", edit(e), delete(e)):
        assert forbidden not in html
    assert "Dati scuola" not in html
    for url in (CREATE, edit(e), delete(e)):
        assert seg.get(url).status_code == 403
        assert seg.post(url, valid(max_hours="5")).status_code == 403
    e.refresh_from_db()
    assert e.max_hours == 4 and CalendarEntry.objects.count() == 1


def test_anonymous_redirected(school, client):
    e = entry(school, 8)
    for url in (MONTH, CREATE, edit(e), delete(e)):
        for method in (client.get, client.post):
            r = method(url, valid())
            assert r.status_code == 302 and r.url.startswith("/login/")
    assert CalendarEntry.objects.count() == 1


def test_hub_link(school, admin, seg):
    html = admin.get("/dati-scuola/").content.decode()
    assert f'href="{MONTH}"' in html and ">Calendario<" in html
    assert admin.get(MONTH).status_code == 200
    assert seg.get("/dati-scuola/").status_code == 403


def test_no_role_comparison_in_new_code():
    paths = [
        *pathlib.Path("templates/school/calendar").glob("*.html"),
        pathlib.Path("school/views/calendar.py"),
        pathlib.Path("school/forms.py"),
    ]
    for path in paths:
        body = path.read_text()
        assert "user.role" not in body and "Role." not in body, path


def test_csrf_enforced(school):
    c = login("ADMIN", enforce_csrf_checks=True)
    e = entry(school, 8)
    for url in (CREATE, edit(e), delete(e)):
        assert c.post(url, valid()).status_code == 403
    assert CalendarEntry.objects.count() == 1


# --- create -------------------------------------------------------------


def test_create_htmx(school, classes, admin):
    r = admin.post(CREATE, valid(max_hours="4", group="TERZE"), **HX)
    assert r.status_code == 200
    html = htmllib.unescape(r.content.decode())
    assert "<html" not in html and 'id="calendar-section"' in html
    assert "Riga del 12 ottobre aggiunta." in html
    assert "Terze: 4" in lines(html, "Lunedì 12 ottobre")
    e = CalendarEntry.objects.get()
    assert (e.school, e.group, e.max_hours) == (school, "TERZE", 4)


def test_create_plain_redirects_to_entry_month(school, admin):
    r = admin.post(CREATE, valid(date="2026-11-02"), follow=True)
    assert r.redirect_chain == [(MONTH + "?mese=2026-11", 302)]
    html = r.content.decode()
    assert html.count("Riga del 2 novembre aggiunta.") == 1
    assert "Novembre 2026" in html


def test_create_htmx_shows_saved_month(school, admin):
    r = admin.post(CREATE, valid(date="2026-12-01"), **HX)
    assert "Dicembre 2026" in r.content.decode()


def test_create_get_prefill_and_class_menu(school, classes, admin):
    other = make_school("Altra")
    Class.objects.create(school=other, name="ALTRUI")
    Class.objects.create(school=school, name="<script>alert(1)</script>")
    for kw in ({}, HX):
        r = admin.get(CREATE + "?data=2026-10-12", **kw)
        html = r.content.decode()
        assert 'value="2026-10-12"' in html
        assert "ALTRUI" not in html
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert ("<html" in html) == (not kw)
        assert "csrfmiddlewaretoken" in html
    html = admin.get(CREATE + "?data=zzz").content.decode()
    assert 'value="zzz"' not in html


def test_create_class_stage(school, classes, admin):
    admin.post(CREATE, valid(stage_class=classes["TERZE"].pk))
    e = CalendarEntry.objects.get()
    assert e.stage_class == classes["TERZE"] and e.group is None


def test_closure_without_hours(school, classes, admin):
    admin.post(CREATE, valid(is_closure="on", max_hours=""))
    e = CalendarEntry.objects.get()
    assert e.is_closure and e.max_hours == 0
    assert cell(page(admin), "Lunedì 12 ottobre")[0] == "cal-closure"


@pytest.mark.parametrize("group", ["", "TERZE"])
@pytest.mark.parametrize("hours", ["", "5"])
def test_closure_stores_zero_hours(school, classes, admin, group, hours):
    r = admin.post(CREATE, valid(is_closure="on", group=group, max_hours=hours))
    assert r.status_code == 302
    e = CalendarEntry.objects.get()
    assert e.is_closure and e.max_hours == 0 and (e.group or "") == group
    e.full_clean()
    if not group:
        assert cell(page(admin), "Lunedì 12 ottobre")[0] == "cal-closure"


@pytest.mark.parametrize("hours", ["6", "4", ""])
def test_group_stage_stores_eight_hours(school, classes, admin, hours):
    r = admin.post(CREATE, valid(group="TERZE", is_group_stage="on", max_hours=hours))
    assert r.status_code == 302
    e = CalendarEntry.objects.get()
    assert e.is_group_stage and e.max_hours == 8 and e.group == "TERZE"
    e.full_clean()
    assert "Stage" in cell(page(admin), "Lunedì 12 ottobre")[1]


@pytest.mark.parametrize("posted,stored", [("4", 4), ("5", 5), ("6", 6), ("", 6)])
def test_lesson_row_hours_as_posted(school, classes, admin, posted, stored):
    admin.post(CREATE, valid(max_hours=posted))
    assert CalendarEntry.objects.get().max_hours == stored


@pytest.mark.parametrize("bad", ["0", "8", "7", "abc"])
def test_lesson_row_refuses_other_hours(school, classes, admin, bad):
    r = admin.post(CREATE, valid(max_hours=bad))
    assert r.status_code == 200
    assert "Scegli 4, 5 o 6 ore: scelta non valida." in htmllib.unescape(
        r.content.decode()
    )
    assert CalendarEntry.objects.count() == 0


def test_closure_and_stage_conflict_is_linked_to_the_stage_field(
    school, classes, admin
):
    r = admin.post(CREATE, valid(group="TERZE", is_closure="on", is_group_stage="on"))
    html = r.content.decode()
    msg = "Chiusura e stage di gruppo non possono coesistere."
    assert html.count(msg) == 1
    assert 'id="id_is_group_stage-error"' in html
    tag = re.search(r'<input[^>]*name="is_group_stage"[^>]*>', html).group(0)
    assert 'aria-invalid="true"' in tag
    assert "id_is_group_stage-error" in tag
    assert CalendarEntry.objects.count() == 0


def test_stage_without_group_keeps_its_message(school, classes, admin):
    r = admin.post(CREATE, valid(is_group_stage="on"))
    assert "Lo stage di gruppo richiede un gruppo." in r.content.decode()
    assert "coesistere" not in r.content.decode()


def test_class_stage_keeps_posted_hours(school, classes, admin):
    admin.post(CREATE, valid(stage_class=classes["TERZE"].pk, max_hours="4"))
    assert CalendarEntry.objects.get().max_hours == 4


def test_form_calls_the_shared_rule(school, classes, admin, monkeypatch):
    calls = []
    real = forms.calendar_hours_error

    def spy(hours, **kw):
        calls.append((hours, kw["chiusura"], kw["stage"]))
        return real(hours, **kw)

    monkeypatch.setattr("school.forms.calendar_hours_error", spy)
    admin.post(CREATE, valid(date="2026-10-12", is_closure="on"))
    admin.post(CREATE, valid(date="2026-10-13", group="TERZE", is_group_stage="on"))
    admin.post(CREATE, valid(date="2026-10-14", max_hours="5"))
    assert calls == [(0, True, False), (8, False, True), (5, False, False)]


def test_form_reports_a_rule_error_on_the_flag_or_the_hours(school, monkeypatch):
    monkeypatch.setattr("school.forms.calendar_hours_error", lambda h, **kw: "no")
    f = forms.CalendarEntryForm(valid(), school=school)
    assert not f.is_valid() and f.errors == {"max_hours": ["no"]}
    f = forms.CalendarEntryForm(
        valid(group="TERZE", is_group_stage="on"), school=school
    )
    assert not f.is_valid() and f.errors == {"is_group_stage": ["no"]}


def test_form_has_no_literal_hours_comparison():
    body = pathlib.Path(forms.__file__).read_text()
    start = body.index("class CalendarEntryForm")
    mine = body[start : body.index("class CalendarRangeForm")]
    assert not re.search(r"(==|!=)\s*[08]\b", mine)
    assert "STAGE_ORE_GIORNATA" in mine and "CLOSURE_HOURS" in mine


def test_hours_help_text_on_create_and_edit(school, classes, admin):
    e = entry(school, 12, max_hours=4)
    for url in (CREATE, edit(e)):
        html = htmllib.unescape(admin.get(url).content.decode())
        assert "Per una chiusura la riga salva 0 ore e per uno stage di gruppo" in html
        tag = re.search(r'<select[^>]*name="max_hours"[^>]*>', html).group(0)
        assert 'aria-describedby="id_max_hours-hint"' in tag
        assert 'id="id_max_hours-hint"' in html
    html = admin.get(CREATE).content.decode()
    block = re.search(r'<select[^>]*name="max_hours".*?</select>', html, re.S).group(0)
    assert re.findall(r'<option value="(\d+)"', block) == ["4", "5", "6"]


def selected(html):
    block = re.search(r'<select[^>]*name="max_hours".*?</select>', html, re.S).group(0)
    return re.findall(r'<option value="(\d+)" selected', block)


@pytest.mark.parametrize(
    "kw,flag,saved",
    [
        ({"is_closure": True, "max_hours": 6}, "is_closure", 0),
        ({"is_closure": True, "max_hours": 0}, "is_closure", 0),
        (
            {"is_group_stage": True, "group": "TERZE", "max_hours": 5},
            "is_group_stage",
            8,
        ),
        (
            {"is_group_stage": True, "group": "TERZE", "max_hours": 8},
            "is_group_stage",
            8,
        ),
    ],
)
def test_edit_round_trip_of_closure_and_stage_rows(
    school, classes, admin, kw, flag, saved
):
    e = entry(school, 12, **kw)
    html = admin.get(edit(e)).content.decode()
    assert "importato" not in html
    assert selected(html) == ["6"]
    assert re.search(rf'<input[^>]*name="{flag}"[^>]*checked', html)
    data = valid(group=kw.get("group", ""), **{flag: "on"})
    assert admin.post(edit(e), data).status_code == 302
    e.refresh_from_db()
    assert e.max_hours == saved and getattr(e, flag)


def test_unticking_closure_saves_a_lesson_row(school, classes, admin):
    e = entry(school, 12, is_closure=True, max_hours=0)
    assert admin.post(edit(e), valid()).status_code == 302
    e.refresh_from_db()
    assert not e.is_closure and e.max_hours == 6


def test_legacy_eight_hours_group_row_resaves_unchanged(school, classes, admin):
    e = entry(school, 12, group="TERZE", max_hours=8)
    assert "8 (importato)" in admin.get(edit(e)).content.decode()
    assert admin.post(edit(e), valid(group="TERZE", max_hours="8")).status_code == 302
    e.refresh_from_db()
    assert e.max_hours == 8 and not e.is_group_stage


def test_equivalent_to_the_setup_flow_rows(school, classes, admin):
    admin.post(CREATE, valid(date="2026-10-12", is_closure="on"))
    admin.post(CREATE, valid(date="2026-10-13", group="TERZE", is_group_stage="on"))
    mine = {
        e.date.day: (e.is_closure, e.is_group_stage, e.max_hours)
        for e in CalendarEntry.objects.all()
    }
    CalendarEntry.objects.all().delete()
    rows = parse_calendar(
        "data,gruppo,ore_max_giornata,flag_chiusura,stage_classe_id\n"
        "2026-10-12,,0,true,\n"
        "2026-10-13,TERZE,8,false,\n"
    )
    assert not rows.messages
    made = setup_service._create_calendar(school, rows.rows, {})
    theirs = {e.date.day: (e.is_closure, e.is_group_stage, e.max_hours) for e in made}
    assert mine == theirs == {12: (True, False, 0), 13: (False, True, 8)}
    for e in made:
        assert calendar_rules.resolve_hours([e], e.date, 0, "TERZE") == 0


def test_class_stage_next_to_school_row_and_two_classes(school, classes, admin):
    entry(school, 12, max_hours=5)
    for key in ("PRIME", "TERZE"):
        r = admin.post(CREATE, valid(stage_class=classes[key].pk))
        assert r.status_code == 302
    assert CalendarEntry.objects.count() == 3


def test_forged_school_ignored(school, classes, admin):
    other = make_school("Altra")
    admin.post(CREATE, valid(school=other.pk))
    assert CalendarEntry.objects.get().school == school


ERR = "Esiste già una riga per questa data"


@pytest.mark.parametrize(
    "data,msg",
    [
        ({"date": ""}, "Inserisci la data."),
        ({"date": "2026-13-45"}, "data valida"),
        ({"date": "2026-08-31"}, "compresa tra il 01/09/2026 e il 30/06/2027"),
        ({"date": "2027-07-01"}, "compresa tra"),
        (
            {"date": "2026-10-10"},
            "Il calendario riguarda solo i giorni da lunedì a venerdì.",
        ),
        ({"date": "2026-10-11"}, "solo i giorni da lunedì a venerdì"),
        ({"max_hours": "7"}, "Scegli 4, 5 o 6 ore"),
        ({"max_hours": "abc"}, "Scegli 4, 5 o 6 ore"),
        ({"max_hours": "8"}, "Scegli 4, 5 o 6 ore"),
        ({"max_hours": "0"}, "Scegli 4, 5 o 6 ore"),
        (
            {"group": "TERZE", "is_group_stage": "on", "is_closure": "on"},
            "Chiusura e stage di gruppo non possono coesistere.",
        ),
        ({"start_hour_min": "7"}, "da 8 a 13"),
        ({"start_hour_min": "14"}, "da 8 a 13"),
        ({"group": "XX"}, "Scegli un gruppo valido"),
        ({"is_group_stage": "on"}, "Lo stage di gruppo richiede un gruppo."),
        ({"stage_class": "abc"}, "scelta non valida"),
        ({"stage_class": "OTHER"}, "scelta non valida"),
        (
            {"group": "TERZE", "is_group_stage": "on", "stage_class": "OWN"},
            "Stage di classe e stage di gruppo non possono coesistere.",
        ),
        ({"stage_class": "OWN", "group": "TERZE"}, "Per lo stage di una classe"),
        ({"stage_class": "OWN", "is_closure": "on"}, "Per lo stage di una classe"),
        ({"stage_class": "OWN", "start_hour_min": "10"}, "Per lo stage di una classe"),
    ],
)
def test_create_validation(school, classes, admin, data, msg):
    other = Class.objects.create(school=make_school("Altra"), name="ALTRUI")
    data = {
        k: (classes["TERZE"].pk if v == "OWN" else other.pk if v == "OTHER" else v)
        for k, v in data.items()
    }
    for kwargs in ({}, HX):
        r = admin.post(CREATE, valid(**data), **kwargs)
        assert r.status_code == 200
        html = htmllib.unescape(r.content.decode())
        assert msg in html and "<form" in html
        assert 'aria-invalid="true"' in html
        ids = set(re.findall(r'id="([^"]+)"', html))
        for ref in re.findall(r'aria-describedby="([^"]*)"', html):
            assert all(i in ids for i in ref.split())
        if kwargs:
            assert r["HX-Retarget"] == "#calendar-form-area"
            assert r["HX-Reswap"] == "innerHTML"
    assert CalendarEntry.objects.count() == 0


def test_duplicates_rejected_with_link(school, classes, admin):
    first = entry(school, 12, group="TERZE", max_hours=4)
    r = admin.post(CREATE, valid(group="TERZE"))
    html = htmllib.unescape(r.content.decode())
    assert ERR in html and f'href="{edit(first)}"' in html
    whole = entry(school, 13)
    r = admin.post(CREATE, valid(date="2026-10-13"))
    html = htmllib.unescape(r.content.decode())
    assert ERR in html and f'href="{edit(whole)}"' in html
    stage = entry(school, 14, stage_class=classes["PRIME"])
    r = admin.post(CREATE, valid(date="2026-10-14", stage_class=classes["PRIME"].pk))
    html = htmllib.unescape(r.content.decode())
    assert ERR in html and f'href="{edit(stage)}"' in html
    assert CalendarEntry.objects.count() == 3


def test_duplicate_race_reported_by_integrity_error(
    school, classes, admin, monkeypatch
):
    from school import forms

    entry(school, 12)
    monkeypatch.setattr(forms.CalendarEntryForm, "_check_duplicate", lambda *a: None)
    r = admin.post(CREATE, valid(), **HX)
    assert r.status_code == 200 and ERR in htmllib.unescape(r.content.decode())
    assert r["HX-Retarget"] == "#calendar-form-area"
    assert CalendarEntry.objects.count() == 1


# --- edit ---------------------------------------------------------------


def test_edit_both_modes(school, classes, admin):
    e = entry(school, 12, group="TERZE", max_hours=4)
    for kw in ({}, HX):
        html = htmllib.unescape(admin.get(edit(e), **kw).content.decode())
        assert "Modifica riga del 12/10/2026" in html
        assert 'value="2026-10-12"' in html
    r = admin.post(edit(e), valid(group="TERZE", max_hours="5"), **HX)
    html = htmllib.unescape(r.content.decode())
    assert "Riga del 12 ottobre aggiornata." in html and "Terze: 5" in html
    r = admin.post(edit(e), valid(date="2026-11-02", max_hours="4"), follow=True)
    assert r.redirect_chain == [(MONTH + "?mese=2026-11", 302)]
    e.refresh_from_db()
    assert (e.date, e.max_hours, e.group) == (d(2, 11), 4, None)


def test_edit_imported_eight_hours(school, classes, admin):
    e = entry(school, 12, max_hours=8)
    html = admin.get(edit(e)).content.decode()
    assert "8 (importato)" in html
    assert "importato" not in admin.get(CREATE).content.decode()
    r = admin.post(edit(e), valid(max_hours="8"))
    assert r.status_code == 302
    e.refresh_from_db()
    assert e.max_hours == 8
    other = entry(school, 13, max_hours=4)
    r = admin.post(edit(other), valid(date="2026-10-13", max_hours="8"))
    assert r.status_code == 200


def test_unchanged_row_is_not_its_own_duplicate(school, classes, admin):
    e = entry(school, 12, group="TERZE")
    s = entry(school, 13, stage_class=classes["PRIME"])
    assert admin.post(edit(e), valid(group="TERZE")).status_code == 302
    r = admin.post(edit(s), valid(date="2026-10-13", stage_class=classes["PRIME"].pk))
    assert r.status_code == 302


def test_edit_cannot_collide_or_change_school(school, classes, admin):
    other = make_school("Altra")
    e = entry(school, 12)
    entry(school, 13)
    r = admin.post(edit(e), valid(date="2026-10-13", school=other.pk))
    assert r.status_code == 200 and ERR in htmllib.unescape(r.content.decode())
    admin.post(edit(e), valid(max_hours="4", school=other.pk))
    e.refresh_from_db()
    assert e.school == school and e.max_hours == 4


def test_edit_delete_other_school_or_unknown_404(school, admin):
    foreign = entry(make_school("Altra"), 12)
    for url in (
        edit(foreign),
        delete(foreign),
        "/dati-scuola/calendario/9999/modifica/",
    ):
        assert admin.get(url).status_code == 404
        assert admin.post(url, valid()).status_code == 404
    assert CalendarEntry.objects.count() == 1


# --- delete -------------------------------------------------------------


def test_delete_confirm_and_post(school, classes, admin):
    e = entry(school, 12, group="TERZE")
    for kw in ({}, HX):
        r = admin.get(delete(e), **kw)
        html = r.content.decode()
        assert "Conferma" in html and "csrfmiddlewaretoken" in html
        assert ("<html" in html) == (not kw)
    assert CalendarEntry.objects.count() == 1
    r = admin.post(delete(e), **HX)
    html = htmllib.unescape(r.content.decode())
    assert "Riga del 12 ottobre eliminata." in html and "<html" not in html
    assert not CalendarEntry.objects.exists()
    e2 = entry(school, 13)
    r = admin.post(delete(e2), follow=True)
    assert r.redirect_chain == [(MONTH + "?mese=2026-10", 302)]
    assert r.content.decode().count("Riga del 13 ottobre eliminata.") == 1


def test_delete_get_with_stage_class_shows_name(school, classes, admin):
    e = entry(school, 12, stage_class=classes["TERZE"])
    assert "stage della classe 3A" in admin.get(delete(e)).content.decode()


# --- accessibility and escaping -----------------------------------------


def test_escaping_and_labels(school, admin):
    evil = "<script>alert(1)</script>"
    c = Class.objects.create(school=school, name=evil, group="TERZE")
    e = entry(school, 8, stage_class=c)
    raw = admin.get(MONTH).content.decode()
    assert evil not in raw and "&lt;script&gt;" in raw
    assert 'aria-label="Modifica riga del 8 ottobre - Singola classe"' in raw
    assert 'aria-label="Elimina riga del 8 ottobre - Singola classe"' in raw
    assert 'aria-live="polite"' in raw
    assert evil not in admin.get(delete(e)).content.decode()
    flash = admin.post(CREATE, valid(stage_class=c.pk), **HX).content.decode()
    assert evil not in flash
    r = admin.post(delete(e), follow=True)
    assert evil not in r.content.decode()

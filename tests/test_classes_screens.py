import datetime
import html as htmllib
import re

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from scheduling.models import Slot, Timetable
from school.models import (
    Assignment,
    CalendarEntry,
    Class,
    ClassPairing,
    School,
    Subject,
    Teacher,
)

PW = "correct-horse"
LIST = "/dati-scuola/classi/"
CREATE = "/dati-scuola/classi/nuova/"
HX = {"HTTP_HX_REQUEST": "true"}
MON = datetime.date(2026, 10, 5)


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


@pytest.fixture
def school(db):
    return make_school()


@pytest.fixture
def admin(db):
    return login("ADMIN")


@pytest.fixture
def seg(db):
    return login("SEGRETERIA")


def klass(school, name="II ELETTRICISTI", **kw):
    return Class.objects.create(school=school, name=name, **kw)


def edit(c):
    return f"/dati-scuola/classi/{c.pk}/modifica/"


def delete(c):
    return f"/dati-scuola/classi/{c.pk}/elimina/"


def valid(**kw):
    data = {"name": "III MECCANICI", "student_count": "20", "group": "TERZE"}
    data.update(kw)
    return data


def subj(school, name="Italiano"):
    return Subject.objects.create(
        school=school, name=name, kind="TEORIA", cognitive_weight="ALTO"
    )


def teach(school):
    return Teacher.objects.create(school=school, name="ROSSI MARIO", type="ASSUNTO")


def slot(school, **kw):
    tt = Timetable.objects.create(school=school, week_start=MON)
    base = dict(
        timetable=tt,
        teacher=teach(school),
        subject=subj(school, "Slotmat"),
        day="LUNEDI",
        start_hour=8,
        end_hour=9,
    )
    base.update(kw)
    return Slot.objects.create(**base)


def blocked(admin, c, expected):
    html = htmllib.unescape(admin.get(delete(c)).content.decode())
    assert expected in html
    assert "Conferma" not in html
    r = admin.post(delete(c), **HX)
    assert r.status_code == 200
    body = htmllib.unescape(r.content.decode())
    assert "Impossibile eliminare" in body
    assert expected in body
    assert Class.objects.filter(pk=c.pk).exists()


# --- list ---------------------------------------------------------------


def test_list_both_roles_ordered_with_counts(school, admin, seg):
    klass(school, "bb", student_count=7, group="PRIME")
    a = klass(school, "AA")
    Assignment.objects.create(
        school=school,
        teacher=teach(school),
        school_class=a,
        subject=subj(school),
        ore_totali=10,
    )
    for client in (admin, seg):
        r = client.get(LIST)
        assert r.status_code == 200
        html = r.content.decode()
        assert html.index("AA") < html.index(">bb<")
    rows = re.findall(r"<tr>.*?</tr>", html, re.S)
    assert re.search(r"AA.*<td>0</td>\s*<td>-</td>\s*<td>1</td>", rows[1], re.S)
    assert re.search(r"bb.*<td>7</td>\s*<td>Prime</td>\s*<td>0</td>", rows[2], re.S)


def test_secretary_sees_no_write_controls(school, seg):
    c = klass(school)
    html = seg.get(LIST).content.decode()
    assert "Aggiungi classe" not in html
    assert "Modifica" not in html and "Elimina" not in html
    assert edit(c) not in html and delete(c) not in html
    assert "Dati scuola" not in html


def test_admin_sees_controls_and_hub_link(school, admin):
    c = klass(school)
    html = admin.get(LIST).content.decode()
    assert "Aggiungi classe" in html
    assert edit(c) in html and delete(c) in html
    assert 'aria-label="Modifica II ELETTRICISTI"' in html
    assert 'aria-label="Elimina II ELETTRICISTI"' in html
    assert 'href="/dati-scuola/"' in html


def test_empty_state(school, admin, seg):
    assert "Nessuna classe presente." in seg.get(LIST).content.decode()
    html = admin.get(LIST).content.decode()
    assert "Nessuna classe presente." in html and "Aggiungi classe" in html


def test_no_school(db, admin, seg):
    for client in (admin, seg):
        html = client.get(LIST).content.decode()
        assert "Nessuna scuola configurata: esegui prima il setup." in html
        assert "Aggiungi classe" not in html
    for method in (admin.get, admin.post):
        r = method(CREATE, valid())
        assert r.status_code == 200
        assert "Nessuna scuola configurata" in r.content.decode()
    assert Class.objects.count() == 0
    c = Class(pk=1)
    assert "Nessuna scuola" in admin.get(edit(c)).content.decode()
    assert "Nessuna scuola" in admin.post(delete(c)).content.decode()


def test_other_school_hidden(school, admin):
    klass(make_school("Altra"), "SEGRETO")
    assert "SEGRETO" not in admin.get(LIST).content.decode()


def test_list_query_count_constant(school, admin):
    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert admin.get(LIST).status_code == 200
        return len(ctx)

    klass(school, "A")
    count()
    before = count()
    t, s = teach(school), subj(school)
    for i in range(10):
        c = klass(school, f"C{i}")
        Assignment.objects.create(
            school=school, teacher=t, school_class=c, subject=s, ore_totali=1
        )
    assert count() == before


# --- hub / authorization ------------------------------------------------


def test_hub_links(admin, seg, client):
    html = admin.get("/dati-scuola/").content.decode()
    for url, label in ((LIST, "Classi"), ("/dati-scuola/materie/", "Materie")):
        assert f'href="{url}"' in html and f">{label}<" in html
        assert admin.get(url).status_code == 200
    assert ">Docenti<" in html
    assert seg.get("/dati-scuola/").status_code == 403


def test_anonymous_redirected_everywhere(school, client):
    c = klass(school)
    for url in [LIST, CREATE, edit(c), delete(c)]:
        for method in (client.get, client.post):
            r = method(url, valid())
            assert r.status_code == 302 and r.url.startswith("/login/")
            assert "ELETTRICISTI" not in r.content.decode()
    assert Class.objects.count() == 1


def test_secretary_denied_writes(school, seg):
    c = klass(school)
    for url in (CREATE, edit(c), delete(c)):
        assert seg.get(url).status_code == 403
        assert seg.post(url, valid(name="ALTRO")).status_code == 403
    c.refresh_from_db()
    assert c.name == "II ELETTRICISTI" and Class.objects.count() == 1


def test_no_role_comparison_in_new_code():
    import pathlib

    paths = [
        *pathlib.Path("templates/school/classes").glob("*.html"),
        pathlib.Path("school/views/classes.py"),
        pathlib.Path("school/forms.py"),
    ]
    for path in paths:
        text = path.read_text()
        assert "user.role" not in text and "Role." not in text, path


def test_csrf_enforced(school):
    c = login("ADMIN", enforce_csrf_checks=True)
    k = klass(school)
    for url in (CREATE, edit(k), delete(k)):
        assert c.post(url, valid(name="ALTRO")).status_code == 403
    assert list(Class.objects.values_list("name", flat=True)) == ["II ELETTRICISTI"]


# --- create -------------------------------------------------------------


def test_create_htmx_partial_with_message(school, admin):
    r = admin.post(CREATE, valid(), **HX)
    assert r.status_code == 200
    html = r.content.decode()
    assert "<html" not in html
    assert "Classe III MECCANICI aggiunta." in html
    assert 'id="class-section"' in html and "Terze" in html
    assert Class.objects.get().school == school


def test_create_plain_redirects_with_message(school, admin):
    r = admin.post(CREATE, valid(), follow=True)
    assert r.redirect_chain == [(LIST, 302)]
    assert r.content.decode().count("Classe III MECCANICI aggiunta.") == 1


def test_create_get_partial_and_full(school, admin):
    html = admin.get(CREATE, **HX).content.decode()
    assert "<html" not in html and "<form" in html
    full = admin.get(CREATE).content.decode()
    assert "<html" in full and "csrfmiddlewaretoken" in full
    options = re.findall(r'<option value="([^"]*)"', full.split('name="group"')[1])
    assert options == ["", "PRIME", "SECONDE", "TERZE", "QUARTE", "PRIMA4+2"]


@pytest.mark.parametrize(
    "data,msg",
    [
        ({"name": ""}, "Inserisci il nome della classe."),
        ({"name": "X" * 51}, "al massimo 50 caratteri"),
        ({"student_count": "-1"}, "non può essere negativo"),
        ({"student_count": "abc"}, "numero intero"),
        ({"group": "QUINTE"}, "Scegli un gruppo valido."),
        ({"name": "ii   elettricisti"}, "Esiste già una classe con questo nome."),
    ],
)
def test_create_validation(school, admin, data, msg):
    klass(school)
    for kwargs in ({}, HX):
        r = admin.post(CREATE, valid(**data), **kwargs)
        assert r.status_code == 200
        html = r.content.decode()
        assert msg in html and "<form" in html
        assert 'aria-invalid="true"' in html
        err_id = re.search(r'id="(id_\w+-error)"', html).group(1)
        assert err_id in re.search(r'aria-describedby="([^"]*)"', html).group(1) or (
            f'aria-describedby="{err_id}"' in html
        )
        if kwargs:
            assert r["HX-Retarget"] == "#class-form-area"
    assert Class.objects.count() == 1


def test_empty_count_is_zero_and_blank_group(school, admin):
    admin.post(CREATE, {"name": "1A", "student_count": "", "group": ""})
    c = Class.objects.get()
    assert (c.student_count, c.group) == (0, "")
    admin.post(CREATE, {"name": "1B"})
    assert Class.objects.get(name="1B").student_count == 0


def test_name_trimmed_collapsed_case_kept(school, admin):
    admin.post(CREATE, valid(name=" ii   elettricisti "))
    assert Class.objects.get().name == "ii elettricisti"


def test_same_name_other_school_allowed(school, admin):
    klass(make_school("Altra"), "III MECCANICI")
    admin.post(CREATE, valid())
    assert Class.objects.filter(school=school, name="III MECCANICI").exists()


def test_forged_fields_ignored(school, admin):
    other = make_school("Altra")
    admin.post(CREATE, valid(school=other.pk, id=999, assignment_count=5))
    c = Class.objects.get()
    assert c.school == school and c.pk != 999


def test_race_integrity_error_is_field_error(school, admin, monkeypatch):
    from school import forms

    klass(school, "III MECCANICI")
    monkeypatch.setattr(forms.ClassForm, "clean_name", lambda s: "III MECCANICI")
    r = admin.post(CREATE, valid())
    assert r.status_code == 200
    assert "Esiste già una classe" in r.content.decode()
    assert Class.objects.count() == 1


# --- edit ---------------------------------------------------------------


def test_edit_prefilled(school, admin):
    c = klass(school, student_count=12, group="PRIME")
    html = admin.get(edit(c), **HX).content.decode()
    assert 'value="II ELETTRICISTI"' in html and "<html" not in html
    assert 'value="12"' in html


def test_edit_unchanged_and_changed(school, admin):
    c = klass(school)
    same = {"name": "II ELETTRICISTI", "student_count": "0", "group": ""}
    r = admin.post(edit(c), same, **HX)
    assert "Classe II ELETTRICISTI aggiornata." in r.content.decode()
    r = admin.post(edit(c), valid(), **HX)
    html = r.content.decode()
    c.refresh_from_db()
    assert (c.name, c.student_count, c.group) == ("III MECCANICI", 20, "TERZE")
    assert "III MECCANICI" in html and "Terze" in html and "<td>20</td>" in html


def test_edit_duplicate_rejected(school, admin):
    klass(school, "BETA")
    c = klass(school)
    r = admin.post(edit(c), valid(name="beta"))
    assert "Esiste già una classe" in r.content.decode()
    c.refresh_from_db()
    assert c.name == "II ELETTRICISTI"


def test_edit_cannot_move_school(school, admin):
    other = make_school("Altra")
    c = klass(school)
    admin.post(edit(c), valid(school=other.pk))
    c.refresh_from_db()
    assert c.school == school


def test_edit_delete_404(school, admin):
    other = klass(make_school("Altra"), "SEGRETO")
    for url in (edit(other), delete(other), "/dati-scuola/classi/99999/modifica/"):
        for r in (admin.get(url), admin.post(url, valid())):
            assert r.status_code == 404
            assert "SEGRETO" not in r.content.decode()
    assert Class.objects.filter(name="SEGRETO").exists()
    assert admin.get("/dati-scuola/classi/99999/elimina/").status_code == 404


# --- delete -------------------------------------------------------------


def test_delete_confirm_then_post(school, admin):
    c = klass(school)
    html = admin.get(delete(c)).content.decode()
    assert "Eliminare la classe II ELETTRICISTI?" in html
    assert "Conferma" in html and "Annulla" in html
    assert Class.objects.count() == 1
    r = admin.post(delete(c), **HX)
    assert "Classe II ELETTRICISTI eliminata." in r.content.decode()
    assert "<html" not in r.content.decode()
    assert Class.objects.count() == 0


def test_delete_plain_redirects(school, admin):
    c = klass(school)
    r = admin.post(delete(c), follow=True)
    assert r.redirect_chain == [(LIST, 302)]
    assert "eliminata." in r.content.decode()


def test_delete_blocked_combined_counts(school, admin):
    c = klass(school)
    other = klass(school, "ZZ")
    s, t = subj(school), teach(school)
    for i in range(3):
        Assignment.objects.create(
            school=school,
            teacher=t,
            school_class=c,
            subject=subj(school, f"M{i}"),
            ore_totali=1,
        )
    lo, hi = sorted([c, other], key=lambda x: x.pk)
    ClassPairing.objects.create(school=school, class_a=lo, class_b=hi, subject=s)
    CalendarEntry.objects.create(
        school=school, date=datetime.date(2026, 11, 2), stage_class=c
    )
    expected = "3 assegnazioni, 1 accoppiamento, 1 giorno di stage nel calendario"
    blocked(admin, c, expected)
    page = admin.get(delete(c)).content.decode()
    assert "Rimuovili prima dalle rispettive schermate." in page


def test_delete_blocked_as_class_a(school, admin):
    c = klass(school, "AAA")
    other = klass(school, "BBB")
    ClassPairing.objects.create(
        school=school, class_a=c, class_b=other, subject=subj(school)
    )
    blocked(admin, c, "1 accoppiamento")


def test_delete_blocked_as_class_b(school, admin):
    other = klass(school, "AAA")
    c = klass(school, "BBB")
    ClassPairing.objects.create(
        school=school, class_a=other, class_b=c, subject=subj(school)
    )
    blocked(admin, c, "1 accoppiamento")


def test_delete_blocked_stage_plural(school, admin):
    c = klass(school)
    for day in (2, 3):
        CalendarEntry.objects.create(
            school=school, date=datetime.date(2026, 11, day), stage_class=c
        )
    blocked(admin, c, "2 giorni di stage nel calendario")


def test_delete_blocked_slot_as_class(school, admin):
    c = klass(school)
    slot(school, school_class=c)
    blocked(admin, c, "1 slot dell'orario")


def test_delete_blocked_slot_as_paired_class(school, admin):
    c, other = klass(school), klass(school, "ALTRA")
    slot(school, school_class=other, paired=True, paired_class=c)
    blocked(admin, c, "1 slot dell'orario")


def test_delete_blocked_protected_race(school, admin, monkeypatch):
    c = klass(school)
    slot(school, school_class=c)
    monkeypatch.setattr("school.views.classes.class_reference_summary", lambda obj: "")
    r = admin.post(delete(c), **HX)
    assert r.status_code == 200 and "Impossibile eliminare" in r.content.decode()
    assert Class.objects.filter(pk=c.pk).exists()


# --- escaping -----------------------------------------------------------


def test_script_name_escaped_everywhere(school, admin):
    evil = "<script>alert(1)</script>"
    c = klass(school, evil)
    pages = [
        admin.get(LIST),
        admin.get(edit(c)),
        admin.get(delete(c)),
        admin.post(CREATE, valid(name=evil)),
        admin.post(delete(c), **HX),
    ]
    for r in pages:
        assert "<script>alert" not in r.content.decode()
    assert "&lt;script&gt;" in pages[0].content.decode()

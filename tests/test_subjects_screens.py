import datetime
import html as htmllib
import re

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from scheduling.models import Slot, Timetable
from school.models import Assignment, Class, ClassPairing, School, Subject, Teacher

PW = "correct-horse"
LIST = "/dati-scuola/materie/"
CREATE = "/dati-scuola/materie/nuova/"
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


def subj(school, name="Italiano", kind="TEORIA", weight="ALTO"):
    return Subject.objects.create(
        school=school, name=name, kind=kind, cognitive_weight=weight
    )


def edit(s):
    return f"/dati-scuola/materie/{s.pk}/modifica/"


def delete(s):
    return f"/dati-scuola/materie/{s.pk}/elimina/"


def valid(**kw):
    data = {"name": "Storia", "kind": "PRATICA", "cognitive_weight": "BASSO"}
    data.update(kw)
    return data


def klass(school, name):
    return Class.objects.create(school=school, name=name)


def teach(school):
    return Teacher.objects.create(school=school, name="ROSSI MARIO", type="ASSUNTO")


def blocked(admin, s, expected):
    page = htmllib.unescape(admin.get(delete(s)).content.decode())
    assert expected in page and "Conferma" not in page
    r = admin.post(delete(s), **HX)
    assert r.status_code == 200
    body = htmllib.unescape(r.content.decode())
    assert "Impossibile eliminare" in body and expected in body
    assert Subject.objects.filter(pk=s.pk).exists()


# --- list ---------------------------------------------------------------


def test_list_both_roles_ordered_with_counts(school, admin, seg):
    subj(school, "bb", "PRATICA", "BASSO")
    a = subj(school, "AA", "TEORIA", "MEDIO")
    Assignment.objects.create(
        school=school,
        teacher=teach(school),
        school_class=klass(school, "1A"),
        subject=a,
        ore_totali=10,
    )
    for client in (admin, seg):
        r = client.get(LIST)
        assert r.status_code == 200
        html = r.content.decode()
        assert html.index("AA") < html.index(">bb<")
    rows = re.findall(r"<tr>.*?</tr>", html, re.S)
    assert re.search(
        r"AA.*<td>Teoria</td>\s*<td>Medio</td>\s*<td>1</td>", rows[1], re.S
    )
    assert re.search(
        r"bb.*<td>Pratica</td>\s*<td>Basso</td>\s*<td>0</td>", rows[2], re.S
    )


def test_secretary_sees_no_write_controls(school, seg):
    s = subj(school)
    html = seg.get(LIST).content.decode()
    assert "Aggiungi materia" not in html
    assert "Modifica" not in html and "Elimina" not in html
    assert edit(s) not in html and delete(s) not in html
    assert "Dati scuola" not in html


def test_admin_sees_controls_and_hub_link(school, admin):
    s = subj(school)
    html = admin.get(LIST).content.decode()
    assert "Aggiungi materia" in html
    assert edit(s) in html and delete(s) in html
    assert 'aria-label="Modifica Italiano"' in html
    assert 'aria-label="Elimina Italiano"' in html
    assert 'href="/dati-scuola/"' in html


def test_empty_state(school, admin, seg):
    assert "Nessuna materia presente." in seg.get(LIST).content.decode()
    html = admin.get(LIST).content.decode()
    assert "Nessuna materia presente." in html and "Aggiungi materia" in html


def test_no_school(db, admin, seg):
    for client in (admin, seg):
        html = client.get(LIST).content.decode()
        assert "Nessuna scuola configurata: esegui prima il setup." in html
        assert "Aggiungi materia" not in html
    for method in (admin.get, admin.post):
        r = method(CREATE, valid())
        assert r.status_code == 200
        assert "Nessuna scuola configurata" in r.content.decode()
    assert Subject.objects.count() == 0
    s = Subject(pk=1)
    assert "Nessuna scuola" in admin.get(edit(s)).content.decode()
    assert "Nessuna scuola" in admin.post(delete(s)).content.decode()


def test_other_school_hidden(school, admin):
    subj(make_school("Altra"), "SEGRETO")
    assert "SEGRETO" not in admin.get(LIST).content.decode()


def test_list_query_count_constant(school, admin):
    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert admin.get(LIST).status_code == 200
        return len(ctx)

    subj(school, "A")
    count()
    before = count()
    t, c = teach(school), klass(school, "1A")
    for i in range(10):
        s = subj(school, f"M{i}")
        Assignment.objects.create(
            school=school, teacher=t, school_class=c, subject=s, ore_totali=1
        )
    assert count() == before


# --- authorization ------------------------------------------------------


def test_hub_links(admin, seg, client):
    html = admin.get("/dati-scuola/").content.decode()
    assert f'href="{LIST}"' in html and ">Materie<" in html
    assert admin.get(LIST).status_code == 200
    assert seg.get("/dati-scuola/").status_code == 403
    assert client.get("/dati-scuola/").status_code == 302


def test_anonymous_redirected_everywhere(school, client):
    s = subj(school)
    for url in [LIST, CREATE, edit(s), delete(s)]:
        for method in (client.get, client.post):
            r = method(url, valid())
            assert r.status_code == 302 and r.url.startswith("/login/")
            assert "Italiano" not in r.content.decode()
    assert Subject.objects.count() == 1


def test_secretary_denied_writes(school, seg):
    s = subj(school)
    for url in (CREATE, edit(s), delete(s)):
        assert seg.get(url).status_code == 403
        assert seg.post(url, valid(name="Altro")).status_code == 403
    s.refresh_from_db()
    assert s.name == "Italiano" and Subject.objects.count() == 1


def test_no_role_comparison_in_new_code():
    import pathlib

    paths = [
        *pathlib.Path("templates/school/subjects").glob("*.html"),
        pathlib.Path("school/views/subjects.py"),
    ]
    for path in paths:
        text = path.read_text()
        assert "user.role" not in text and "Role." not in text, path


def test_csrf_enforced(school):
    c = login("ADMIN", enforce_csrf_checks=True)
    s = subj(school)
    for url in (CREATE, edit(s), delete(s)):
        assert c.post(url, valid(name="Altro")).status_code == 403
    assert list(Subject.objects.values_list("name", flat=True)) == ["Italiano"]


# --- create -------------------------------------------------------------


def test_create_htmx_partial_with_message(school, admin):
    r = admin.post(CREATE, valid(), **HX)
    assert r.status_code == 200
    html = r.content.decode()
    assert "<html" not in html
    assert "Materia Storia aggiunta." in html
    assert 'id="subject-section"' in html and "Pratica" in html
    assert Subject.objects.get().school == school


def test_create_plain_redirects_with_message(school, admin):
    r = admin.post(CREATE, valid(), follow=True)
    assert r.redirect_chain == [(LIST, 302)]
    assert r.content.decode().count("Materia Storia aggiunta.") == 1


def test_create_get_partial_and_full(school, admin):
    html = admin.get(CREATE, **HX).content.decode()
    assert "<html" not in html and "<form" in html
    full = admin.get(CREATE).content.decode()
    assert "<html" in full and "csrfmiddlewaretoken" in full
    for field, values in (
        ("kind", ["", "TEORIA", "PRATICA"]),
        ("cognitive_weight", ["", "ALTO", "MEDIO", "BASSO"]),
    ):
        part = full.split(f'name="{field}"')[1].split("</select>")[0]
        assert re.findall(r'<option value="([^"]*)"', part) == values
        assert part.count("selected") == 1 and 'value="" selected' in part


@pytest.mark.parametrize(
    "data,msg",
    [
        ({"name": ""}, "Inserisci il nome della materia."),
        ({"name": "X" * 256}, "al massimo 255 caratteri"),
        ({"kind": ""}, "Scegli il tipo di materia."),
        ({"cognitive_weight": ""}, "Scegli il peso cognitivo."),
        ({"kind": "MISTA"}, "Scegli un tipo valido."),
        ({"cognitive_weight": "ENORME"}, "Scegli un peso valido."),
        ({"name": "ITALIANO"}, "Esiste già una materia con questo nome."),
    ],
)
def test_create_validation(school, admin, data, msg):
    subj(school)
    for kwargs in ({}, HX):
        r = admin.post(CREATE, valid(**data), **kwargs)
        assert r.status_code == 200
        html = r.content.decode()
        assert msg in html and "<form" in html
        assert 'aria-invalid="true"' in html
        err_id = re.search(r'id="(id_\w+-error)"', html).group(1)
        assert f'aria-describedby="{err_id}"' in html
        if kwargs:
            assert r["HX-Retarget"] == "#subject-form-area"
    assert Subject.objects.count() == 1


def test_name_trimmed_collapsed_case_kept(school, admin):
    admin.post(CREATE, valid(name=" ii   elettricisti "))
    assert Subject.objects.get().name == "ii elettricisti"


def test_same_name_other_school_allowed(school, admin):
    subj(make_school("Altra"), "Storia")
    admin.post(CREATE, valid())
    assert Subject.objects.filter(school=school, name="Storia").exists()


def test_forged_fields_ignored(school, admin):
    other = make_school("Altra")
    admin.post(CREATE, valid(school=other.pk, id=999, assignment_count=5))
    s = Subject.objects.get()
    assert s.school == school and s.pk != 999


def test_race_integrity_error_is_field_error(school, admin, monkeypatch):
    from school import forms

    subj(school, "Storia")
    monkeypatch.setattr(forms.SubjectForm, "clean_name", lambda s: "Storia")
    r = admin.post(CREATE, valid())
    assert r.status_code == 200
    assert "Esiste già una materia" in r.content.decode()
    assert Subject.objects.count() == 1


# --- edit ---------------------------------------------------------------


def test_edit_prefilled(school, admin):
    s = subj(school)
    html = admin.get(edit(s), **HX).content.decode()
    assert 'value="Italiano"' in html and "<html" not in html
    assert re.search(r'<option value="TEORIA" selected>', html)


def test_edit_unchanged_and_changed(school, admin):
    s = subj(school)
    same = {"name": "italiano", "kind": "TEORIA", "cognitive_weight": "ALTO"}
    r = admin.post(edit(s), same, **HX)
    assert "Materia italiano aggiornata." in r.content.decode()
    r = admin.post(edit(s), valid(), **HX)
    s.refresh_from_db()
    assert (s.name, s.kind, s.cognitive_weight) == ("Storia", "PRATICA", "BASSO")
    html = r.content.decode()
    assert "Storia" in html and "Pratica" in html and "Basso" in html


def test_edit_duplicate_rejected(school, admin):
    subj(school, "Storia")
    s = subj(school)
    r = admin.post(edit(s), valid(name="STORIA"))
    assert "Esiste già una materia" in r.content.decode()
    s.refresh_from_db()
    assert s.name == "Italiano"


def test_edit_cannot_move_school(school, admin):
    other = make_school("Altra")
    s = subj(school)
    admin.post(edit(s), valid(school=other.pk))
    s.refresh_from_db()
    assert s.school == school


def test_edit_delete_404(school, admin):
    other = subj(make_school("Altra"), "SEGRETO")
    for url in (edit(other), delete(other), "/dati-scuola/materie/99999/modifica/"):
        for r in (admin.get(url), admin.post(url, valid())):
            assert r.status_code == 404
            assert "SEGRETO" not in r.content.decode()
    assert Subject.objects.filter(name="SEGRETO").exists()
    assert admin.get("/dati-scuola/materie/99999/elimina/").status_code == 404


# --- delete -------------------------------------------------------------


def test_delete_confirm_then_post(school, admin):
    s = subj(school)
    html = admin.get(delete(s)).content.decode()
    assert "Eliminare la materia Italiano?" in html
    assert "Conferma" in html and "Annulla" in html
    assert Subject.objects.count() == 1
    r = admin.post(delete(s), **HX)
    assert "Materia Italiano eliminata." in r.content.decode()
    assert "<html" not in r.content.decode()
    assert Subject.objects.count() == 0


def test_delete_plain_redirects(school, admin):
    s = subj(school)
    r = admin.post(delete(s), follow=True)
    assert r.redirect_chain == [(LIST, 302)]
    assert "eliminata." in r.content.decode()


def test_delete_blocked_assignments_and_pairing(school, admin):
    s = subj(school)
    t = teach(school)
    c1, c2, c3 = klass(school, "1A"), klass(school, "1B"), klass(school, "1C")
    for c in (c1, c2):
        Assignment.objects.create(
            school=school, teacher=t, school_class=c, subject=s, ore_totali=1
        )
    ClassPairing.objects.create(school=school, class_a=c1, class_b=c3, subject=s)
    blocked(admin, s, "2 assegnazioni, 1 accoppiamento")
    assert "Rimuovili prima dalle rispettive schermate." in (
        admin.get(delete(s)).content.decode()
    )


def test_delete_blocked_only_pairing(school, admin):
    s = subj(school)
    c1, c2 = klass(school, "1A"), klass(school, "1B")
    ClassPairing.objects.create(school=school, class_a=c1, class_b=c2, subject=s)
    blocked(admin, s, "1 accoppiamento")


def test_delete_blocked_only_slot(school, admin):
    s = subj(school)
    tt = Timetable.objects.create(school=school, week_start=MON)
    Slot.objects.create(
        timetable=tt,
        school_class=klass(school, "1A"),
        teacher=teach(school),
        subject=s,
        day="LUNEDI",
        start_hour=8,
        end_hour=9,
    )
    blocked(admin, s, "1 slot dell'orario")


def test_delete_blocked_protected_race(school, admin, monkeypatch):
    s = subj(school)
    c = klass(school, "1A")
    Assignment.objects.create(
        school=school, teacher=teach(school), school_class=c, subject=s, ore_totali=1
    )
    monkeypatch.setattr("school.views.subjects.subject_reference_summary", lambda o: "")
    r = admin.post(delete(s), **HX)
    assert r.status_code == 200 and "Impossibile eliminare" in r.content.decode()
    assert Subject.objects.filter(pk=s.pk).exists()


# --- escaping -----------------------------------------------------------


def test_script_name_escaped_everywhere(school, admin):
    evil = "<script>alert(1)</script>"
    s = subj(school, evil)
    pages = [
        admin.get(LIST),
        admin.get(edit(s)),
        admin.get(delete(s)),
        admin.post(CREATE, valid(name=evil)),
        admin.post(delete(s), **HX),
    ]
    for r in pages:
        assert "<script>alert" not in r.content.decode()
    assert "&lt;script&gt;" in pages[0].content.decode()

import datetime
import re

import pytest
from django.test import Client

from accounts.models import User
from availability.models import Availability
from scheduling.models import Slot, Timetable
from school.models import (
    Assignment,
    Class,
    ClassPairing,
    School,
    Subject,
    Teacher,
)

PW = "correct-horse"
LIST = "/dati-scuola/docenti/"
CREATE = "/dati-scuola/docenti/nuovo/"
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


def teacher(school, name="ROSSI MARIO", **kw):
    kw.setdefault("type", "ASSUNTO")
    return Teacher.objects.create(school=school, name=name, **kw)


def edit(t):
    return f"/dati-scuola/docenti/{t.pk}/modifica/"


def delete(t):
    return f"/dati-scuola/docenti/{t.pk}/elimina/"


def valid(**kw):
    data = {"name": "Verdi Anna", "email": "", "type": "CONTRATTO", "active": "on"}
    data.update(kw)
    return data


def refs(school, t):
    c1 = Class.objects.create(school=school, name="1A")
    c2 = Class.objects.create(school=school, name="1B")
    s = Subject.objects.create(
        school=school, name="Italiano", kind="TEORIA", cognitive_weight="ALTO"
    )
    return c1, c2, s


# --- list ---------------------------------------------------------------


def test_list_both_roles_ordered_with_counts(school, admin, seg):
    b = teacher(school, "bianchi luca", type="CONTRATTO", active=False)
    a = teacher(school, "ROSSI MARIO")
    c1, _, s = refs(school, a)
    Assignment.objects.create(
        school=school, teacher=a, school_class=c1, subject=s, ore_totali=10
    )
    for client in (admin, seg):
        r = client.get(LIST)
        assert r.status_code == 200
        html = r.content.decode()
        assert html.index("bianchi luca") < html.index("ROSSI MARIO")
        assert "Contratto" in html and "Assunto" in html
    rows = re.findall(r"<tr>.*?</tr>", html, re.S)
    assert re.search(r"ROSSI MARIO.*<td>Sì</td>\s*<td>1</td>", rows[2], re.S)
    assert re.search(r"<td>No</td>\s*<td>0</td>", rows[1], re.S)
    assert b.pk


def test_secretary_sees_no_write_controls(school, seg):
    t = teacher(school)
    html = seg.get(LIST).content.decode()
    assert "Aggiungi docente" not in html
    assert "Modifica" not in html and "Elimina" not in html
    assert edit(t) not in html and delete(t) not in html
    assert "Dati scuola" not in html


def test_admin_sees_controls_and_hub_link(school, admin):
    t = teacher(school)
    html = admin.get(LIST).content.decode()
    assert "Aggiungi docente" in html
    assert edit(t) in html and delete(t) in html
    assert 'href="/dati-scuola/"' in html


def test_empty_state(school, admin):
    html = admin.get(LIST).content.decode()
    assert "Nessun docente presente." in html
    assert "Aggiungi docente" in html


def test_no_school(db, admin, seg):
    for client in (admin, seg):
        html = client.get(LIST).content.decode()
        assert "Nessuna scuola configurata: esegui prima il setup." in html
        assert "Aggiungi docente" not in html
    r = admin.get(CREATE)
    assert r.status_code == 200
    assert "Nessuna scuola configurata" in r.content.decode()
    assert "<form" not in r.content.decode().split("<main")[1].split("</main>")[0]
    assert admin.post(CREATE, valid()).status_code == 200
    assert Teacher.objects.count() == 0


def test_list_other_school_hidden(school, admin):
    teacher(make_school("Altra"), "SEGRETO NOME")
    assert "SEGRETO" not in admin.get(LIST).content.decode()


def test_list_query_count_constant(school, admin):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert admin.get(LIST).status_code == 200
        return len(ctx)

    teacher(school, "A B")
    count()  # warm up
    before = count()
    for i in range(10):
        teacher(school, f"COGNOME{i} NOME")
    assert count() == before


# --- hub ----------------------------------------------------------------


def test_hub(admin, seg, client):
    r = admin.get("/dati-scuola/")
    html = r.content.decode()
    assert r.status_code == 200 and "<h1>Dati scuola</h1>" in html
    assert f'href="{LIST}"' in html and ">Docenti<" in html
    assert seg.get("/dati-scuola/").status_code == 403
    assert client.get("/dati-scuola/").status_code == 302


# --- authorization ------------------------------------------------------


def urls_for(t):
    return [CREATE, edit(t), delete(t)]


def test_anonymous_redirected_everywhere(school, client):
    t = teacher(school)
    for url in [LIST, *urls_for(t)]:
        for method in (client.get, client.post):
            r = method(url, valid())
            assert r.status_code == 302 and r.url.startswith("/login/")
            assert "ROSSI" not in r.content.decode()
    assert Teacher.objects.count() == 1


def test_secretary_denied_writes(school, seg):
    t = teacher(school)
    for url in urls_for(t):
        r = seg.get(url)
        assert r.status_code == 403
        assert "ADMIN" not in r.content.decode()
        assert "Amministratore" not in r.content.decode().split("<main")[1]
        assert seg.post(url, valid(name="Altro Nome")).status_code == 403
    t.refresh_from_db()
    assert t.name == "ROSSI MARIO"
    assert Teacher.objects.count() == 1


def test_no_role_comparison_in_school_code():
    import pathlib

    for path in list(pathlib.Path("school").rglob("*.py")) + list(
        pathlib.Path("templates/school").rglob("*.html")
    ):
        if "migrations" in path.parts or "importers" in path.parts:
            continue
        text = path.read_text()
        assert "user.role" not in text and "Role." not in text, path


def test_csrf_enforced(school):
    c = login("ADMIN", enforce_csrf_checks=True)
    t = teacher(school)
    for url in urls_for(t):
        assert c.post(url, valid(name="Altro Nome")).status_code == 403
    assert list(Teacher.objects.values_list("name", flat=True)) == ["ROSSI MARIO"]


# --- create -------------------------------------------------------------


def test_create_htmx_partial_with_message(school, admin):
    r = admin.post(CREATE, valid(), **HX)
    assert r.status_code == 200
    html = r.content.decode()
    assert "<html" not in html
    assert "Docente VERDI ANNA aggiunto." in html
    assert "VERDI ANNA" in html and 'id="teacher-section"' in html
    assert Teacher.objects.get().school == school


def test_create_plain_redirects_with_message(school, admin):
    r = admin.post(CREATE, valid(), follow=True)
    assert r.redirect_chain == [(LIST, 302)]
    assert "Docente VERDI ANNA aggiunto." in r.content.decode()
    assert r.content.decode().count("Docente VERDI ANNA aggiunto.") == 1


def test_create_get_htmx_partial_and_full(school, admin):
    html = admin.get(CREATE, **HX).content.decode()
    assert "<html" not in html and "<form" in html
    full = admin.get(CREATE).content.decode()
    assert "<html" in full and "csrfmiddlewaretoken" in full
    m = re.search(r'name="active"[^>]*>', full)
    assert "checked" in m.group(0)
    m = re.search(r'name="first_time"[^>]*>', full)
    assert "checked" in m.group(0)
    assert 'value="ASSUNTO"' in full


@pytest.mark.parametrize(
    "data,msg",
    [
        ({"name": ""}, "Inserisci il cognome e il nome."),
        ({"name": "ROSSI"}, "Inserisci cognome e nome"),
        ({"name": "Rossi   mario"}, "Esiste già un docente"),
        ({"email": "abc"}, "indirizzo email valido"),
        ({"type": ""}, "Scegli il tipo di docente."),
        ({"type": "SUPPLENTE"}, "Scegli un tipo valido."),
    ],
)
def test_create_validation(school, admin, data, msg):
    teacher(school)
    for kwargs in ({}, HX):
        r = admin.post(CREATE, valid(**data), **kwargs)
        assert r.status_code == 200
        html = r.content.decode()
        assert msg in html
        assert 'aria-describedby="' in html and "-error" in html
        err_id = re.search(r'id="(id_\w+-error)"', html).group(1)
        assert err_id in re.search(r'aria-describedby="([^"]*)"', html).group(1) or (
            f'aria-describedby="{err_id}"' in html
        )
        assert "<form" in html
    assert Teacher.objects.count() == 1


def test_name_normalised_and_email_optional(school, admin):
    admin.post(CREATE, valid(name="  verdi   anna "))
    t = Teacher.objects.get()
    assert t.name == "VERDI ANNA" and t.email == ""


def test_name_too_long(school, admin):
    r = admin.post(CREATE, valid(name="A " + "B" * 300))
    assert "al massimo 255" in r.content.decode()
    assert Teacher.objects.count() == 0


def test_same_name_other_school_allowed(school, admin):
    teacher(make_school("Altra"), "VERDI ANNA")
    admin.post(CREATE, valid())
    assert Teacher.objects.filter(school=school, name="VERDI ANNA").exists()


def test_forged_fields_ignored(school, admin):
    other = make_school("Altra")
    admin.post(CREATE, valid(school=other.pk, id=999, assignment_count=5))
    t = Teacher.objects.get()
    assert t.school == school and t.pk != 999


def test_race_integrity_error_is_field_error(school, admin, monkeypatch):
    from school import forms

    def stale(self):
        return self.cleaned_data["name"]

    teacher(school, "VERDI ANNA")
    monkeypatch.setattr(forms.TeacherForm, "clean_name", lambda s: "VERDI ANNA")
    r = admin.post(CREATE, valid())
    assert r.status_code == 200
    assert "Esiste già un docente" in r.content.decode()
    assert Teacher.objects.count() == 1


# --- edit ---------------------------------------------------------------


def test_edit_form_prefilled(school, admin):
    t = teacher(school)
    html = admin.get(edit(t), **HX).content.decode()
    assert 'value="ROSSI MARIO"' in html and "<html" not in html


def test_edit_same_name_and_fields(school, admin):
    t = teacher(school)
    r = admin.post(edit(t), {"name": "rossi mario", "type": "CONTRATTO"}, **HX)
    assert "Docente ROSSI MARIO aggiornato." in r.content.decode()
    t.refresh_from_db()
    assert (t.type, t.active, t.first_time) == ("CONTRATTO", False, False)
    assert "Contratto" in r.content.decode()


def test_edit_duplicate_and_invalid(school, admin):
    teacher(school, "BIANCHI LUCA")
    t = teacher(school)
    r = admin.post(edit(t), valid(name="bianchi luca"))
    assert "Esiste già" in r.content.decode()
    t.refresh_from_db()
    assert t.name == "ROSSI MARIO"


def test_edit_cannot_move_school(school, admin):
    other = make_school("Altra")
    t = teacher(school)
    admin.post(edit(t), valid(school=other.pk))
    t.refresh_from_db()
    assert t.school == school


def test_edit_delete_404(school, admin):
    other = teacher(make_school("Altra"), "SEGRETO NOME")
    for url in (
        edit(other),
        delete(other),
        "/dati-scuola/docenti/99999/modifica/",
        "/dati-scuola/docenti/99999/elimina/",
    ):
        for r in (admin.get(url), admin.post(url, valid())):
            assert r.status_code == 404
            assert "SEGRETO" not in r.content.decode()
    assert Teacher.objects.filter(name="SEGRETO NOME").exists()


# --- delete -------------------------------------------------------------


def test_delete_confirm_then_post(school, admin):
    t = teacher(school)
    r = admin.get(delete(t))
    assert "ROSSI MARIO" in r.content.decode() and "<form" in r.content.decode()
    assert Teacher.objects.count() == 1  # GET never deletes
    r = admin.post(delete(t), **HX)
    assert "Docente ROSSI MARIO eliminato." in r.content.decode()
    assert "<html" not in r.content.decode()
    assert Teacher.objects.count() == 0


def test_delete_plain_redirects(school, admin):
    t = teacher(school)
    r = admin.post(delete(t), follow=True)
    assert r.redirect_chain == [(LIST, 302)]
    assert "eliminato." in r.content.decode()


def test_delete_blocked_assignments_and_pairing(school, admin):
    t = teacher(school)
    c1, c2, s = refs(school, t)
    s2 = Subject.objects.create(
        school=school, name="Storia", kind="TEORIA", cognitive_weight="MEDIO"
    )
    for subj in (s, s2):
        Assignment.objects.create(
            school=school, teacher=t, school_class=c1, subject=subj, ore_totali=5
        )
    ClassPairing.objects.create(
        school=school, class_a=c1, class_b=c2, subject=s, teacher=t
    )
    for kwargs in ({}, HX):
        r = admin.post(delete(t), follow=True, **kwargs)
        html = r.content.decode()
        assert "2 assegnazioni" in html and "1 accoppiamento" in html
        assert "disattiva il docente" in html
    assert Teacher.objects.filter(pk=t.pk).exists()
    assert "Impossibile eliminare" in admin.get(delete(t)).content.decode()


def test_delete_blocked_availability(school, admin):
    t = teacher(school)
    Availability.objects.create(
        school=school, teacher=t, week_start=MON, slots={"lunedi": []}
    )
    r = admin.post(delete(t), follow=True)
    assert r.status_code == 200
    assert "1 disponibilità" in r.content.decode()
    assert Teacher.objects.filter(pk=t.pk).exists()


def test_delete_blocked_slot(school, admin):
    t = teacher(school)
    c1, _, s = refs(school, t)
    tt = Timetable.objects.create(school=school, week_start=MON)
    Slot.objects.create(
        timetable=tt,
        school_class=c1,
        teacher=t,
        subject=s,
        day="LUNEDI",
        start_hour=8,
        end_hour=9,
    )
    r = admin.post(delete(t), follow=True)
    assert r.status_code == 200
    assert "1 slot dell&#x27;orario" in r.content.decode()
    assert Teacher.objects.filter(pk=t.pk).exists()


def test_delete_protected_error_safety_net(school, admin, monkeypatch):
    t = teacher(school)
    from school.views import teachers as v

    monkeypatch.setattr(v, "teacher_reference_summary", lambda _t: "")
    from django.db.models import ProtectedError

    def boom(self, *a, **k):
        raise ProtectedError("x", [])

    monkeypatch.setattr(Teacher, "delete", boom)
    r = admin.post(delete(t), follow=True)
    assert r.status_code == 200
    assert "Impossibile eliminare" in r.content.decode()

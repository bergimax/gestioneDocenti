import datetime
import html as htmllib
import pathlib
import re

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from school.models import Assignment, Class, ClassPairing, School, Subject, Teacher

PW = "correct-horse"
LIST = "/dati-scuola/accoppiamenti/"
CREATE = "/dati-scuola/accoppiamenti/nuovo/"
HX = {"HTTP_HX_REQUEST": "true"}


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
    return Teacher.objects.create(school=school, name=name, type="ASSUNTO", **kw)


def klass(school, name):
    return Class.objects.create(school=school, name=name)


def subj(school, name="Matematica"):
    return Subject.objects.create(
        school=school, name=name, kind="TEORIA", cognitive_weight="ALTO"
    )


def assign(school, t, c, s):
    return Assignment.objects.create(
        school=school, teacher=t, school_class=c, subject=s, ore_totali=10
    )


@pytest.fixture
def world(school):
    """Two classes (x has the lower id), a subject and a teacher, all assigned."""
    x, y = klass(school, "II ELETTRICISTI"), klass(school, "III ELETTRICISTI")
    s, t = subj(school), teacher(school)
    assign(school, t, x, s)
    assign(school, t, y, s)
    return type("W", (), {"x": x, "y": y, "s": s, "t": t, "school": school})


def post_data(a, b, s, t=None, note=""):
    return {
        "class_a": a.pk,
        "class_b": b.pk,
        "subject": s.pk,
        "teacher": t.pk if t else "",
        "note": note,
    }


def make_pairing(school, a, b, s, t=None, note=""):
    a, b = sorted((a, b), key=lambda c: c.pk)
    return ClassPairing.objects.create(
        school=school, class_a=a, class_b=b, subject=s, teacher=t, note=note
    )


def edit(p):
    return f"/dati-scuola/accoppiamenti/{p.pk}/modifica/"


def delete(p):
    return f"/dati-scuola/accoppiamenti/{p.pk}/elimina/"


def text(r):
    return htmllib.unescape(r.content.decode())


# --- list ---------------------------------------------------------------


def test_list_both_roles(world, admin, seg):
    make_pairing(world.school, world.x, world.y, world.s, None)
    make_pairing(world.school, world.x, world.y, world.s, world.t, note="aula 3")
    for client in (admin, seg):
        r = client.get(LIST)
        assert r.status_code == 200
        rows = re.findall(r"<tr>.*?</tr>", r.content.decode(), re.S)
        first = [
            c.strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", rows[1], re.S)
        ]
        assert (
            first[:5]
            == ["II ELETTRICISTI", "III ELETTRICISTI", "Matematica", "-", "-"][:5]
        )
    second = [
        c.strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", rows[2], re.S)
    ]
    assert second[3:5] == ["ROSSI MARIO", "aula 3"]


def test_secretary_sees_no_write_controls(world, seg):
    p = make_pairing(world.school, world.x, world.y, world.s)
    html = seg.get(LIST).content.decode()
    assert "Aggiungi accoppiamento" not in html
    assert "Modifica" not in html and "Elimina" not in html
    assert edit(p) not in html and delete(p) not in html
    assert "Dati scuola" not in html


def test_admin_controls_and_labels(world, admin):
    p = make_pairing(world.school, world.x, world.y, world.s)
    html = admin.get(LIST).content.decode()
    assert "Aggiungi accoppiamento" in html and edit(p) in html and delete(p) in html
    name = "II ELETTRICISTI + III ELETTRICISTI - Matematica"
    assert f'aria-label="Modifica accoppiamento {name}"' in html
    assert f'aria-label="Elimina accoppiamento {name}"' in html
    assert 'aria-live="polite"' in html and 'href="/dati-scuola/"' in html


def test_empty_state_and_no_school(school, admin, seg):
    assert "Nessun accoppiamento presente." in seg.get(LIST).content.decode()
    html = admin.get(LIST).content.decode()
    assert "Nessun accoppiamento presente." in html and "Aggiungi accoppiamento" in html
    School.objects.all().delete()
    for client in (admin, seg):
        html = client.get(LIST).content.decode()
        assert "Nessuna scuola configurata: esegui prima il setup." in html
        assert "Aggiungi accoppiamento" not in html
    p = ClassPairing(pk=1)
    for method in (admin.get, admin.post):
        for url in (CREATE, edit(p), delete(p)):
            r = method(url)
            assert r.status_code == 200 and "Nessuna scuola configurata" in text(r)


def test_ordering(world, admin):
    z = klass(world.school, "zz")
    assign(world.school, world.t, z, world.s)
    first = make_pairing(world.school, world.x, z, world.s)
    second = make_pairing(world.school, world.x, world.y, world.s)
    html = admin.get(LIST).content.decode()
    assert html.index(delete(second)) < html.index(delete(first))


def test_list_query_count_constant(world, admin):
    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert admin.get(LIST).status_code == 200
        return len(ctx)

    s = world.s
    for i in range(3):
        c = klass(world.school, f"K{i}")
        assign(world.school, world.t, c, s)
        make_pairing(world.school, world.x, c, s, world.t if i else None)
    three = count()
    for i in range(3, 30):
        c = klass(world.school, f"K{i}")
        assign(world.school, world.t, c, s)
        make_pairing(world.school, world.x, c, s, world.t if i % 2 else None)
    assert count() == three


def test_other_school_hidden(world, admin):
    other = make_school("Altra")
    a, b, s = klass(other, "SEGRETO A"), klass(other, "SEGRETO B"), subj(other)
    make_pairing(other, a, b, s)
    assert "SEGRETO" not in admin.get(LIST).content.decode()
    html = admin.get(CREATE).content.decode()
    assert "SEGRETO" not in html


# --- authorization ------------------------------------------------------


def test_anonymous_redirected_everywhere(world, client):
    p = make_pairing(world.school, world.x, world.y, world.s)
    for url in [LIST, CREATE, edit(p), delete(p)]:
        for method in (client.get, client.post):
            r = method(url, {})
            assert r.status_code == 302 and r.url.startswith("/login/")
    assert ClassPairing.objects.count() == 1


def test_secretary_denied_writes(world, seg):
    p = make_pairing(world.school, world.x, world.y, world.s)
    data = post_data(world.x, world.y, world.s, world.t)
    for url in (CREATE, edit(p), delete(p)):
        assert seg.get(url).status_code == 403
        assert seg.post(url, data).status_code == 403
    p.refresh_from_db()
    assert p.teacher is None and ClassPairing.objects.count() == 1


def test_csrf_enforced(world):
    c = login("ADMIN", enforce_csrf_checks=True)
    p = make_pairing(world.school, world.x, world.y, world.s)
    data = post_data(world.x, world.y, world.s, world.t)
    for url in (CREATE, edit(p), delete(p)):
        assert c.post(url, data).status_code == 403
    assert (
        ClassPairing.objects.count() == 1 and ClassPairing.objects.get().teacher is None
    )


# --- create -------------------------------------------------------------


def test_create_htmx_and_plain(world, admin):
    r = admin.post(CREATE, post_data(world.x, world.y, world.s, note="aula 3"), **HX)
    assert r.status_code == 200 and "<html" not in text(r)
    assert (
        "Accoppiamento II ELETTRICISTI + III ELETTRICISTI - Matematica aggiunto."
        in text(r)
    )
    p = ClassPairing.objects.get()
    assert (p.school, p.teacher, p.note) == (world.school, None, "aula 3")
    r = admin.post(CREATE, post_data(world.x, world.y, world.s, world.t), follow=True)
    assert r.redirect_chain == [(LIST, 302)]
    assert "aggiunto." in text(r)
    assert ClassPairing.objects.count() == 2


def test_create_get_partial_and_full(world, admin):
    teacher(world.school, "INATTIVO DUE", active=False)
    html = admin.get(CREATE, **HX).content.decode()
    assert "<html" not in html and "<form" in html and "Nessun docente" in html
    assert "INATTIVO" not in html and "ROSSI MARIO" in html
    assert "csrfmiddlewaretoken" in admin.get(CREATE).content.decode()


@pytest.mark.parametrize("reverse", [False, True])
def test_either_order_stores_canonical(world, admin, reverse):
    a, b = (world.y, world.x) if reverse else (world.x, world.y)
    admin.post(CREATE, post_data(a, b, world.s))
    p = ClassPairing.objects.get()
    assert p.class_a_id == world.x.pk < p.class_b_id == world.y.pk
    html = admin.get(LIST).content.decode()
    assert html.index("II ELETTRICISTI") < html.index(">III ELETTRICISTI<")
    form = admin.get(edit(p)).content.decode()
    pick = re.search(r'name="class_a".*?<option value="(\d+)" selected', form, re.S)
    assert pick.group(1) == str(world.x.pk)


def test_same_class_twice(world, admin):
    for kwargs in ({}, HX):
        r = admin.post(CREATE, post_data(world.x, world.x, world.s), **kwargs)
        assert r.status_code == 200 and "Scegli due classi diverse." in text(r)
    assert ClassPairing.objects.count() == 0


def lacking(world):
    z = klass(world.school, "IV SENZA")
    return z


def test_first_class_lacks_assignment(world, admin):
    z = lacking(world)
    r = admin.post(CREATE, post_data(z, world.y, world.s))
    assert "La classe IV SENZA non ha un'assegnazione per Matematica." in text(r)
    assert ClassPairing.objects.count() == 0


def test_second_class_lacks_assignment(world, admin):
    z = lacking(world)
    r = admin.post(CREATE, post_data(world.x, z, world.s))
    assert "La classe IV SENZA non ha un'assegnazione per Matematica." in text(r)
    assert ClassPairing.objects.count() == 0


def test_both_classes_lack_two_messages(world, admin):
    z, w = lacking(world), klass(world.school, "V SENZA")
    t = text(admin.post(CREATE, post_data(z, w, world.s)))
    assert "La classe IV SENZA non ha" in t and "La classe V SENZA non ha" in t


@pytest.mark.parametrize("which", ["x", "y"])
def test_teacher_lacking_in_either_class(world, admin, which):
    other = teacher(world.school, "VERDI ANNA")
    assign(world.school, other, getattr(world, "y" if which == "x" else "x"), world.s)
    r = admin.post(CREATE, post_data(world.x, world.y, world.s, other))
    name = getattr(world, which).name
    assert f"VERDI ANNA non ha un'assegnazione per Matematica in {name}." in text(r)
    assert ClassPairing.objects.count() == 0


def test_exact_and_reversed_duplicate(world, admin):
    make_pairing(world.school, world.x, world.y, world.s, world.t)
    for a, b in ((world.x, world.y), (world.y, world.x)):
        for kwargs in ({}, HX):
            r = admin.post(CREATE, post_data(a, b, world.s, world.t), **kwargs)
            assert r.status_code == 200
            assert (
                "Esiste già un accoppiamento per queste classi, materia e docente."
                in text(r)
            )
    assert ClassPairing.objects.count() == 1


def test_duplicate_without_teacher(world, admin):
    make_pairing(world.school, world.x, world.y, world.s, None)
    r = admin.post(CREATE, post_data(world.y, world.x, world.s))
    assert "Esiste già un accoppiamento" in text(r)
    assert ClassPairing.objects.count() == 1


def test_two_teachers_and_teacherless_all_allowed(world, admin):
    other = teacher(world.school, "VERDI ANNA")
    assign(world.school, other, world.x, world.s)
    assign(world.school, other, world.y, world.s)
    for t in (world.t, other, None):
        r = admin.post(CREATE, post_data(world.x, world.y, world.s, t))
        assert r.status_code == 302
    assert ClassPairing.objects.count() == 3


def test_race_integrity_error_is_field_error(world, admin, monkeypatch):
    from school import forms

    make_pairing(world.school, world.x, world.y, world.s, world.t)
    monkeypatch.setattr(
        forms.PairingForm, "_duplicates", lambda *a: ClassPairing.objects.none()
    )
    r = admin.post(CREATE, post_data(world.x, world.y, world.s, world.t))
    assert r.status_code == 200 and "Esiste già un accoppiamento" in text(r)
    assert ClassPairing.objects.count() == 1


@pytest.mark.parametrize("field", ["class_a", "class_b", "subject", "teacher"])
def test_other_school_ids_rejected(world, admin, field):
    other = make_school("Altra")
    objs = {
        "class_a": klass(other, "ALTRA A"),
        "class_b": klass(other, "ALTRA B"),
        "subject": subj(other, "Altro"),
        "teacher": teacher(other, "ALTRO DOCENTE"),
    }
    data = post_data(world.x, world.y, world.s, world.t)
    data[field] = objs[field].pk
    r = admin.post(CREATE, data)
    assert r.status_code == 200 and "scelta non valida" in text(r)
    assert ClassPairing.objects.count() == 0


def test_inactive_teacher_not_accepted_on_create(world, admin):
    world.t.active = False
    world.t.save()
    r = admin.post(CREATE, post_data(world.x, world.y, world.s, world.t))
    assert "scelta non valida" in text(r)


def test_missing_choices_messages(world, admin):
    r = admin.post(CREATE, {})
    t = text(r)
    assert "Scegli una classe." in t and "Scegli la seconda classe." in t
    assert "Scegli una materia." in t


def test_error_aria(world, admin):
    z = lacking(world)
    html = admin.post(CREATE, post_data(z, world.y, world.s)).content.decode()
    assert 'aria-invalid="true"' in html
    err_id = re.search(r'id="(id_\w+-error)"', html).group(1)
    assert f'aria-describedby="{err_id}"' in html
    r = admin.post(CREATE, post_data(z, world.y, world.s), **HX)
    assert r["HX-Retarget"] == "#pairing-form-area" and r["HX-Reswap"] == "innerHTML"


def test_forged_school_ignored(world, admin):
    other = make_school("Altra")
    data = post_data(world.x, world.y, world.s)
    data.update(school=other.pk, id=999)
    admin.post(CREATE, data)
    p = ClassPairing.objects.get()
    assert p.school == world.school and p.pk != 999


# --- edit ---------------------------------------------------------------


def test_edit_prefilled_and_unchanged_accepted(world, admin):
    p = make_pairing(world.school, world.x, world.y, world.s, world.t, note="n")
    html = admin.get(edit(p), **HX).content.decode()
    assert "<html" not in html and "n</textarea>" in html
    for kwargs in ({}, HX):
        r = admin.post(
            edit(p), post_data(world.x, world.y, world.s, world.t, "n"), **kwargs
        )
        assert r.status_code in (200, 302)
        assert "Esiste già" not in text(r)
    assert "aggiornato." in text(
        admin.post(edit(p), post_data(world.y, world.x, world.s, world.t, "n"), **HX)
    )
    assert ClassPairing.objects.count() == 1


def test_edit_changes_note_and_teacher(world, admin):
    p = make_pairing(world.school, world.x, world.y, world.s)
    r = admin.post(
        edit(p), post_data(world.x, world.y, world.s, world.t, "nuova"), follow=True
    )
    assert r.redirect_chain == [(LIST, 302)]
    p.refresh_from_db()
    assert (p.teacher, p.note) == (world.t, "nuova")


def test_edit_subject_class_lacks(world, admin):
    p = make_pairing(world.school, world.x, world.y, world.s)
    other = subj(world.school, "Storia")
    assign(world.school, world.t, world.x, other)
    r = admin.post(edit(p), post_data(world.x, world.y, other), **HX)
    assert "La classe III ELETTRICISTI non ha un'assegnazione per Storia." in text(r)
    p.refresh_from_db()
    assert p.subject == world.s


def test_edit_into_duplicate_refused(world, admin):
    other = subj(world.school, "Storia")
    assign(world.school, world.t, world.x, other)
    assign(world.school, world.t, world.y, other)
    make_pairing(world.school, world.x, world.y, other)
    p = make_pairing(world.school, world.x, world.y, world.s)
    r = admin.post(edit(p), post_data(world.x, world.y, other))
    assert "Esiste già un accoppiamento" in text(r)
    p.refresh_from_db()
    assert p.subject == world.s


def test_edit_inactive_teacher_still_selectable(world, admin):
    p = make_pairing(world.school, world.x, world.y, world.s, world.t)
    world.t.active = False
    world.t.save()
    html = admin.get(edit(p)).content.decode()
    assert re.search(rf'<option value="{world.t.pk}" selected>ROSSI MARIO', html)
    r = admin.post(edit(p), post_data(world.x, world.y, world.s, world.t, "ok"))
    assert r.status_code == 302


def test_edit_delete_404(world, admin):
    other = make_school("Altra")
    a, b, s = klass(other, "SEGRETO A"), klass(other, "SEGRETO B"), subj(other)
    o = make_pairing(other, a, b, s)
    data = post_data(world.x, world.y, world.s)
    for url in (edit(o), delete(o), "/dati-scuola/accoppiamenti/99999/modifica/"):
        for r in (admin.get(url), admin.post(url, data)):
            assert r.status_code == 404 and "SEGRETO" not in r.content.decode()
    assert admin.get("/dati-scuola/accoppiamenti/99999/elimina/").status_code == 404
    assert ClassPairing.objects.count() == 1


# --- delete -------------------------------------------------------------


def test_delete_confirm_then_post(world, admin):
    p = make_pairing(world.school, world.x, world.y, world.s, world.t)
    html = text(admin.get(delete(p)))
    assert (
        "Eliminare l'accoppiamento II ELETTRICISTI + III ELETTRICISTI - Matematica?"
        in html
    )
    assert "Conferma" in html and ClassPairing.objects.count() == 1
    r = admin.get(delete(p), **HX)
    assert (
        "<html" not in r.content.decode() and r["HX-Retarget"] == "#pairing-form-area"
    )
    r = admin.post(delete(p), **HX)
    assert (
        "Accoppiamento II ELETTRICISTI + III ELETTRICISTI - Matematica eliminato."
        in text(r)
    )
    assert "Nessun accoppiamento presente." in text(r)
    assert ClassPairing.objects.count() == 0
    assert Assignment.objects.count() == 2


def test_delete_plain_redirects(world, admin):
    p = make_pairing(world.school, world.x, world.y, world.s)
    r = admin.post(delete(p), follow=True)
    assert r.redirect_chain == [(LIST, 302)]
    assert "eliminato." in text(r)
    assert Assignment.objects.count() == 2


# --- escaping -----------------------------------------------------------


def test_note_and_names_escaped(world, admin):
    bad = "<script>alert(1)</script>"
    p = make_pairing(world.school, world.x, world.y, world.s, world.t, note=bad)
    for url in (LIST, edit(p), delete(p)):
        assert "<script>alert" not in admin.get(url).content.decode()
    r = admin.post(edit(p), post_data(world.x, world.y, world.s, world.t, bad), **HX)
    assert "<script>alert" not in r.content.decode()
    assert "&lt;script&gt;" in r.content.decode()
    c = klass(world.school, bad)
    assign(world.school, world.t, c, world.s)
    q = make_pairing(world.school, world.x, c, world.s)
    r = admin.post(delete(q), **HX)
    assert "<script>alert" not in r.content.decode()
    assert "&lt;script&gt;" in r.content.decode()


def test_no_role_comparison():
    for path in [
        *pathlib.Path("templates/school/pairings").glob("*.html"),
        pathlib.Path("school/views/pairings.py"),
    ]:
        text_ = path.read_text()
        assert "user.role" not in text_ and "Role." not in text_, path

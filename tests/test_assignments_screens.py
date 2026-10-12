import datetime
import html as htmllib
import re
from unittest import mock

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from school.models import Assignment, Class, ClassPairing, School, Subject, Teacher

PW = "correct-horse"
LIST = "/dati-scuola/assegnazioni/"
CREATE = "/dati-scuola/assegnazioni/nuova/"
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


def klass(school, name="II ELETTRICISTI"):
    return Class.objects.create(school=school, name=name)


def subj(school, name="Matematica"):
    return Subject.objects.create(
        school=school, name=name, kind="TEORIA", cognitive_weight="ALTO"
    )


def assign(school, t=None, c=None, s=None, hours=40):
    return Assignment.objects.create(
        school=school,
        teacher=t or teacher(school),
        school_class=c or klass(school),
        subject=s or subj(school),
        ore_totali=hours,
    )


def pairing(school, a, b, s, t=None):
    a, b = sorted((a, b), key=lambda c: c.pk)
    return ClassPairing.objects.create(
        school=school, class_a=a, class_b=b, subject=s, teacher=t
    )


def edit(a):
    return f"/dati-scuola/assegnazioni/{a.pk}/modifica/"


def delete(a):
    return f"/dati-scuola/assegnazioni/{a.pk}/elimina/"


def valid(sch, **kw):
    data = {
        "teacher": Teacher.objects.get_or_create(
            school=sch, name="BIANCHI LUCA", defaults={"type": "ASSUNTO"}
        )[0].pk,
        "school_class": Class.objects.get_or_create(school=sch, name="III MECCANICI")[
            0
        ].pk,
        "subject": Subject.objects.get_or_create(
            school=sch,
            name="Italiano",
            defaults={"kind": "TEORIA", "cognitive_weight": "ALTO"},
        )[0].pk,
        "ore_totali": "30",
    }
    data.update(kw)
    return data


def text(r):
    return htmllib.unescape(r.content.decode())


# --- list ---------------------------------------------------------------


def test_list_both_roles_with_hours(school, admin, seg):
    assign(school, hours=40)
    for client in (admin, seg):
        r = client.get(LIST)
        assert r.status_code == 200
        row = re.findall(r"<tr>.*?</tr>", r.content.decode(), re.S)[1]
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)
        assert [c.strip() for c in cells[:6]] == [
            "ROSSI MARIO",
            "II ELETTRICISTI",
            "Matematica",
            "40",
            "0",
            "40",
        ]


def test_negative_remaining_shown(school, admin):
    a = assign(school, hours=40)
    with mock.patch("school.selectors.delivered_hours_map", return_value={a.pk: 45}):
        row = re.findall(r"<tr>.*?</tr>", admin.get(LIST).content.decode(), re.S)[1]
    cells = [c.strip() for c in re.findall(r"<td>(.*?)</td>", row, re.S)]
    assert cells[-3:] == ["40", "45", "-5"]


def test_ordering_case_insensitive(school, admin):
    t = teacher(school, "bianchi luca")
    s = subj(school)
    assign(school, teacher(school, "ZETA ANNA"), klass(school, "A"), s)
    assign(school, t, klass(school, "b"), s)
    assign(school, t, klass(school, "C"), s)
    html = admin.get(LIST).content.decode()
    assert html.index(">b<") < html.index(">C<") < html.index("ZETA")


def test_secretary_sees_no_write_controls(school, seg):
    a = assign(school)
    html = seg.get(LIST).content.decode()
    assert "Aggiungi assegnazione" not in html
    assert "Modifica" not in html and "Elimina" not in html
    assert edit(a) not in html and delete(a) not in html
    assert "Dati scuola" not in html


def test_admin_sees_controls_and_labels(school, admin):
    a = assign(school)
    html = admin.get(LIST).content.decode()
    assert "Aggiungi assegnazione" in html
    assert edit(a) in html and delete(a) in html
    name = "ROSSI MARIO - II ELETTRICISTI - Matematica"
    assert f'aria-label="Modifica assegnazione {name}"' in html
    assert f'aria-label="Elimina assegnazione {name}"' in html
    assert 'href="/dati-scuola/"' in html


def test_empty_state(school, admin, seg):
    assert "Nessuna assegnazione presente." in seg.get(LIST).content.decode()
    html = admin.get(LIST).content.decode()
    assert "Nessuna assegnazione presente." in html and "Aggiungi assegnazione" in html


def test_no_school(db, admin, seg):
    for client in (admin, seg):
        html = client.get(LIST).content.decode()
        assert "Nessuna scuola configurata: esegui prima il setup." in html
        assert "Aggiungi assegnazione" not in html
    for method in (admin.get, admin.post):
        r = method(CREATE, {"ore_totali": "3"})
        assert r.status_code == 200
        assert "Nessuna scuola configurata" in r.content.decode()
    a = Assignment(pk=1)
    assert "Nessuna scuola" in admin.get(edit(a)).content.decode()
    assert "Nessuna scuola" in admin.post(delete(a)).content.decode()
    assert Assignment.objects.count() == 0


def test_other_school_hidden(school, admin):
    other = make_school("Altra")
    assign(other, teacher(other, "SEGRETO UNO"))
    assert "SEGRETO" not in admin.get(LIST).content.decode()


def test_list_query_count_constant(school, admin):
    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert admin.get(LIST).status_code == 200
        return len(ctx)

    t, s = teacher(school), subj(school)
    for i in range(3):
        assign(school, t, klass(school, f"C{i}"), s)
    three = count()
    for i in range(3, 30):
        assign(school, t, klass(school, f"C{i}"), s)
    assert count() == three


# --- hub / authorization ------------------------------------------------


def test_hub_links(admin, seg, school):
    html = admin.get("/dati-scuola/").content.decode()
    assert f'href="{LIST}"' in html and ">Assegnazioni<" in html
    assert 'href="/dati-scuola/accoppiamenti/"' in html and ">Accoppiamenti<" in html
    assert admin.get(LIST).status_code == 200
    assert admin.get("/dati-scuola/accoppiamenti/").status_code == 200
    assert seg.get("/dati-scuola/").status_code == 403


def test_anonymous_redirected_everywhere(school, client):
    a = assign(school)
    for url in [LIST, CREATE, edit(a), delete(a)]:
        for method in (client.get, client.post):
            r = method(url, {"ore_totali": "1"})
            assert r.status_code == 302 and r.url.startswith("/login/")
    assert Assignment.objects.count() == 1


def test_secretary_denied_writes(school, seg):
    a = assign(school)
    for url in (CREATE, edit(a), delete(a)):
        assert seg.get(url).status_code == 403
        assert seg.post(url, valid(school, ore_totali="1")).status_code == 403
    a.refresh_from_db()
    assert a.ore_totali == 40 and Assignment.objects.count() == 1


def test_no_role_comparison_in_new_code():
    import pathlib

    paths = [
        *pathlib.Path("templates/school/assignments").glob("*.html"),
        *pathlib.Path("templates/school/pairings").glob("*.html"),
        pathlib.Path("school/views/assignments.py"),
        pathlib.Path("school/views/pairings.py"),
        pathlib.Path("school/forms.py"),
        pathlib.Path("school/assignment_hours.py"),
    ]
    for path in paths:
        text_ = path.read_text()
        assert "user.role" not in text_ and "Role." not in text_, path


def test_csrf_enforced(school):
    c = login("ADMIN", enforce_csrf_checks=True)
    a = assign(school)
    for url in (CREATE, edit(a), delete(a)):
        assert c.post(url, valid(school)).status_code == 403
    assert Assignment.objects.get().ore_totali == 40


# --- create -------------------------------------------------------------


def test_create_htmx_partial_with_message(school, admin):
    r = admin.post(CREATE, valid(school), **HX)
    assert r.status_code == 200
    html = text(r)
    assert "<html" not in html
    assert "Assegnazione BIANCHI LUCA - III MECCANICI - Italiano aggiunta." in html
    assert 'id="assignment-section"' in html
    assert Assignment.objects.get().school == school


def test_create_plain_redirects_with_message(school, admin):
    r = admin.post(CREATE, valid(school), follow=True)
    assert r.redirect_chain == [(LIST, 302)]
    assert text(r).count("Assegnazione BIANCHI LUCA") >= 1
    assert "aggiunta." in text(r)


def test_create_get_partial_and_full(school, admin):
    teacher(school, "ATTIVO UNO")
    teacher(school, "INATTIVO DUE", active=False)
    html = admin.get(CREATE, **HX).content.decode()
    assert "<html" not in html and "<form" in html
    assert "ATTIVO UNO" in html and "INATTIVO" not in html
    full = admin.get(CREATE).content.decode()
    assert "<html" in full and "csrfmiddlewaretoken" in full
    assert 'aria-live="polite"' in admin.get(LIST).content.decode()


def test_hours_zero_accepted(school, admin):
    admin.post(CREATE, valid(school, ore_totali="0"))
    assert Assignment.objects.get().ore_totali == 0


@pytest.mark.parametrize(
    "key,value,msg",
    [
        ("teacher", "", "Scegli un docente."),
        ("school_class", "", "Scegli una classe."),
        ("subject", "", "Scegli una materia."),
        ("ore_totali", "", "Inserisci le ore totali."),
        ("ore_totali", "-1", "non possono essere negative"),
        ("ore_totali", "abc", "Inserisci un numero intero."),
        ("ore_totali", "10000", "non possono superare 9999"),
        ("ore_totali", "1.5", "Inserisci un numero intero."),
    ],
)
def test_create_validation(school, admin, key, value, msg):
    data = valid(school, **{key: value})
    for kwargs in ({}, HX):
        r = admin.post(CREATE, data, **kwargs)
        assert r.status_code == 200
        html = r.content.decode()
        assert msg in html and "<form" in html
        assert 'aria-invalid="true"' in html
        err_id = re.search(r'id="(id_\w+-error)"', html).group(1)
        assert err_id in re.search(r'aria-describedby="([^"]*)"', html).group(1)
        if kwargs:
            assert r["HX-Retarget"] == "#assignment-form-area"
            assert r["HX-Reswap"] == "innerHTML"
    assert Assignment.objects.count() == 0


def test_max_hours_accepted(school, admin):
    admin.post(CREATE, valid(school, ore_totali="9999"))
    assert Assignment.objects.get().ore_totali == 9999


def test_duplicate_names_the_triple(school, admin):
    a = assign(school)
    data = {
        "teacher": a.teacher_id,
        "school_class": a.school_class_id,
        "subject": a.subject_id,
        "ore_totali": "5",
    }
    r = admin.post(CREATE, data, **HX)
    assert (
        "Esiste già un'assegnazione di ROSSI MARIO per II ELETTRICISTI, Matematica."
        in text(r)
    )
    assert Assignment.objects.count() == 1


def test_race_integrity_error_is_field_error(school, admin, monkeypatch):
    from school import forms

    a = assign(school)
    monkeypatch.setattr(forms.AssignmentForm, "clean", lambda self: self.cleaned_data)
    data = {
        "teacher": a.teacher_id,
        "school_class": a.school_class_id,
        "subject": a.subject_id,
        "ore_totali": "5",
    }
    r = admin.post(CREATE, data)
    assert r.status_code == 200
    assert "Esiste già un'assegnazione" in text(r)
    assert Assignment.objects.count() == 1


def test_other_school_ids_rejected_and_not_offered(school, admin):
    other = make_school("Altra")
    ot, oc, os_ = (
        teacher(other, "ALTRO DOCENTE"),
        klass(other, "ALTRA"),
        subj(other, "Altro"),
    )
    html = admin.get(CREATE).content.decode()
    assert "ALTRO DOCENTE" not in html and "ALTRA" not in html
    for key, obj in (("teacher", ot), ("school_class", oc), ("subject", os_)):
        r = admin.post(CREATE, valid(school, **{key: obj.pk}))
        assert "scelta non valida" in text(r)
    assert Assignment.objects.count() == 0


def test_inactive_teacher_rejected(school, admin):
    t = teacher(school, "INATTIVO DUE", active=False)
    r = admin.post(CREATE, valid(school, teacher=t.pk))
    assert "scelta non valida" in text(r)
    assert Assignment.objects.count() == 0


def test_forged_school_ignored(school, admin):
    other = make_school("Altra")
    admin.post(CREATE, valid(school, school=other.pk, id=999))
    a = Assignment.objects.get()
    assert a.school == school and a.pk != 999


# --- edit ---------------------------------------------------------------


def test_edit_form_has_only_hours(school, admin):
    a = assign(school)
    html = admin.get(edit(a), **HX).content.decode()
    assert "<html" not in html
    assert re.findall(r'name="(\w+)"', html) == ["csrfmiddlewaretoken", "ore_totali"]
    assert "ROSSI MARIO" in html and 'value="40"' in html


def test_edit_updates_list_and_remaining(school, admin):
    a = assign(school)
    r = admin.post(edit(a), {"ore_totali": "55"}, **HX)
    assert "aggiornata." in text(r) and "<html" not in text(r)
    row = re.findall(r"<tr>.*?</tr>", r.content.decode(), re.S)[1]
    assert [c.strip() for c in re.findall(r"<td>(.*?)</td>", row, re.S)][-3:] == [
        "55",
        "0",
        "55",
    ]
    a.refresh_from_db()
    assert a.ore_totali == 55


def test_edit_plain_redirects(school, admin):
    a = assign(school)
    r = admin.post(edit(a), {"ore_totali": "7"}, follow=True)
    assert r.redirect_chain == [(LIST, 302)]
    assert "aggiornata." in text(r)


def test_edit_ignores_posted_identity(school, admin):
    a = assign(school)
    other = valid(school, ore_totali="12")
    admin.post(edit(a), other)
    b = Assignment.objects.get(pk=a.pk)
    assert (b.teacher_id, b.school_class_id, b.subject_id) == (
        a.teacher_id,
        a.school_class_id,
        a.subject_id,
    )
    assert b.ore_totali == 12


@pytest.mark.parametrize("value", ["", "-1", "x", "10000"])
def test_edit_validation(school, admin, value):
    a = assign(school)
    for kwargs in ({}, HX):
        r = admin.post(edit(a), {"ore_totali": value}, **kwargs)
        assert r.status_code == 200 and 'aria-invalid="true"' in r.content.decode()
        if kwargs:
            assert r["HX-Retarget"] == "#assignment-form-area"
    a.refresh_from_db()
    assert a.ore_totali == 40


def test_edit_calls_setter_with_assignment_and_int(school, admin):
    a = assign(school)
    with mock.patch("school.views.assignments.set_assignment_total_hours") as m:
        admin.post(edit(a), {"ore_totali": "21"})
    assert m.call_count == 1
    (arg, hours), _ = m.call_args
    assert arg.pk == a.pk and hours == 21 and isinstance(hours, int)


def test_edit_inactive_teacher(school, admin):
    a = assign(school, teacher(school, "INATTIVO DUE", active=False))
    assert "INATTIVO DUE" in admin.get(LIST).content.decode()
    admin.post(edit(a), {"ore_totali": "9"})
    a.refresh_from_db()
    assert a.ore_totali == 9


def test_ore_totali_written_only_by_the_setter():
    import pathlib

    for path in pathlib.Path("school").rglob("*.py"):
        if "migrations" in path.parts or path.name == "assignment_hours.py":
            continue
        assert not re.search(r"\.ore_totali\s*=[^=]", path.read_text()), path


def test_edit_delete_404(school, admin):
    other = make_school("Altra")
    o = assign(other, teacher(other, "SEGRETO UNO"))
    for url in (edit(o), delete(o), "/dati-scuola/assegnazioni/99999/modifica/"):
        for r in (admin.get(url), admin.post(url, {"ore_totali": "1"})):
            assert r.status_code == 404
            assert "SEGRETO" not in r.content.decode()
    assert admin.get("/dati-scuola/assegnazioni/99999/elimina/").status_code == 404
    assert Assignment.objects.get().ore_totali == 40


# --- delete -------------------------------------------------------------


def test_delete_confirm_then_post_htmx(school, admin):
    a = assign(school)
    html = text(admin.get(delete(a)))
    assert (
        "Eliminare l'assegnazione ROSSI MARIO - II ELETTRICISTI - Matematica?" in html
    )
    assert "L'operazione non si può annullare." in html and "Conferma" in html
    assert Assignment.objects.count() == 1
    r = admin.post(delete(a), **HX)
    assert "Assegnazione ROSSI MARIO - II ELETTRICISTI - Matematica eliminata." in text(
        r
    )
    assert "Nessuna assegnazione presente." in text(r)
    assert Assignment.objects.count() == 0


def test_delete_plain_redirects(school, admin):
    a = assign(school)
    r = admin.post(delete(a), follow=True)
    assert r.redirect_chain == [(LIST, 302)]
    assert "eliminata." in text(r)


def test_delete_htmx_get_is_partial(school, admin):
    a = assign(school)
    r = admin.get(delete(a), **HX)
    assert (
        "<html" not in r.content.decode()
        and r["HX-Retarget"] == "#assignment-form-area"
    )


def test_delete_blocked_by_pairing_with_teacher(school, admin):
    t, s = teacher(school), subj(school)
    c1, c2 = klass(school, "II ELETTRICISTI"), klass(school, "III ELETTRICISTI")
    a = assign(school, t, c1, s)
    assign(school, t, c2, s)
    pairing(school, c1, c2, s, t)
    expected = (
        "è usata dall'accoppiamento II ELETTRICISTI + III ELETTRICISTI - Matematica."
    )
    html = text(admin.get(delete(a)))
    assert expected in html and "Elimina prima l'accoppiamento." in html
    assert "Conferma" not in html and "Impossibile eliminare" in html
    r = admin.post(delete(a), **HX)
    assert "Impossibile eliminare" in text(r) and "II ELETTRICISTI + III" in text(r)
    assert Assignment.objects.filter(pk=a.pk).exists()
    r = admin.post(delete(a))
    assert r.status_code == 302 and Assignment.objects.filter(pk=a.pk).exists()


def test_delete_blocked_lists_every_pairing(school, admin):
    t, s = teacher(school), subj(school)
    c1, c2, c3 = klass(school, "A1"), klass(school, "B2"), klass(school, "C3")
    a = assign(school, t, c1, s)
    for c in (c2, c3):
        assign(school, t, c, s)
    pairing(school, c1, c2, s, t)
    pairing(school, c1, c3, s, t)
    html = text(admin.get(delete(a)))
    assert "dagli accoppiamenti" in html and "Elimina prima gli accoppiamenti." in html
    assert "A1 + B2 - Matematica" in html and "A1 + C3 - Matematica" in html


def test_delete_blocked_when_last_for_teacherless_pairing(school, admin):
    t, s = teacher(school), subj(school)
    c1, c2 = klass(school, "A1"), klass(school, "B2")
    a = assign(school, t, c1, s)
    assign(school, t, c2, s)
    pairing(school, c1, c2, s, None)
    assert "Conferma" not in text(admin.get(delete(a)))
    admin.post(delete(a))
    assert Assignment.objects.filter(pk=a.pk).exists()


def test_delete_allowed_when_second_teacher_covers(school, admin):
    t, s = teacher(school), subj(school)
    c1, c2 = klass(school, "A1"), klass(school, "B2")
    a = assign(school, t, c1, s)
    assign(school, teacher(school, "VERDI ANNA"), c1, s)
    assign(school, t, c2, s)
    pairing(school, c1, c2, s, None)
    assert "Conferma" in text(admin.get(delete(a)))
    admin.post(delete(a))
    assert not Assignment.objects.filter(pk=a.pk).exists()
    assert ClassPairing.objects.count() == 1


def test_delete_allowed_for_pairing_of_other_subject(school, admin):
    t, s = teacher(school), subj(school)
    c1, c2 = klass(school, "A1"), klass(school, "B2")
    a = assign(school, t, c1, s)
    other = subj(school, "Storia")
    assign(school, t, c1, other)
    assign(school, t, c2, other)
    pairing(school, c1, c2, other, t)
    admin.post(delete(a))
    assert not Assignment.objects.filter(pk=a.pk).exists()


def test_delete_protected_error_is_reported(school, admin):
    from django.db.models import ProtectedError

    a = assign(school)
    with mock.patch.object(
        Assignment, "delete", side_effect=ProtectedError("x", set())
    ):
        r = admin.post(delete(a), **HX)
    assert "Impossibile eliminare" in text(r)
    assert Assignment.objects.filter(pk=a.pk).exists()


# --- escaping -----------------------------------------------------------


def test_names_are_escaped_everywhere(school, admin):
    bad = "<script>alert(1)</script>"
    t = teacher(school, bad + " X")
    a = assign(school, t)
    for url in (LIST, CREATE, edit(a), delete(a)):
        assert "<script>alert" not in admin.get(url).content.decode()
    r = admin.post(edit(a), {"ore_totali": "3"}, **HX)
    assert "<script>alert" not in r.content.decode()
    assert "&lt;script&gt;" in r.content.decode()
    r = admin.post(delete(a), **HX)
    assert "<script>alert" not in r.content.decode()
    assert "&lt;script&gt;" in r.content.decode()


# --- delivered hours correction (#37) --------------------------------------


def adjust_url(a):
    return f"/dati-scuola/assegnazioni/{a.pk}/correggi-ore/"


def history_url(a):
    return f"/dati-scuola/assegnazioni/{a.pk}/storico-ore/"


def test_admin_sees_adjust_action_secretary_does_not(school, admin, seg):
    a = assign(school)
    assert "Correggi ore erogate" in text(admin.get(LIST))
    assert "Correggi ore erogate" not in text(seg.get(LIST))
    assert seg.post(adjust_url(a), {"nuove_ore": "3", "motivo": "x"}).status_code == 403
    assert seg.get(adjust_url(a)).status_code == 403
    assert Client().post(adjust_url(a), {}).status_code == 302


def test_adjust_form_shows_current_value(school, admin):
    a = assign(school)
    r = admin.get(adjust_url(a), **HX)
    assert r.status_code == 200 and "Ore erogate attuali" in text(r)


def test_adjust_saves_and_row_shows_values(school, admin):
    from ledger.models import LedgerEntry

    a = assign(school, hours=10)
    r = admin.post(adjust_url(a), {"nuove_ore": "4", "motivo": "recupero"}, **HX)
    assert r.status_code == 200
    entry = LedgerEntry.objects.get()
    assert entry.hours == 4 and entry.reason == "recupero" and entry.created_by
    row = re.search(r"<td>10</td>\s*<td>4</td>\s*<td>6</td>", r.content.decode())
    assert row


def test_adjust_unchanged_and_errors_write_nothing(school, admin):
    from ledger.models import LedgerEntry

    a = assign(school)
    assert (
        admin.post(adjust_url(a), {"nuove_ore": "0", "motivo": ""}).status_code == 302
    )
    r = admin.post(adjust_url(a), {"nuove_ore": "3", "motivo": "  "})
    assert r.status_code == 200 and "Indica il motivo" in text(r)
    r = admin.post(adjust_url(a), {"nuove_ore": "abc", "motivo": "x"})
    assert "numero intero" in text(r)
    r = admin.post(adjust_url(a), {"nuove_ore": "-2", "motivo": "x"})
    assert "negative" in text(r)
    assert not LedgerEntry.objects.exists()


def test_adjust_above_total_is_accepted(school, admin):
    a = assign(school, hours=5)
    admin.post(adjust_url(a), {"nuove_ore": "9", "motivo": "extra"})
    r = admin.get(LIST)
    assert re.search(r"<td>5</td>\s*<td>9</td>\s*<td>-4</td>", r.content.decode())


def test_history_lists_entries_newest_first(school, admin):
    a = assign(school)
    admin.post(adjust_url(a), {"nuove_ore": "4", "motivo": "primo"})
    admin.post(adjust_url(a), {"nuove_ore": "1", "motivo": "secondo"})
    body = text(admin.get(history_url(a), **HX))
    assert body.index("secondo") < body.index("primo")
    assert "Correzione" in body and "admin@example.com" in body


def test_history_query_count_is_constant(school, admin):
    def count(n):
        a = assign(
            school,
            t=teacher(school, f"T{n}"),
            c=klass(school, f"C{n}"),
            s=subj(school, f"M{n}"),
        )
        for i in range(n):
            admin.post(adjust_url(a), {"nuove_ore": str(i + 1), "motivo": "m"})
        with CaptureQueriesContext(connection) as ctx:
            admin.get(history_url(a), **HX)
        return len(ctx)

    assert count(1) == count(5)


def test_list_query_count_constant_with_adjustments(school, admin):
    def count():
        with CaptureQueriesContext(connection) as ctx:
            admin.get(LIST)
        return len(ctx)

    for i in range(3):
        assign(
            school,
            t=teacher(school, f"A{i}"),
            c=klass(school, f"A{i}"),
            s=subj(school, f"A{i}"),
        )
    small = count()
    for i in range(27):
        assign(
            school,
            t=teacher(school, f"B{i}"),
            c=klass(school, f"B{i}"),
            s=subj(school, f"B{i}"),
        )
    assert count() == small

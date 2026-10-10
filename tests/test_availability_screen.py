import datetime
import json

import pytest
from django.test import Client

from accounts.models import User
from availability import grid as g
from availability.models import Availability
from school.models import School, Teacher

PW = "correct-horse"
D = datetime.date
W1, W2, W3 = D(2026, 9, 7), D(2026, 9, 14), D(2026, 9, 21)
HX = {"HTTP_HX_REQUEST": "true"}
PAGE = "/disponibilita/"


def url(kind, week, tid=None):
    base = f"/disponibilita/{week:%Y-%m-%d}/"
    return base + ("copia/" if kind == "copy" else f"docente/{tid}/{kind}/")


def make_school(name="Scuola"):
    return School.objects.create(
        name=name,
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )


def login(role):
    u = User.objects.create_user(f"{role.lower()}@example.com", PW, role)
    c = Client()
    c.force_login(u)
    return c


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr("availability.views.today", lambda: D(2026, 9, 16))


@pytest.fixture
def school(db):
    return make_school()


@pytest.fixture
def admin(db):
    return login("ADMIN")


@pytest.fixture
def seg(db):
    return login("SEGRETERIA")


def teacher(school, name="ROSSI", **kw):
    kw.setdefault("type", "CONTRATTO")
    kw.setdefault("first_time", False)
    return Teacher.objects.create(school=school, name=name, **kw)


def grid_with(off=()):
    grid = g.default_grid()
    for day, i in off:
        grid[day][i]["disponibile"] = False
    return grid


def store(t, week, grid=None):
    return Availability.objects.create(
        school=t.school, teacher=t, week_start=week, slots=grid or grid_with()
    )


def toggle(c, t, week, day="lunedi", slot=0, **kw):
    return c.post(url("toggle", week, t.pk), {"day": day, "slot": slot}, **HX, **kw)


# access


def test_access_both_roles_and_anonymous(admin, seg, client, school):
    assert admin.get(PAGE).status_code == 200
    assert seg.get(PAGE).status_code == 200
    r = client.get(PAGE)
    assert r.status_code == 302 and r.url.startswith("/login/")


def test_menu_entry_visible_to_both(admin, seg, school):
    for c in (admin, seg):
        assert 'href="/disponibilita/"' in c.get("/").content.decode()


def test_writes_require_login_and_work_for_secretary(seg, client, school):
    t = teacher(school)
    assert toggle(seg, t, W1).status_code == 200
    r = client.post(url("toggle", W1, t.pk), {"day": "lunedi", "slot": 0})
    assert r.status_code == 302
    assert client.post(url("copy", W2)).status_code == 302


def test_no_school_does_not_error(admin, db):
    assert admin.get(PAGE).status_code == 200


# week selector


def test_opens_on_current_week(admin, school):
    html = admin.get(PAGE).content.decode()
    assert '<option value="2026-09-14" selected>' in html
    assert "Settimana 2: 14/09/2026 - 18/09/2026" in html


def test_opens_on_first_week_outside_school_year(admin, school, monkeypatch):
    monkeypatch.setattr("availability.views.today", lambda: D(2026, 7, 1))
    html = admin.get(PAGE).content.decode()
    assert '<option value="2026-09-07" selected>' in html


def test_week_selector_htmx_returns_section_only(admin, school):
    r = admin.get(PAGE + "?settimana=2026-09-21", **HX)
    html = r.content.decode()
    assert "<html" not in html
    assert '<option value="2026-09-21" selected>' in html


# grid display


def test_lists_active_teachers_with_30_cells(admin, school):
    teacher(school, "ROSSI")
    teacher(school, "BIANCHI", active=False)
    html = admin.get(PAGE).content.decode()
    assert "ROSSI" in html and "BIANCHI" not in html
    assert html.count('class="av-cell av-on"') == 30
    assert html.count("av-cell") >= 30


def test_unavailable_cell_is_different(admin, school):
    t = teacher(school)
    store(t, W2, grid_with([("martedi", 2)]))
    html = admin.get(PAGE).content.decode()
    assert html.count('class="av-cell av-off"') == 1
    assert html.count('class="av-cell av-on"') == 29


def test_labels_stored_inherited_default(admin, school):
    stored, inh, dflt = (
        teacher(school, "A"),
        teacher(school, "B"),
        teacher(school, "C", first_time=True),
    )
    store(stored, W2)
    store(inh, W1, grid_with([("lunedi", 0)]))
    store(dflt, W1, grid_with([("lunedi", 0)]))
    html = admin.get(PAGE).content.decode()
    assert "Salvata per questa settimana" in html
    assert "Copiata dalla settimana del 07/09/2026" in html
    assert "Predefinita (tutto disponibile)" in html
    # inherited grid shows its off cell, first-year default does not
    assert html.count('class="av-cell av-off"') == 1


def test_display_does_not_write(admin, school):
    teacher(school)
    admin.get(PAGE)
    assert Availability.objects.count() == 0


def test_assunto_note_and_editable(admin, school):
    t = teacher(school, type="ASSUNTO")
    html = admin.get(PAGE).content.decode()
    assert "sempre disponibile" in html
    assert "vincola solo i docenti a contratto" in html
    assert toggle(admin, t, W2).status_code == 200
    assert (
        Availability.objects.get(teacher=t).slots["lunedi"][0]["disponibile"] is False
    )


# toggle / save


def test_toggle_creates_record_from_displayed_grid(admin, school):
    t = teacher(school)
    store(t, W1, grid_with([("venerdi", 5)]))
    r = toggle(admin, t, W2, "lunedi", 1)
    assert r.status_code == 200
    a = Availability.objects.get(teacher=t, week_start=W2)
    assert g.grid_errors(a.slots) == []
    assert a.slots["lunedi"][1]["disponibile"] is False
    assert a.slots["venerdi"][5]["disponibile"] is False  # inherited value kept
    assert a.slots["lunedi"][0]["disponibile"] is True
    assert Availability.objects.filter(teacher=t).count() == 2


def test_toggle_twice_restores_and_persists_after_reload(admin, school):
    t = teacher(school)
    toggle(admin, t, W2)
    html = admin.get(PAGE).content.decode()
    assert html.count('class="av-cell av-off"') == 1
    toggle(admin, t, W2)
    html = admin.get(PAGE).content.decode()
    assert html.count('class="av-cell av-off"') == 0


def test_toggle_response_is_card_plus_oob_status(admin, school):
    t = teacher(school)
    html = toggle(admin, t, W2).content.decode()
    assert f'id="teacher-card-{t.pk}"' in html
    assert 'id="availability-status"' in html and 'hx-swap-oob="true"' in html
    assert "<html" not in html


def test_save_whole_grid(admin, school):
    t = teacher(school)
    grid = grid_with([("giovedi", 3)])
    r = admin.post(url("salva", W2, t.pk), {"grid": json.dumps(grid)}, **HX)
    assert r.status_code == 200
    assert Availability.objects.get(teacher=t).slots == grid


# completion


def test_completion_updates_after_save(admin, school):
    a, b = teacher(school, "A"), teacher(school, "B")
    html = admin.get(PAGE).content.decode()
    assert "0.0%" in html and "0 di 2 docenti compilati" in html
    assert "<li>A</li>" in html and "<li>B</li>" in html
    html = toggle(admin, a, W2).content.decode()
    assert "50.0%" in html and "1 di 2 docenti compilati" in html
    assert "<li>B</li>" in html and "<li>A</li>" not in html
    html = toggle(admin, b, W2).content.decode()
    assert "100.0%" in html and "2 di 2" in html


def test_completion_with_zero_teachers(admin, school):
    r = admin.get(PAGE)
    assert r.status_code == 200
    assert "0 di 0 docenti compilati" in r.content.decode()


# copy


def copy(c, week):
    return c.post(url("copy", week), **HX)


def test_copy_skips_filled_and_reports(admin, school):
    a, b, c = (teacher(school, n) for n in "ABC")
    store(a, W1, grid_with([("lunedi", 0)]))
    store(b, W1, grid_with([("lunedi", 1)]))
    store(b, W2, grid_with([("lunedi", 2)]))
    r = copy(admin, W2)
    assert r.status_code == 200
    assert "Copiati 1 docenti, saltati 1" in r.content.decode()
    assert Availability.objects.get(teacher=a, week_start=W2).slots == grid_with(
        [("lunedi", 0)]
    )
    assert Availability.objects.get(teacher=b, week_start=W2).slots == grid_with(
        [("lunedi", 2)]
    )
    assert not Availability.objects.filter(teacher=c, week_start=W2).exists()


def test_copy_twice_copies_zero_second_time(admin, school):
    a = teacher(school)
    store(a, W1)
    assert "Copiati 1 docenti" in copy(admin, W2).content.decode()
    assert "Copiati 0 docenti, saltati 1" in copy(admin, W2).content.decode()
    assert Availability.objects.count() == 2


def test_copy_first_week_changes_nothing(admin, seg, school):
    a = teacher(school)
    store(a, W1)
    html = admin.get(PAGE + "?settimana=2026-09-07").content.decode()
    assert "disabled" in html
    r = copy(seg, W1)
    assert r.status_code == 200
    assert "Nessuna settimana precedente" in r.content.decode()
    assert Availability.objects.count() == 1


def test_copy_previous_week_without_records(admin, school):
    teacher(school)
    r = copy(admin, W2)
    assert "non ha disponibilita registrate" in r.content.decode()
    assert Availability.objects.count() == 0


def test_copy_ignores_inactive_teachers(admin, school):
    t = teacher(school, active=False)
    store(t, W1)
    html = copy(admin, W2).content.decode()
    assert "Copiati 0 docenti" not in html or "non ha" in html
    assert Availability.objects.filter(week_start=W2).count() == 0


def test_copy_only_uses_previous_week_not_older(admin, school):
    t = teacher(school)
    store(t, W1)
    copy(admin, W3)
    assert not Availability.objects.filter(teacher=t, week_start=W3).exists()


# invalid requests


def test_invalid_week_rejected(admin, school):
    t = teacher(school)
    for bad in ("2026-09-08", "2026-08-31", "2027-12-06", "abc"):
        r = admin.post(
            f"/disponibilita/{bad}/docente/{t.pk}/toggle/", {"day": "lunedi", "slot": 0}
        )
        assert r.status_code in (400, 404)
        assert admin.post(f"/disponibilita/{bad}/copia/").status_code in (400, 404)
    assert admin.get(PAGE + "?settimana=2026-09-08").status_code == 400
    assert Availability.objects.count() == 0


def test_unknown_or_foreign_or_inactive_teacher_rejected(admin, school):
    other = make_school("Altra")
    foreign = teacher(other, "ESTERNO")
    inactive = teacher(school, "OFF", active=False)
    for tid in (99999, foreign.pk, inactive.pk):
        assert toggle(admin, Teacher(pk=tid), W2).status_code == 404
    assert Availability.objects.count() == 0


def test_malformed_requests_write_nothing(admin, school):
    t = teacher(school)
    for data in (
        {"day": "sabato", "slot": 0},
        {"day": "lunedi", "slot": 99},
        {"day": "lunedi", "slot": "x"},
        {},
    ):
        r = admin.post(url("toggle", W2, t.pk), data, **HX)
        assert r.status_code == 400
    bad_grids = ["not json", "[]", json.dumps({"lunedi": []}), ""]
    broken = grid_with()
    broken["lunedi"][0]["disponibile"] = "si"
    bad_grids.append(json.dumps(broken))
    for bad in bad_grids:
        r = admin.post(url("salva", W2, t.pk), {"grid": bad}, **HX)
        assert r.status_code == 400
    assert Availability.objects.count() == 0


def test_writes_are_post_only(admin, school):
    t = teacher(school)
    assert admin.get(url("toggle", W2, t.pk)).status_code == 405
    assert admin.get(url("copy", W2)).status_code == 405

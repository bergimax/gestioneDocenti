import datetime
import re

import pytest
from django.test import Client

from accounts.models import User
from scheduling import views
from scheduling.models import (
    JobStatus,
    Slot,
    SolveJob,
    Timetable,
    TimetableState,
)
from school.models import (
    Assignment,
    CalendarEntry,
    Class,
    School,
    Subject,
    Teacher,
)

D = datetime.date
MON = D(2026, 10, 12)
PAGE = "/orario/"
GRID = f"/orario/{MON:%Y-%m-%d}/griglia/"
GENERATE = f"/orario/{MON:%Y-%m-%d}/genera/"
STATUS = f"/orario/{MON:%Y-%m-%d}/stato/"
HX = {"HTTP_HX_REQUEST": "true"}


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
        name="S",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )


@pytest.fixture
def admin(db):
    return login("ADMIN")


@pytest.fixture
def seg(db):
    return login("SEGRETERIA")


@pytest.fixture
def world(school):
    class W:
        pass

    w = W()
    w.school = school
    w.a = Class.objects.create(school=school, name="1A", group="PRIME")
    w.b = Class.objects.create(school=school, name="1B", group="SECONDE")
    w.rossi = Teacher.objects.create(
        school=school, name="ROSSI MARIO", type="ASSUNTO", first_time=False
    )
    w.bianchi = Teacher.objects.create(
        school=school, name="BIANCHI ANNA", type="ASSUNTO", first_time=False
    )
    w.mat = Subject.objects.create(
        school=school, name="Matematica", kind="TEORIA", cognitive_weight="MEDIO"
    )
    Assignment.objects.create(
        school=school, teacher=w.rossi, school_class=w.a, subject=w.mat, ore_totali=10
    )
    w.tt = Timetable.objects.create(
        school=school, week_start=MON, version=3, quality_score=82.5, quality_level="B"
    )
    return w


def slot(w, klass, teacher, day="LUNEDI", hour=8, **kw):
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


def html(response):
    return response.content.decode()


def cells(text, css):
    return re.findall(rf'<td class="{css}[^"]*"[^>]*>', text)


# ---- access ---------------------------------------------------------------


def test_anonymous_is_redirected_to_login(client, school):
    for url in (PAGE, GRID, STATUS):
        r = client.get(url)
        assert r.status_code == 302 and r.url.startswith("/login/")
    assert client.post(GENERATE).status_code == 302


@pytest.mark.parametrize("role", ["ADMIN", "SEGRETERIA"])
def test_both_roles_open_the_page(school, role):
    assert login(role).get(PAGE).status_code == 200


def test_page_without_school_says_so(db, admin):
    assert "Nessuna scuola configurata" in html(admin.get(PAGE))


# ---- week selector ----------------------------------------------------------


def test_selector_lists_every_monday_from_first_monday(school, admin):
    text = html(admin.get(PAGE))
    values = re.findall(r'<option value="(\d{4}-\d{2}-\d{2})"', text)
    assert values[0] == "2026-09-07"  # 1 Sep 2026 is a Tuesday
    assert values[-1] == "2027-06-28"
    assert len(values) == 43
    assert all(D.fromisoformat(v).weekday() == 0 for v in values)


def test_default_week_is_the_current_one(school, admin):
    text = html(admin.get(PAGE))
    assert '<option value="2026-10-12" selected>' in text


def test_chosen_week_is_loaded(school, admin):
    text = html(admin.get(PAGE, {"settimana": "2026-10-19"}))
    assert '<option value="2026-10-19" selected>' in text


@pytest.mark.parametrize(
    "raw", ["2026-10-13", "abc", "2026-02-30", "2020-10-12", "2027-07-05"]
)
def test_invalid_week_in_url_is_404(school, admin, raw):
    assert admin.get(PAGE, {"settimana": raw}).status_code == 404
    assert admin.get(f"/orario/{raw}/griglia/").status_code == 404
    assert admin.get(f"/orario/{raw}/stato/").status_code == 404
    assert admin.post(f"/orario/{raw}/genera/").status_code == 404
    assert SolveJob.objects.count() == 0


# ---- empty state and generate ------------------------------------------------


def test_week_without_timetable_shows_empty_state_and_button(school, admin):
    text = html(admin.get(PAGE, {"settimana": "2026-10-12"}))
    assert "Nessun orario" in text
    assert "Genera orario" in text
    assert "tt-grid" not in text


def test_segreteria_empty_state_has_no_button(school, seg):
    text = html(seg.get(PAGE, {"settimana": "2026-10-12"}))
    assert "Nessun orario" in text
    assert "Genera orario" not in text


def test_generate_creates_job_and_shows_running_status(world, admin):
    r = admin.post(GENERATE, **HX)
    assert r.status_code == 200
    job = SolveJob.objects.get()
    assert job.status == JobStatus.PENDING and job.week_start == MON
    text = html(r)
    assert "Generazione in corso" in text
    assert f'hx-get="{STATUS}"' in text and 'hx-trigger="every 2s"' in text
    assert "Genera orario" not in text


def test_generate_twice_creates_one_job(world, admin):
    admin.post(GENERATE, **HX)
    r = admin.post(GENERATE, **HX)
    assert r.status_code == 200
    assert SolveJob.objects.count() == 1


def test_page_with_running_job_polls_and_has_no_button(world, admin):
    SolveJob.objects.create(school=world.school, week_start=MON)
    text = html(admin.get(PAGE))
    assert "Generazione in corso" in text
    assert "Genera orario" not in text


def test_status_while_running_has_no_grid(world, admin):
    SolveJob.objects.create(school=world.school, week_start=MON)
    text = html(admin.get(STATUS, **HX))
    assert "Generazione in corso" in text
    assert "tt-grid" not in text and "hx-swap-oob" not in text


def test_status_when_done_returns_grid_out_of_band(world, admin):
    slot(world, world.a, world.rossi)
    job = SolveJob.objects.create(school=world.school, week_start=MON)
    job.transition_to(JobStatus.RUNNING)
    job.transition_to(JobStatus.DONE, result={})
    text = html(admin.get(STATUS, **HX))
    assert 'id="timetable-body" hx-swap-oob="true"' in text
    assert "tt-grid" in text and "ROSSI" in text
    assert "Generazione in corso" not in text
    assert "Genera orario" in text


def test_failed_job_shows_error_and_button_again(world, admin):
    job = SolveJob.objects.create(school=world.school, week_start=MON)
    job.transition_to(JobStatus.RUNNING)
    job.transition_to(
        JobStatus.FAILED, error="Nessun orario possibile per questa settimana."
    )
    for text in (html(admin.get(STATUS, **HX)), html(admin.get(PAGE))):
        assert "Nessun orario possibile per questa settimana." in text
        assert "Genera orario" in text
        assert "in corso" not in text


def test_generate_without_assignments_is_409_with_message(school, admin):
    r = admin.post(GENERATE, **HX)
    assert r.status_code == 409
    assert "Nessuna assegnazione" in html(r)
    assert SolveJob.objects.count() == 0


def test_generate_on_approved_week_is_refused(world, admin):
    Timetable.objects.filter(pk=world.tt.pk).update(state=TimetableState.APPROVATO)
    r = admin.post(GENERATE, **HX)
    assert r.status_code == 409
    assert "già approvata" in html(r)
    assert SolveJob.objects.count() == 0


def test_segreteria_cannot_generate(world, seg):
    r = seg.post(GENERATE, **HX)
    assert r.status_code == 403
    assert "Permesso negato" in html(r)  # the predicate's page, not Django's default
    assert SolveJob.objects.count() == 0


def test_generate_rejects_get(world, admin):
    assert admin.get(GENERATE).status_code == 405


def test_response_swap_handler_covers_409(world, admin):
    assert "status === 409" in html(admin.get(PAGE))


# ---- grid -----------------------------------------------------------------


def test_grid_has_day_blocks_class_rows_and_hour_columns(world, admin):
    text = html(admin.get(PAGE))
    assert text.count('<table class="tt-grid">') == 5
    for label in ("Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì"):
        assert f'aria-label="{label}"' in text
    assert text.count('<th scope="row">1A</th>') == 5
    assert text.count('<th scope="row">1B</th>') == 5
    for h in range(8, 14):
        assert f'<th scope="col">{h}-{h + 1}</th>' in text
    assert '<th scope="col">14-15</th>' not in text
    # 2 classes x 6 hours x 5 days
    assert len(re.findall(r"<td class=", text)) == 60


def test_lesson_shows_subject_and_teacher_surname_only(world, admin):
    slot(world, world.a, world.rossi, hour=9)
    text = html(admin.get(PAGE))
    assert '<span class="tt-subject">Matematica</span>' in text
    assert '<span class="tt-teacher">ROSSI</span>' in text
    assert "MARIO" not in text


def test_lesson_is_in_the_right_cell(world, admin):
    slot(world, world.a, world.rossi, day="MARTEDI", hour=10)
    text = html(admin.get(PAGE))
    tuesday = text.split('aria-label="Martedì"')[1].split("</section>")[0]
    row_1a = tuesday.split('<th scope="row">1A</th>')[1].split("</tr>")[0]
    tds = re.findall(r"<td class=\"(tt-\w+)", row_1a)
    assert tds == ["tt-free", "tt-free", "tt-lesson", "tt-free", "tt-free", "tt-free"]
    monday = text.split('aria-label="Lunedì"')[1].split("</section>")[0]
    assert "tt-lesson" not in monday


def test_paired_lesson_shows_in_both_classes(world, admin):
    slot(world, world.a, world.rossi, paired=True, paired_class=world.b)
    text = html(admin.get(PAGE))
    monday = text.split('aria-label="Lunedì"')[1].split("</section>")[0]
    for name in ("1A", "1B"):
        row = monday.split(f'<th scope="row">{name}</th>')[1].split("</tr>")[0]
        assert 'class="tt-lesson' in row and "ROSSI" in row


def test_partner_class_own_lesson_wins_over_pair_copy(world, admin):
    slot(world, world.a, world.rossi, paired=True, paired_class=world.b)
    slot(world, world.b, world.bianchi, paired=True, paired_class=world.a)
    monday = html(admin.get(PAGE)).split('aria-label="Lunedì"')[1]
    row_b = monday.split('<th scope="row">1B</th>')[1].split("</tr>")[0]
    assert "BIANCHI" in row_b and "ROSSI" not in row_b


def test_class_stage_day_shows_yellow_stage_cells_without_teacher(world, admin):
    CalendarEntry.objects.create(
        school=world.school,
        date=MON + datetime.timedelta(days=1),
        stage_class=world.a,
    )
    text = html(admin.get(PAGE))
    tuesday = text.split('aria-label="Martedì"')[1].split("</section>")[0]
    row_a = tuesday.split('<th scope="row">1A</th>')[1].split("</tr>")[0]
    row_b = tuesday.split('<th scope="row">1B</th>')[1].split("</tr>")[0]
    assert row_a.count('<td class="tt-stage">STAGE</td>') == 6
    assert "STAGE" not in row_b
    monday = text.split('aria-label="Lunedì"')[1].split("</section>")[0]
    assert "STAGE" not in monday


def test_group_stage_applies_to_the_group_only(world, admin):
    CalendarEntry.objects.create(
        school=world.school, date=MON, group="SECONDE", is_group_stage=True, max_hours=0
    )
    monday = (
        html(admin.get(PAGE)).split('aria-label="Lunedì"')[1].split("</section>")[0]
    )
    row_a = monday.split('<th scope="row">1A</th>')[1].split("</tr>")[0]
    row_b = monday.split('<th scope="row">1B</th>')[1].split("</tr>")[0]
    assert row_b.count("STAGE") == 6
    assert "STAGE" not in row_a


# ---- conflicts, quality ---------------------------------------------------


def set_conflicts(world, items):
    Timetable.objects.filter(pk=world.tt.pk).update(conflicts=items)


def test_no_conflicts_no_marks(world, admin):
    slot(world, world.a, world.rossi)
    text = html(admin.get(PAGE))
    assert 'data-conflict="true"' not in text
    assert "tt-conflict" not in text.replace(".tt-conflict", "")
    assert "Conflitti (" not in text


def test_conflicting_lesson_is_marked_and_listed(world, admin):
    slot(world, world.a, world.rossi, hour=9)
    slot(world, world.a, world.rossi, hour=10)
    set_conflicts(
        world,
        [
            {
                "kind": "docente_double_booking",
                "description": "Il docente ROSSI e doppiamente prenotato",
                "classe_id": world.a.pk,
                "docente_id": world.rossi.pk,
                "giorno": "LUNEDI",
                "ore_slot": 1,
            }
        ],
    )
    text = html(admin.get(PAGE))
    assert text.count('data-conflict="true"') == 1
    marked = re.search(r'<td class="tt-lesson tt-conflict" data-conflict="true"[ >]', text)
    assert marked
    assert "Conflitti (1)" in text
    assert "Il docente ROSSI e doppiamente prenotato" in text


def test_conflict_marks_other_class_of_same_teacher(world, admin):
    slot(world, world.a, world.rossi, hour=9)
    slot(world, world.b, world.rossi, hour=9)
    set_conflicts(
        world,
        [
            {
                "description": "d",
                "classe_id": world.a.pk,
                "docente_id": world.rossi.pk,
                "giorno": "LUNEDI",
                "ore_slot": 1,
            }
        ],
    )
    assert html(admin.get(PAGE)).count('data-conflict="true"') == 2


def test_conflict_on_other_day_or_hour_does_not_mark(world, admin):
    slot(world, world.a, world.rossi, hour=9)
    set_conflicts(
        world,
        [
            {
                "description": "x",
                "classe_id": world.a.pk,
                "docente_id": world.rossi.pk,
                "giorno": "MARTEDI",
                "ore_slot": 1,
            },
            {
                "description": "y",
                "classe_id": world.a.pk,
                "docente_id": world.rossi.pk,
                "giorno": "LUNEDI",
                "ore_slot": 2,
            },
        ],
    )
    text = html(admin.get(PAGE))
    assert '<td class="tt-lesson" data-day="LUNEDI"' in text
    assert "tt-lesson tt-conflict" not in text
    assert "Conflitti (2)" in text


def test_conflict_without_slot_is_listed_but_marks_nothing(world, admin):
    set_conflicts(
        world, [{"description": "Ore fuori target", "docente_id": world.rossi.pk}]
    )
    text = html(admin.get(PAGE))
    assert 'data-conflict="true"' not in text
    assert "Ore fuori target" in text


def test_unfilled_conflict_marks_the_empty_cell_and_shows_message(world, admin):
    set_conflicts(
        world,
        [
            {
                "kind": "classe_unfilled",
                "description": "Ora scoperta",
                "classe_id": world.a.pk,
                "docente_id": None,
                "giorno": "LUNEDI",
                "ore_slot": 0,
            }
        ],
    )
    text = html(admin.get(PAGE))
    assert '<td class="tt-free tt-conflict" data-conflict="true" ' in text
    assert "Nessun orario completo possibile" in text


def test_complete_result_has_no_best_effort_message(world, admin):
    assert "Nessun orario completo possibile" not in html(admin.get(PAGE))


def test_quality_score_and_level_are_shown(world, admin):
    text = html(admin.get(PAGE))
    assert "livello <strong>B</strong>" in text
    assert "82,5" in text


# ---- approved week --------------------------------------------------------


def test_approved_week_shows_badge_and_no_button(world, admin):
    Timetable.objects.filter(pk=world.tt.pk).update(state=TimetableState.APPROVATO)
    text = html(admin.get(PAGE))
    assert "Approvata" in text
    assert "Genera orario" not in text


def test_draft_week_has_no_approved_badge(world, admin):
    text = html(admin.get(PAGE))
    assert "Approvata" not in text
    assert "Genera orario" in text


# ---- version polling --------------------------------------------------------


def test_page_polls_the_version_endpoint(world, admin):
    text = html(admin.get(PAGE))
    assert f'hx-get="{GRID}?versione=3"' in text
    assert 'hx-trigger="every 5s"' in text


def test_same_version_returns_204_without_body(world, admin):
    r = admin.get(GRID, {"versione": "3"}, **HX)
    assert r.status_code == 204
    assert r.content == b""


def test_new_version_returns_the_grid(world, admin):
    slot(world, world.a, world.rossi)
    r = admin.get(GRID, {"versione": "2"}, **HX)
    assert r.status_code == 200
    assert "tt-grid" in html(r) and "?versione=3" in html(r)


def test_missing_version_returns_the_grid(world, admin):
    assert admin.get(GRID).status_code == 200


def test_week_without_timetable_polls_with_sentinel(school, admin):
    assert admin.get(GRID, {"versione": "-1"}).status_code == 204
    r = admin.get(GRID, {"versione": "0"})
    assert r.status_code == 200 and "Nessun orario" in html(r)


def test_segreteria_polls_the_grid_too(world, seg):
    assert seg.get(GRID, {"versione": "1"}).status_code == 200


# ---- viewer: segreteria and leaks -------------------------------------------


def test_segreteria_sees_data_but_no_actions(world, seg):
    slot(world, world.a, world.rossi)
    set_conflicts(world, [{"description": "Un conflitto", "giorno": None}])
    text = html(seg.get(PAGE))
    assert "ROSSI" in text and "82,5" in text and "Un conflitto" in text
    assert "Genera orario" not in text
    assert "/genera/" not in text
    assert "hx-post" not in text


def test_segreteria_status_has_no_button(world, seg):
    assert "Genera orario" not in html(seg.get(STATUS, **HX))


def test_html_does_not_leak_job_internals(world, admin):
    job = SolveJob.objects.create(
        school=world.school, week_start=MON, params={"secret": "pw-token-xyz"}
    )
    job.transition_to(JobStatus.RUNNING)
    job.transition_to(JobStatus.DONE, result={"internal": "leak-me"})
    for url in (PAGE, STATUS):
        text = html(admin.get(url))
        assert "pw-token-xyz" not in text and "leak-me" not in text


def test_grid_is_scrolled_inside_its_own_wrapper(world, admin):
    text = html(admin.get(PAGE))
    assert text.count('<div class="tt-scroll">') == 5
    css = open("static/css/app.css").read()
    assert re.search(r"\.tt-scroll \{[^}]*overflow-x: auto", css)


def test_menu_marks_orario_active(world, admin):
    assert re.search(
        r'class="active" aria-current="page">Orario<', html(admin.get(PAGE))
    )

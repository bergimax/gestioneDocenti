"""Repair solving end to end (#33): form, REPAIR job, diff, confirm, cancel."""

import dataclasses
import datetime
import re
from unittest import mock

import pytest
from django.test import Client, override_settings

from accounts.models import User
from availability.models import Availability
from scheduling import repair, tasks
from scheduling.context import build_week_context
from scheduling.models import (
    JobKind,
    JobStatus,
    Slot,
    SolveJob,
    Timetable,
    TimetableState,
)
from scheduling.moves import MoveRejected
from scheduling.services import apply_change
from scheduling.solver import (
    CLASSE_UNFILLED,
    NO_COMPLETE_MESSAGE,
    TEACHER_ABSENT,
    Perturbation,
    SolveResult,
    SolveStatus,
    apply_perturbation,
    diff_lessons,
    solve,
)
from school.models import (
    Assignment,
    CalendarEntry,
    Class,
    ClassPairing,
    School,
    Subject,
    Teacher,
)

D = datetime.date
MON = D(2026, 10, 12)
WEEK = f"{MON:%Y-%m-%d}"
URL = f"/orario/{WEEK}/sostituzione/"
STATUS = URL + "stato/"
PAGE = f"/orario/?settimana={WEEK}"
DAYS = repair.DAYS


@pytest.fixture
def rw(db):
    """3 classes x 5 teachers, 4 lessons a day Monday-Friday, a generated week."""

    class R:
        pass

    r = R()
    s = r.school = School.objects.create(
        name="S",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2026, 12, 21),
    )
    for i in range(5):
        CalendarEntry.objects.create(
            school=s, date=MON + datetime.timedelta(days=i), max_hours=4
        )
    r.classes = [Class.objects.create(school=s, name=n) for n in ("1A", "1B", "1C")]
    r.teachers = [
        Teacher.objects.create(
            school=s, name=f"DOC{i} NOME", type="ASSUNTO", first_time=False
        )
        for i in range(5)
    ]
    r.subjects = [
        Subject.objects.create(
            school=s, name=f"Mat{i}", kind="TEORIA", cognitive_weight="MEDIO"
        )
        for i in range(5)
    ]
    for c in r.classes:
        for t, sub in zip(r.teachers, r.subjects, strict=True):
            Assignment.objects.create(
                school=s, teacher=t, school_class=c, subject=sub, ore_totali=40
            )
    job = SolveJob.objects.create(school=s, week_start=MON)
    tasks.run_job(job.pk)
    r.tt = Timetable.objects.get(school=s, week_start=MON)
    return r


@pytest.fixture
def admin(db):
    c = Client()
    c.force_login(User.objects.create_user("a@example.com", "pw-long-enough", "ADMIN"))
    return c


@pytest.fixture
def seg(db):
    c = Client()
    c.force_login(
        User.objects.create_user("s@example.com", "pw-long-enough", "SEGRETERIA")
    )
    return c


def html(response):
    return response.content.decode()


def absent(r, teacher=0, days=("MERCOLEDI",)):
    return {"tipo": "teacher_absent", "docente": r.teachers[teacher].pk, "giorni": days}


def removed(r, klass=0, day="LUNEDI", hour=11):
    return {
        "tipo": "slot_removed",
        "classe": r.classes[klass].pk,
        "giorno": day,
        "ora": hour,
    }


def closed(r, klass=0, day="LUNEDI"):
    return {"tipo": "class_closed", "classe": r.classes[klass].pk, "giorno": day}


def job_of():
    return SolveJob.objects.filter(kind=JobKind.REPAIR).order_by("-pk").first()


def start(client, data):
    """Submit the form, run the job in this process, return the finished job."""
    response = client.post(URL, data)
    assert response.status_code == 200, html(response)
    job = job_of()
    repair.run_repair(job.pk)
    job.refresh_from_db()
    return job


def confirm_url(job):
    return f"{URL}{job.pk}/conferma/"


def dismiss_url(job):
    return f"{URL}{job.pk}/annulla/"


def cells(r, **where):
    return {
        (s.school_class_id, s.teacher_id, s.subject_id, s.day, s.start_hour)
        for s in Slot.objects.filter(timetable=r.tt, **where)
    }


def snapshot(r):
    r.tt.refresh_from_db()
    return r.tt.version, cells(r), r.tt.quality_score, r.tt.conflicts


def no_double_booking(r):
    slots = list(Slot.objects.filter(timetable=r.tt))
    teacher_cells = [(s.teacher_id, s.day, s.start_hour) for s in slots]
    class_cells = [(s.school_class_id, s.day, s.start_hour) for s in slots]
    assert len(set(teacher_cells)) == len(teacher_cells)
    assert len(set(class_cells)) == len(class_cells)
    for c in r.classes:
        for d in DAYS:
            hours = sorted(
                s.start_hour for s in slots if s.school_class_id == c.pk and s.day == d
            )
            if hours:
                assert hours == list(range(hours[0], hours[0] + len(hours)))


# ---- the form ----------------------------------------------------------------


def test_admin_sees_the_sostituzione_form_on_a_draft_week(admin, rw):
    page = html(admin.get(PAGE))
    assert "Sostituzione" in page and 'name="tipo"' in page
    for kind in ("teacher_absent", "slot_removed", "class_closed"):
        assert f'value="{kind}"' in page
    assert 'name="giorni"' in page and 'name="giorno"' in page and 'name="ora"' in page
    assert 'value="teacher_absent" data-needs="docente giorni"' in page
    assert 'value="slot_removed" data-needs="classe giorno ora"' in page
    assert 'value="class_closed" data-needs="classe giorno"' in page


def test_each_form_field_has_an_id_of_its_own(admin, rw):
    page = html(admin.get(PAGE))
    ids = re.findall(r'id="(id_[a-z_]+)"', page)
    assert len(ids) == len(set(ids))


def test_a_valid_submission_starts_one_repair_job_and_shows_its_status(admin, rw):
    version = rw.tt.version
    with mock.patch.object(repair, "run_repair_job") as task:
        enqueue = task.enqueue
        response = admin.post(URL, absent(rw, days=("MERCOLEDI", "GIOVEDI")))
    assert response.status_code == 200
    job = job_of()
    enqueue.assert_called_once_with(job.pk)
    assert job.kind == JobKind.REPAIR and job.status == JobStatus.PENDING
    assert job.params == {
        "repair": {
            "kind": "teacher_absent",
            "teacher_id": rw.teachers[0].pk,
            "class_id": None,
            "days": ["MERCOLEDI", "GIOVEDI"],
            "hour": None,
        }
    }
    page = html(response)
    assert "Sostituzione in corso" in page
    assert f'hx-get="{STATUS}"' in page and 'hx-trigger="every 2s"' in page
    assert "Docente assente: DOC0 NOME, mercoledì, giovedì" in page
    rw.tt.refresh_from_db()
    assert rw.tt.version == version  # starting a repair does not touch the timetable


def test_enqueue_failure_leaves_no_job(admin, rw):
    with mock.patch.object(repair, "run_repair_job") as task:
        task.enqueue.side_effect = OSError
        with pytest.raises(OSError):
            admin.post(URL, absent(rw))
    assert not SolveJob.objects.filter(kind=JobKind.REPAIR).exists()


def test_slot_and_class_params_are_stored(admin, rw):
    admin.post(URL, removed(rw, hour=10))
    assert job_of().params["repair"] == {
        "kind": "slot_removed",
        "teacher_id": None,
        "class_id": rw.classes[0].pk,
        "days": ["LUNEDI"],
        "hour": 10,
    }
    job_of().delete()
    admin.post(URL, closed(rw, klass=1, day="VENERDI"))
    assert job_of().params["repair"] == {
        "kind": "class_closed",
        "teacher_id": None,
        "class_id": rw.classes[1].pk,
        "days": ["VENERDI"],
        "hour": None,
    }


def free_hour(r, klass=0, day="LUNEDI"):
    used = {
        s.start_hour
        for s in Slot.objects.filter(
            timetable=r.tt, school_class=r.classes[klass], day=day
        )
    }
    return next(h for h in range(8, 14) if h not in used)


def reject_cases(rw):
    return [
        ("no kind", {}, "Scegli il tipo di sostituzione."),
        (
            "no day",
            {"tipo": "teacher_absent", "docente": rw.teachers[0].pk},
            "Scegli almeno un giorno.",
        ),
        (
            "saturday",
            absent(rw, days=("SABATO",)),
            "sono ammessi solo i giorni feriali",
        ),
        (
            "saturday single",
            {**closed(rw), "giorno": "SABATO"},
            "sono ammessi solo i giorni feriali",
        ),
        (
            "no teacher",
            {"tipo": "teacher_absent", "giorni": ["LUNEDI"]},
            "Scegli il docente.",
        ),
        (
            "unknown teacher",
            {"tipo": "teacher_absent", "docente": 99999, "giorni": ["LUNEDI"]},
            "Il docente indicato non esiste.",
        ),
        (
            "unknown class",
            {"tipo": "class_closed", "classe": 99999, "giorno": "LUNEDI"},
            "La classe indicata non esiste.",
        ),
        ("no class", {"tipo": "class_closed", "giorno": "LUNEDI"}, "Scegli la classe."),
        ("no hour", {**removed(rw), "ora": ""}, "Indica l'ora di inizio."),
        ("hour 7", removed(rw, hour=7), "L'ora deve essere compresa tra 8 e 13."),
        ("hour 14", removed(rw, hour=14), "L'ora deve essere compresa tra 8 e 13."),
        ("hour text", removed(rw, hour="ab"), "L'ora deve essere compresa tra 8 e 13."),
    ]


def test_invalid_submissions_show_an_error_and_start_no_job(admin, rw):
    before = snapshot(rw)
    for label, data, message in reject_cases(rw):
        response = admin.post(URL, data)
        assert response.status_code == 400, label
        assert message in html(response).replace("&#x27;", "'"), label
        assert not SolveJob.objects.filter(kind=JobKind.REPAIR).exists(), label
    assert snapshot(rw) == before


def test_unknown_slot_is_rejected(admin, rw):
    response = admin.post(URL, removed(rw, hour=free_hour(rw)))
    assert response.status_code == 400
    assert "nessuna lezione della classe" in html(response)
    assert not SolveJob.objects.filter(kind=JobKind.REPAIR).exists()


def test_a_teacher_with_no_lessons_that_week_has_nothing_to_repair(admin, rw):
    Slot.objects.filter(timetable=rw.tt, teacher=rw.teachers[0]).delete()
    response = admin.post(URL, absent(rw))
    assert response.status_code == 400
    assert "Niente da riparare" in html(response)
    assert not SolveJob.objects.filter(kind=JobKind.REPAIR).exists()


def test_a_teacher_of_another_school_is_unknown(admin, rw):
    other = School.objects.create(
        name="O",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2026, 12, 21),
    )
    stranger = Teacher.objects.create(
        school=other, name="X", type="ASSUNTO", first_time=False
    )
    response = admin.post(
        URL, {"tipo": "teacher_absent", "docente": stranger.pk, "giorni": ["LUNEDI"]}
    )
    assert response.status_code == 400
    assert not SolveJob.objects.filter(kind=JobKind.REPAIR).exists()


def test_a_week_without_timetable_is_refused_and_has_no_form(admin, rw):
    rw.tt.delete()
    assert 'name="tipo"' not in html(admin.get(PAGE))
    response = admin.post(URL, absent(rw))
    assert response.status_code == 409
    assert "genera prima l&#x27;orario" in html(response)
    assert not SolveJob.objects.filter(kind=JobKind.REPAIR).exists()


def test_a_second_repair_while_one_is_active_is_refused(admin, rw):
    assert admin.post(URL, absent(rw)).status_code == 200
    response = admin.post(URL, absent(rw, days=("LUNEDI",)))
    assert response.status_code == 409 and "già in corso" in html(response)
    assert SolveJob.objects.filter(kind=JobKind.REPAIR).count() == 1


def test_a_running_generation_blocks_the_repair(admin, rw):
    SolveJob.objects.create(school=rw.school, week_start=MON, kind=JobKind.GENERATE)
    response = admin.post(URL, absent(rw))
    assert response.status_code == 409 and "già in corso" in html(response)
    assert not SolveJob.objects.filter(kind=JobKind.REPAIR).exists()


def test_a_finished_repair_does_not_block_the_next(admin, rw):
    start(admin, absent(rw))
    assert admin.post(URL, absent(rw, days=("LUNEDI",))).status_code == 200
    assert SolveJob.objects.filter(kind=JobKind.REPAIR).count() == 2


# ---- permissions and the approved week ------------------------------------


def test_approved_week_hides_the_form_and_rejects_the_repair(admin, rw):
    rw.tt.state = TimetableState.APPROVATO
    rw.tt.save()
    assert 'name="tipo"' not in html(admin.get(PAGE))
    response = admin.post(URL, absent(rw))
    assert response.status_code == 409 and "approvata" in html(response)
    assert not SolveJob.objects.filter(kind=JobKind.REPAIR).exists()


def test_segreteria_sees_no_form_and_cannot_post(seg, rw):
    page = html(seg.get(PAGE))
    assert 'name="tipo"' not in page and "Calcola la sostituzione" not in page
    assert seg.post(URL, absent(rw)).status_code == 403
    assert not SolveJob.objects.filter(kind=JobKind.REPAIR).exists()


def test_segreteria_cannot_poll_confirm_or_dismiss(seg, admin, rw):
    job = start(admin, absent(rw))
    before = snapshot(rw)
    assert seg.get(STATUS).status_code == 403
    assert seg.post(confirm_url(job)).status_code == 403
    assert seg.post(dismiss_url(job)).status_code == 403
    assert snapshot(rw) == before


def test_segreteria_does_not_see_a_finished_proposal(seg, admin, rw):
    start(admin, absent(rw))
    page = html(seg.get(PAGE))
    assert "Conferma" not in page and "Proposta" not in page


def test_anonymous_users_are_redirected_to_login(rw):
    c = Client()
    for response in (
        c.post(URL, absent(rw)),
        c.get(STATUS),
        c.post(f"{URL}1/conferma/"),
        c.post(f"{URL}1/annulla/"),
    ):
        assert response.status_code == 302 and "/login" in response["Location"]
    assert not SolveJob.objects.filter(kind=JobKind.REPAIR).exists()


def test_only_post_starts_a_repair(admin, rw):
    assert admin.get(URL).status_code == 405
    assert admin.post(STATUS).status_code == 405


# ---- teacher absent --------------------------------------------------------


def test_teacher_absent_has_no_lessons_that_day_and_keeps_the_rest(admin, rw):
    teacher = rw.teachers[0]
    before = cells(rw)
    n = len([c for c in before if c[1] == teacher.pk and c[3] == "MERCOLEDI"])
    assert n > 0
    job = start(admin, absent(rw))
    assert job.status == JobStatus.DONE
    assert admin.post(confirm_url(job)).status_code == 200
    after = cells(rw)
    assert not [c for c in after if c[1] == teacher.pk and c[3] == "MERCOLEDI"]
    # H1-H3: nobody twice in one hour, every class day is one contiguous block
    no_double_booking(rw)
    for c in rw.classes:
        for d in DAYS:
            assert len([x for x in after if x[0] == c.pk and x[3] == d]) == 4
    # the other days' lessons of the teacher are not touched
    other = {c for c in before if c[1] == teacher.pk and c[3] != "MERCOLEDI"}
    assert other <= after
    # the hours the teacher lost are given back on other days
    assert len([c for c in after if c[1] == teacher.pk]) == len(
        [c for c in before if c[1] == teacher.pk]
    )


@pytest.mark.parametrize("day", DAYS)
def test_moved_lessons_are_at_most_twice_the_lessons_of_the_absent_teacher(
    admin, rw, day
):
    teacher = rw.teachers[0]
    n = len([c for c in cells(rw) if c[1] == teacher.pk and c[3] == day])
    job = start(admin, absent(rw, days=(day,)))
    assert 0 < job.result["n_changes"] <= 2 * n
    assert job.result["n_removed"] == 0 and job.result["n_added"] == 0


def test_full_regeneration_moves_clearly_more_lessons_than_the_repair(admin, rw):
    teacher = rw.teachers[0]
    job = start(admin, absent(rw))
    ctx = build_week_context(MON, rw.school)
    current = repair.current_lessons(list(Slot.objects.filter(timetable=rw.tt)), ctx)
    pert = Perturbation(TEACHER_ABSENT, teacher.pk, days=frozenset({2}))
    full = solve(apply_perturbation(ctx, pert))
    full_changes = len(diff_lessons(current, full.lessons))
    assert full_changes >= 3 * job.result["n_changes"]
    assert full_changes > 20  # a rebuilt week, not a repair


def test_two_absent_days_are_both_free_after_the_repair(admin, rw):
    job = start(admin, absent(rw, days=("LUNEDI", "VENERDI")))
    assert job.status == JobStatus.DONE
    admin.post(confirm_url(job))
    teacher = rw.teachers[0].pk
    assert not [
        c for c in cells(rw) if c[1] == teacher and c[3] in ("LUNEDI", "VENERDI")
    ]
    no_double_booking(rw)


def test_a_day_without_lessons_of_the_teacher_gives_an_empty_diff(
    admin, rw, monkeypatch
):
    Slot.objects.filter(timetable=rw.tt, teacher=rw.teachers[0], day="MARTEDI").delete()
    monkeypatch.setattr(
        repair, "solve_exclusive", mock.Mock(side_effect=AssertionError("no solve"))
    )
    before = snapshot(rw)
    job = start(admin, absent(rw, days=("MARTEDI",)))
    assert job.status == JobStatus.DONE
    assert job.result["empty"] is True and job.result["changes"] == []
    assert job.result["message"] == "Nessuna lezione da modificare"
    panel = html(admin.get(PAGE))
    assert "Nessuna lezione da modificare" in panel
    assert "Conferma" not in panel
    # a direct post is refused as well, and nothing moves
    response = admin.post(confirm_url(job))
    assert response.status_code == 409
    assert snapshot(rw)[0] == before[0] and snapshot(rw)[1] == before[1]


# ---- slot removed and class closed ----------------------------------------


@pytest.mark.parametrize("hour", [8, 9, 10, 11])
def test_a_removed_slot_is_empty_after_the_repair(admin, rw, hour):
    klass = rw.classes[0]
    job = start(admin, removed(rw, hour=hour))
    assert job.status == JobStatus.DONE
    assert admin.post(confirm_url(job)).status_code == 200
    monday = Slot.objects.filter(timetable=rw.tt, school_class=klass, day="LUNEDI")
    assert hour not in [s.start_hour for s in monday]
    assert monday.count() == 3
    no_double_booking(rw)
    # the other days of the class are as they were
    assert Slot.objects.filter(timetable=rw.tt, school_class=klass).count() == 19


def test_removing_the_last_slot_of_a_day_changes_that_one_lesson_only(admin, rw):
    last = max(
        s.start_hour
        for s in Slot.objects.filter(
            timetable=rw.tt, school_class=rw.classes[0], day="LUNEDI"
        )
    )
    job = start(admin, removed(rw, hour=last))
    assert job.result["n_changes"] == 1 and job.result["n_removed"] == 1
    (change,) = job.result["changes"]
    assert change["old"]["day"] == "LUNEDI" and change["old"]["hour"] == last
    assert change["new"] is None


def test_a_closed_class_has_no_lessons_that_day_and_no_capacity_conflict(admin, rw):
    klass = rw.classes[1]
    job = start(admin, closed(rw, klass=1, day="GIOVEDI"))
    assert job.status == JobStatus.DONE
    assert job.result["n_removed"] == 4 and job.result["n_changes"] == 4
    assert not any("non coperte" in text for text in job.result["conflicts"])
    admin.post(confirm_url(job))
    rw.tt.refresh_from_db()
    assert not Slot.objects.filter(timetable=rw.tt, school_class=klass, day="GIOVEDI")
    assert not [
        c
        for c in rw.tt.conflicts
        if c["kind"] == CLASSE_UNFILLED and c["giorno"] == "GIOVEDI"
    ]
    assert Slot.objects.filter(timetable=rw.tt, school_class=klass).count() == 16


def test_closing_a_day_the_class_already_does_not_have_gives_an_empty_diff(admin, rw):
    Slot.objects.filter(
        timetable=rw.tt, school_class=rw.classes[0], day="MARTEDI"
    ).delete()
    job = start(admin, closed(rw, day="MARTEDI"))
    assert job.result["empty"] is True


# ---- the diff view -----------------------------------------------------------


def test_status_poll_while_running_keeps_polling(admin, rw):
    admin.post(URL, absent(rw))
    response = admin.get(STATUS)
    assert response.status_code == 200
    assert "Sostituzione in corso" in html(response)
    assert 'hx-trigger="every 2s"' in html(response)


def test_the_diff_lists_every_change_with_a_count_and_the_two_buttons(admin, rw):
    job = start(admin, closed(rw, klass=1, day="GIOVEDI"))
    response = admin.get(STATUS)
    page = html(response)
    assert 'hx-trigger="every 2s"' not in page
    assert "<strong>4</strong> lezioni da modificare" in page
    assert page.count("rimossa") == 4
    # Which teacher teaches 1B on Thursday depends on the primary keys the
    # solver sees, so only the shape of the rows is asserted.
    assert "1B" in page and re.search(r"Mat\d", page) and re.search(r"DOC\d NOME", page)
    assert re.search(r"Gioved&igrave;|Giovedì", page)
    assert "Conferma" in page and "Annulla" in page
    assert f'action="{confirm_url(job)}"' in page
    assert f'action="{dismiss_url(job)}"' in page


def test_moved_lessons_show_the_old_and_the_new_slot(admin, rw):
    job = start(admin, absent(rw))
    page = html(admin.get(STATUS))
    for change in job.result["changes"]:
        old, new = change["old"], change["new"]
        assert f"{old['label']} {old['hour']}-{old['hour'] + 1}" in page
        assert f"{new['label']} {new['hour']}-{new['hour'] + 1}" in page
    assert "rimossa" not in page
    assert f"<strong>{job.result['n_changes']}</strong> lezioni" in page


def test_the_proposal_survives_a_page_reload(admin, rw):
    start(admin, closed(rw))
    assert "Conferma" in html(admin.get(PAGE))


def test_nothing_is_written_until_the_admin_confirms(admin, rw):
    before = snapshot(rw)
    job = start(admin, absent(rw))
    assert job.status == JobStatus.DONE
    html(admin.get(STATUS))
    html(admin.get(PAGE))
    assert snapshot(rw) == before


def test_annulla_leaves_the_timetable_and_version_unchanged(admin, rw):
    before = snapshot(rw)
    job = start(admin, absent(rw))
    response = admin.post(dismiss_url(job))
    assert response.status_code == 200 and "annullata" in html(response)
    assert "Conferma" not in html(response)
    assert snapshot(rw) == before
    job.refresh_from_db()
    assert job.result["dismissed"] is True
    assert "Conferma" not in html(admin.get(PAGE))
    again = admin.post(confirm_url(job))
    assert again.status_code == 409 and "annullata" in html(again)
    assert snapshot(rw) == before


def test_a_dismissed_proposal_cannot_be_dismissed_after_it_was_applied(admin, rw):
    job = start(admin, absent(rw))
    admin.post(confirm_url(job))
    response = admin.post(dismiss_url(job))
    assert response.status_code == 409 and "già stata applicata" in html(response)


# ---- confirm -----------------------------------------------------------------


def test_confirm_applies_the_diff_bumps_the_version_once_and_returns_the_grid(
    admin, rw
):
    job = start(admin, absent(rw))
    base = job.result["base_version"]
    rw.tt.refresh_from_db()
    assert rw.tt.version == base
    Timetable.objects.filter(pk=rw.tt.pk).update(
        quality_score=1.0, quality_level="C", conflicts=[{"junk": True}]
    )
    response = admin.post(confirm_url(job))
    assert response.status_code == 200
    page = html(response)
    assert 'id="timetable-body"' in page and 'hx-swap-oob="true"' in page
    assert "Sostituzione applicata" in page
    assert "Conferma" not in page
    rw.tt.refresh_from_db()
    assert rw.tt.version == base + 1
    # score and conflicts are recomputed, not carried over
    assert rw.tt.quality_score != 1.0 and rw.tt.conflicts == []
    assert rw.tt.quality_level in ("A", "B", "C") and rw.tt.quality_level != "C"
    # the grid in the answer is the new one
    new_cell = job.result["changes"][0]["new"]
    assert f'data-day="{new_cell["day"]}" data-hour="{new_cell["hour"]}"' in page


def test_confirm_writes_exactly_the_diff(admin, rw):
    before = cells(rw)
    job = start(admin, absent(rw))
    admin.post(confirm_url(job))
    after = cells(rw)
    gone = {c for c in before - after}
    came = {c for c in after - before}
    assert len(gone) == len(came) == job.result["n_moved"]
    ids = {
        (r["class_id"], r["teacher_id"], r["subject_id"]) for r in job.result["changes"]
    }
    assert {c[:3] for c in gone | came} <= ids
    for r in job.result["changes"]:
        old = (
            r["class_id"],
            r["teacher_id"],
            r["subject_id"],
            r["old"]["day"],
            r["old"]["hour"],
        )
        new = (
            r["class_id"],
            r["teacher_id"],
            r["subject_id"],
            r["new"]["day"],
            r["new"]["hour"],
        )
        assert old in gone and new in came
    assert Slot.objects.filter(timetable=rw.tt).count() == 60
    for s in Slot.objects.filter(timetable=rw.tt):
        assert s.end_hour == s.start_hour + 1


def test_confirming_twice_applies_once(admin, rw):
    job = start(admin, absent(rw))
    assert admin.post(confirm_url(job)).status_code == 200
    after_first = snapshot(rw)
    response = admin.post(confirm_url(job))
    assert response.status_code == 409 and "già stata applicata" in html(response)
    assert snapshot(rw) == after_first
    assert after_first[0] == job.result["base_version"] + 1


def test_a_changed_timetable_refuses_the_confirmation(admin, rw):
    job = start(admin, absent(rw))
    apply_change(rw.tt.pk, lambda locked: None)  # somebody else bumped the version
    before = snapshot(rw)
    page = html(admin.get(PAGE))
    assert "ripeti la sostituzione" in page and "Conferma" not in page
    response = admin.post(confirm_url(job))
    assert response.status_code == 409
    assert "ripeti la sostituzione" in html(response)
    assert snapshot(rw) == before
    job.refresh_from_db()
    assert job.result["applied"] is False


def test_a_regenerated_week_refuses_the_confirmation(admin, rw):
    job = start(admin, absent(rw))
    regen = SolveJob.objects.create(school=rw.school, week_start=MON)
    tasks.run_job(regen.pk)
    before = snapshot(rw)
    assert admin.post(confirm_url(job)).status_code == 409
    assert snapshot(rw) == before


def test_confirm_on_an_approved_week_is_refused(admin, rw):
    job = start(admin, absent(rw))
    before = snapshot(rw)
    Timetable.objects.filter(pk=rw.tt.pk).update(state=TimetableState.APPROVATO)
    response = admin.post(confirm_url(job))
    assert response.status_code == 409 and "approvata" in html(response)
    assert snapshot(rw) == before


def test_a_failure_while_confirming_rolls_everything_back(admin, rw, monkeypatch):
    job = start(admin, absent(rw))
    before = snapshot(rw)

    def boom(*args, **kwargs):
        raise MoveRejected("boom")

    monkeypatch.setattr(repair, "reevaluate", boom)
    response = admin.post(confirm_url(job))
    assert response.status_code == 409
    assert snapshot(rw) == before
    job.refresh_from_db()
    assert job.result["applied"] is False


def test_confirm_needs_a_finished_repair_of_this_week(admin, rw):
    admin.post(URL, absent(rw))
    pending = job_of()
    assert admin.post(confirm_url(pending)).status_code == 409
    assert admin.post(f"{URL}99999/conferma/").status_code == 404
    other = SolveJob.objects.create(
        school=rw.school,
        week_start=MON + datetime.timedelta(days=7),
        kind=JobKind.REPAIR,
    )
    assert admin.post(confirm_url(other)).status_code == 404
    gen = SolveJob.objects.filter(kind=JobKind.GENERATE).first()
    assert admin.post(confirm_url(gen)).status_code == 404


# ---- the perturbation is one-off ------------------------------------------------


def stores(r):
    return {
        "availability": list(Availability.objects.values()),
        "calendar": list(CalendarEntry.objects.order_by("pk").values()),
        "teachers": list(Teacher.objects.order_by("pk").values()),
        "classes": list(Class.objects.order_by("pk").values()),
        "assignments": list(Assignment.objects.order_by("pk").values()),
    }


@pytest.mark.parametrize(
    "build", [lambda r: absent(r), lambda r: removed(r), lambda r: closed(r)]
)
def test_the_perturbation_is_stored_nowhere_but_in_the_job(admin, rw, build):
    before = stores(rw)
    job = start(admin, build(rw))
    admin.post(confirm_url(job))
    assert stores(rw) == before
    assert "repair" in job.params
    # a plain regeneration afterwards knows nothing of it
    ctx = build_week_context(MON, rw.school)
    assert ctx.excluded_days == {} and ctx.blocked_cells == {}
    assert all(
        cap == 4 for caps in ctx.class_capacity.values() for cap in caps.values()
    )
    assert ctx.current_lessons is None


# ---- no complete repair ------------------------------------------------------


@pytest.fixture
def lonely(db):
    """One class and one teacher: 2 lessons on Monday and on Tuesday."""

    class R:
        pass

    r = R()
    s = r.school = School.objects.create(
        name="S",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2026, 12, 21),
    )
    for i in range(5):
        CalendarEntry.objects.create(
            school=s,
            date=MON + datetime.timedelta(days=i),
            max_hours=2 if i < 2 else 0,
            is_closure=i >= 2,
        )
    r.klass = Class.objects.create(school=s, name="1A")
    r.teacher = Teacher.objects.create(
        school=s, name="ROSSI MARIO", type="ASSUNTO", first_time=False
    )
    subject = Subject.objects.create(
        school=s, name="Mat", kind="TEORIA", cognitive_weight="MEDIO"
    )
    Assignment.objects.create(
        school=s,
        teacher=r.teacher,
        school_class=r.klass,
        subject=subject,
        ore_totali=40,
    )
    job = SolveJob.objects.create(school=s, week_start=MON)
    tasks.run_job(job.pk)
    r.tt = Timetable.objects.get(school=s, week_start=MON)
    assert r.tt.slots.count() == 4
    return r


def test_without_a_replacement_the_job_says_so_and_lists_why(admin, lonely):
    job = start(
        admin,
        {"tipo": "teacher_absent", "docente": lonely.teacher.pk, "giorni": ["LUNEDI"]},
    )
    assert job.status == JobStatus.DONE
    assert job.result["complete"] is False
    assert job.result["message"] == NO_COMPLETE_MESSAGE
    assert any(
        "non coperte" in c or "Ora non coperta" in c for c in job.result["conflicts"]
    )
    assert job.result["explanations"]
    page = html(admin.get(STATUS))
    assert NO_COMPLETE_MESSAGE in page
    assert "Conflitti della proposta" in page
    assert job.result["explanations"][0] in page.replace("&#x27;", "'")
    # the best-effort diff can be confirmed, with its conflicts shown beside it
    assert "Conferma" in page


def test_a_best_effort_diff_applies_and_keeps_its_conflicts(admin, lonely):
    job = start(
        admin,
        {"tipo": "teacher_absent", "docente": lonely.teacher.pk, "giorni": ["LUNEDI"]},
    )
    assert admin.post(confirm_url(job)).status_code == 200
    lonely.tt.refresh_from_db()
    assert lonely.tt.slots.filter(day="LUNEDI").count() == 0
    assert lonely.tt.slots.count() == 2
    assert [c for c in lonely.tt.conflicts if c["kind"] == CLASSE_UNFILLED]
    assert lonely.tt.quality_level == "C" or lonely.tt.quality_score < 100


def test_a_complete_repair_does_not_show_the_no_complete_message(admin, rw):
    job = start(admin, absent(rw))
    assert job.result["complete"] is True
    page = html(admin.get(STATUS))
    assert NO_COMPLETE_MESSAGE not in page
    assert "Perché non c'è una sostituzione completa" not in page


# ---- failures ----------------------------------------------------------------------


def test_a_timeout_fails_the_job_and_leaves_the_timetable_alone(admin, rw, monkeypatch):
    monkeypatch.setattr(
        repair,
        "solve_exclusive",
        lambda ctx: SolveResult(SolveStatus.UNKNOWN),
    )
    before = snapshot(rw)
    admin.post(URL, absent(rw))
    job = job_of()
    repair.run_repair(job.pk)
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert "timeout" in job.error and "secondi" in job.error
    assert snapshot(rw) == before
    page = html(admin.get(PAGE))
    assert job.error in page and "Conferma" not in page


@override_settings(SOLVER_TIMEOUT_SECONDS=7, SOLVER_EXPLAIN_BUDGET_SECONDS=3)
def test_the_repair_uses_the_solver_timeout_setting(admin, rw, monkeypatch):
    seen = []

    def fake(ctx):
        seen.append(ctx)
        return SolveResult(SolveStatus.UNKNOWN)

    monkeypatch.setattr(repair, "solve_exclusive", fake)
    admin.post(URL, absent(rw))
    repair.run_repair(job_of().pk)
    (ctx,) = seen
    assert ctx.timeout_seconds == 7 and ctx.explain_budget_seconds == 3
    assert "7 secondi" in job_of().error


def test_an_infeasible_answer_fails_the_job(admin, rw, monkeypatch):
    monkeypatch.setattr(
        repair, "solve_exclusive", lambda ctx: SolveResult(SolveStatus.INFEASIBLE)
    )
    admin.post(URL, absent(rw))
    repair.run_repair(job_of().pk)
    assert job_of().status == JobStatus.FAILED
    assert job_of().error == tasks.infeasible_message()


def test_an_unexpected_error_fails_the_job_with_a_message(admin, rw, monkeypatch):
    def boom(ctx):
        raise RuntimeError("kaput")

    monkeypatch.setattr(repair, "solve_exclusive", boom)
    admin.post(URL, absent(rw))
    repair.run_repair(job_of().pk)
    job = job_of()
    assert job.status == JobStatus.FAILED
    assert job.error.startswith(tasks.ERROR_UNEXPECTED) and "kaput" in job.error


def test_a_week_approved_before_the_job_runs_fails_the_job(admin, rw):
    admin.post(URL, absent(rw))
    Timetable.objects.filter(pk=rw.tt.pk).update(state=TimetableState.APPROVATO)
    repair.run_repair(job_of().pk)
    assert job_of().status == JobStatus.FAILED
    assert "approvata" in job_of().error


def test_only_a_pending_repair_job_runs(admin, rw):
    admin.post(URL, absent(rw))
    job = job_of()
    repair.run_repair(job.pk)
    result = SolveJob.objects.get(pk=job.pk).result
    repair.run_repair(job.pk)  # DONE already: skipped
    assert SolveJob.objects.get(pk=job.pk).result == result
    generate = SolveJob.objects.create(school=rw.school, week_start=MON)
    repair.run_repair(generate.pk)  # not a repair job: skipped
    generate.refresh_from_db()
    assert generate.status == JobStatus.PENDING


def test_a_generate_job_is_untouched_by_the_repair_code(admin, rw):
    job = SolveJob.objects.create(
        school=rw.school, week_start=MON + datetime.timedelta(days=7)
    )
    assert job.kind == JobKind.GENERATE and job.params == {}


def test_the_task_runs_the_repair(admin, rw):
    admin.post(URL, absent(rw))
    job = job_of()
    tasks.run_repair_job.func(job.pk)
    job.refresh_from_db()
    assert job.status == JobStatus.DONE


# ---- helpers of the service ----------------------------------------------------


def test_perturbation_from_params_maps_days_and_clock_hours():
    p = repair.perturbation_from_params(
        {
            "kind": "slot_removed",
            "class_id": 4,
            "days": ["MARTEDI", "VENERDI"],
            "hour": 11,
        }
    )
    assert p == Perturbation("slot_removed", None, 4, frozenset({1, 4}), 3)
    q = repair.perturbation_from_params(
        {
            "kind": "teacher_absent",
            "teacher_id": 2,
            "class_id": None,
            "days": ["LUNEDI"],
            "hour": None,
        }
    )
    assert q == Perturbation("teacher_absent", 2, None, frozenset({0}), None)


def test_current_lessons_skips_slots_that_are_no_longer_assignments(rw):
    ctx = build_week_context(MON, rw.school)
    slots = list(Slot.objects.filter(timetable=rw.tt))
    assert len(repair.current_lessons(slots, ctx)) == 60
    ctx = dataclasses.replace(ctx, assignments=ctx.assignments[1:])
    assert len(repair.current_lessons(slots, ctx)) == 60 - 4


def test_describe_names_the_perturbation(rw):
    assert (
        repair.describe(
            {
                "kind": "slot_removed",
                "class_id": rw.classes[0].pk,
                "days": ["LUNEDI"],
                "hour": 9,
            }
        )
        == "Ora eliminata: classe 1A, lunedì 9-10"
    )
    assert (
        repair.describe(
            {"kind": "class_closed", "class_id": rw.classes[2].pk, "days": ["VENERDI"]}
        )
        == "Classe chiusa: 1C, venerdì"
    )


# ---- pairings ---------------------------------------------------------------------


def test_a_paired_lesson_is_repaired_and_written_for_both_classes(admin, rw):
    ClassPairing.objects.create(
        school=rw.school,
        class_a=rw.classes[0],
        class_b=rw.classes[1],
        subject=rw.subjects[0],
        teacher=rw.teachers[0],
    )
    regen = SolveJob.objects.create(school=rw.school, week_start=MON)
    tasks.run_job(regen.pk)
    job = start(admin, absent(rw, teacher=1))
    assert job.status == JobStatus.DONE
    assert admin.post(confirm_url(job)).status_code == 200
    rw.tt.refresh_from_db()
    paired = list(
        Slot.objects.filter(timetable=rw.tt, teacher=rw.teachers[0], paired=True)
    )
    assert paired
    for s in paired:
        twin = Slot.objects.filter(
            timetable=rw.tt,
            school_class=s.paired_class,
            paired_class=s.school_class,
            teacher=s.teacher,
            day=s.day,
            start_hour=s.start_hour,
        )
        assert twin.count() == 1
    assert not [
        c for c in cells(rw) if c[1] == rw.teachers[1].pk and c[3] == "MERCOLEDI"
    ]


def test_confirm_writes_one_audit_entry_and_a_refusal_none(admin, rw):
    from scheduling.models import AuditLog

    job = start(admin, absent(rw))
    before = AuditLog.objects.count()  # the preview records nothing
    assert admin.post(confirm_url(job)).status_code == 200
    assert AuditLog.objects.count() == before + 1
    entry = AuditLog.objects.first()
    assert entry.action == "REPAIR_CONFIRMED"
    assert (entry.week_start, entry.timetable_id) == (rw.tt.week_start, rw.tt.pk)
    assert entry.user.role == "ADMIN"
    assert entry.details["job"] == job.pk
    assert admin.post(confirm_url(job)).status_code == 409  # already applied
    assert AuditLog.objects.count() == before + 1


def test_dismiss_writes_no_audit_entry(admin, rw):
    from scheduling.models import AuditLog

    job = start(admin, absent(rw))
    before = AuditLog.objects.count()
    admin.post(dismiss_url(job))
    assert AuditLog.objects.count() == before

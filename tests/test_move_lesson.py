import datetime
import re
from unittest import mock

import pytest
from django.template.defaultfilters import floatformat
from django.test import Client
from django.utils import timezone

from accounts.models import User
from availability import grid as g
from availability.models import Availability
from scheduling import moves, views
from scheduling.models import Slot, Timetable, TimetableState
from scheduling.moves import InvalidMove, MoveRejected, move_lesson
from scheduling.solver import SolveResult, SolveStatus
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
MOVE = f"/orario/{MON:%Y-%m-%d}/sposta/"
PAGE = "/orario/"
GRID = f"/orario/{MON:%Y-%m-%d}/griglia/"


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
def admin(db):
    return login("ADMIN")


@pytest.fixture
def seg(db):
    return login("SEGRETERIA")


@pytest.fixture
def w(db):
    """Two classes, Monday and Tuesday of 4 hours, the other days closed.

    Every class has two lessons a day (8 and 9), so 10-13 are free. Rossi and
    Bianchi have more hours than they are placed for: the solver would fill
    the free hours if it were allowed to.
    """

    class W:
        pass

    w = W()
    s = w.school = School.objects.create(
        name="S",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )
    for i in range(5):
        CalendarEntry.objects.create(
            school=s,
            date=MON + datetime.timedelta(days=i),
            max_hours=4 if i < 2 else 0,
            is_closure=i >= 2,
        )
    w.a = Class.objects.create(school=s, name="1A")
    w.b = Class.objects.create(school=s, name="1B")
    w.rossi = Teacher.objects.create(
        school=s, name="ROSSI MARIO", type="ASSUNTO", first_time=False
    )
    w.bianchi = Teacher.objects.create(
        school=s, name="BIANCHI ANNA", type="ASSUNTO", first_time=False
    )
    w.mat = Subject.objects.create(
        school=s, name="Matematica", kind="TEORIA", cognitive_weight="MEDIO"
    )
    w.ita = Subject.objects.create(
        school=s, name="Italiano", kind="TEORIA", cognitive_weight="MEDIO"
    )
    w.lab = Subject.objects.create(
        school=s, name="Laboratorio", kind="PRATICA", cognitive_weight="MEDIO"
    )
    w.asg = {}
    for name, teacher, klass, subject in (
        ("a_mat", w.rossi, w.a, w.mat),
        ("a_ita", w.bianchi, w.a, w.ita),
        ("b_mat", w.rossi, w.b, w.mat),
        ("b_ita", w.bianchi, w.b, w.ita),
        ("a_lab", w.bianchi, w.a, w.lab),
    ):
        w.asg[name] = Assignment.objects.create(
            school=s,
            teacher=teacher,
            school_class=klass,
            subject=subject,
            ore_totali=4,
        )
    w.tt = Timetable.objects.create(school=s, week_start=MON, version=1)

    def slot(klass, teacher, subject, day, hour, **kw):
        return Slot.objects.create(
            timetable=w.tt,
            school_class=klass,
            teacher=teacher,
            subject=subject,
            day=day,
            start_hour=hour,
            end_hour=hour + 1,
            **kw,
        )

    w.slot = slot
    w.slots = {}
    for day in ("LUNEDI", "MARTEDI"):
        w.slots[day, "a_mat"] = w.slot(w.a, w.rossi, w.mat, day, 8)
        w.slots[day, "a_ita"] = w.slot(w.a, w.bianchi, w.ita, day, 9)
        w.slots[day, "b_ita"] = w.slot(w.b, w.bianchi, w.ita, day, 8)
        w.slots[day, "b_mat"] = w.slot(w.b, w.rossi, w.mat, day, 9)
    return w


def post(client, slot, day, hour, version=None, **extra):
    data = {
        "slot_id": slot.pk if hasattr(slot, "pk") else slot,
        "giorno": day,
        "ora_inizio": hour,
        "version": version,
    }
    data.update(extra)
    return client.post(MOVE, {k: v for k, v in data.items() if v is not None})


def where(slot):
    slot.refresh_from_db()
    return slot.day, slot.start_hour, slot.end_hour


def state(w):
    w.tt.refresh_from_db()
    return (
        w.tt.version,
        w.tt.quality_score,
        w.tt.quality_level,
        w.tt.soft_conflicts,
        w.tt.conflicts,
        sorted(Slot.objects.values_list("pk", "day", "start_hour", "end_hour")),
    )


def html(response):
    return response.content.decode()


def kinds(w, **where_):
    w.tt.refresh_from_db()
    return [c for c in w.tt.conflicts if all(c.get(k) == v for k, v in where_.items())]


def cell(text, day, hour):
    m = re.search(
        rf'<td class="[^"]*" (?:data-conflict="true" )?data-day="{day}" '
        rf'data-hour="{hour}"[^>]*>(.*?)</td>',
        text,
        re.S,
    )
    assert m, f"no cell {day} {hour}"
    return m.group(0)


# ---- the happy path ---------------------------------------------------------


def test_move_to_an_empty_hour_rerenders_the_grid(w, admin):
    slot = w.slots["LUNEDI", "a_ita"]
    r = post(admin, slot, "LUNEDI", 11, 1)
    assert r.status_code == 200
    assert where(slot) == ("LUNEDI", 11, 12)
    text = html(r)
    assert 'id="timetable-body"' in text
    assert f'data-slot-id="{slot.pk}"' in cell(text, "LUNEDI", 11)
    assert "data-slot-id" not in cell(text, "LUNEDI", 9)
    assert 'data-version="2"' in text


def test_the_response_shows_the_recomputed_score_and_conflicts(w, admin):
    w.tt.quality_score, w.tt.quality_level = 12.5, "C"
    w.tt.save()
    text = html(post(admin, w.slots["LUNEDI", "a_ita"], "LUNEDI", 11, 1))
    w.tt.refresh_from_db()
    assert w.tt.quality_score != 12.5
    assert f"punteggio {floatformat(w.tt.quality_score, 1)}" in text
    assert f"Conflitti ({len(w.tt.conflicts)})" in text
    # the hole the move left in the class day is listed and marks its cell
    assert "Ora non coperta per la classe" in text
    assert 'data-conflict="true"' in cell(text, "LUNEDI", 10)
    assert w.tt.soft_conflicts >= len(w.tt.conflicts) > 0
    assert w.tt.quality_level in ("A", "B", "C")


def test_the_move_adds_and_removes_no_lesson(w, admin):
    before = Slot.objects.count()
    post(admin, w.slots["LUNEDI", "a_ita"], "LUNEDI", 11, 1)
    assert Slot.objects.count() == before
    # the free hours stay free: the solver was not allowed to fill them
    assert kinds(w, kind="classe_unfilled", giorno="LUNEDI", ore_slot=1)


def test_moving_back_restores_score_and_conflicts(w, admin):
    slot = w.slots["LUNEDI", "a_ita"]
    post(admin, slot, "LUNEDI", 10, 1)
    away = state(w)
    post(admin, slot, "MARTEDI", 13, 2)
    assert state(w)[1] != away[1] or state(w)[4] != away[4]
    r = post(admin, slot, "LUNEDI", 10, 3)
    assert r.status_code == 200
    back = state(w)
    assert back[1] == away[1]
    assert {c["chiave"] for c in back[4]} == {c["chiave"] for c in away[4]}
    assert back[0] == 4


def test_a_successful_move_bumps_the_version_exactly_once(w, admin):
    post(admin, w.slots["LUNEDI", "a_ita"], "LUNEDI", 11, 1)
    w.tt.refresh_from_db()
    assert w.tt.version == 2


def test_moving_onto_its_own_cell_is_a_no_op(w, admin):
    before = state(w)
    r = post(admin, w.slots["LUNEDI", "a_ita"], "LUNEDI", 9, 1)
    assert r.status_code == 200
    assert state(w) == before
    assert 'data-version="1"' in html(r)


# ---- conflicts: accepted, reported, lesson marked -------------------------


def conflicted(text, slot_cell):
    return 'data-conflict="true"' in cell(text, *slot_cell)


def test_teacher_double_booked_is_accepted_and_reported(w, admin):
    # Rossi is in 1B on Tuesday at 10; move his 1A lesson there.
    Slot.objects.filter(pk=w.slots["MARTEDI", "b_mat"].pk).update(
        start_hour=10, end_hour=11
    )
    r = post(admin, w.slots["MARTEDI", "a_mat"], "MARTEDI", 10, 1)
    assert r.status_code == 200
    items = kinds(w, kind="docente_double_booking", docente_id=w.rossi.pk)
    assert len(items) == 1 and items[0]["giorno"] == "MARTEDI"
    assert items[0]["ore_slot"] == 2
    assert conflicted(html(r), ("MARTEDI", 10))
    assert where(w.slots["MARTEDI", "a_mat"]) == ("MARTEDI", 10, 11)


def test_teacher_unavailable_is_accepted_and_reported(w, admin):
    w.rossi.type = "CONTRATTO"
    w.rossi.save()
    grid = g.default_grid()
    for slot in grid["martedi"]:
        slot["disponibile"] = False
    Availability.objects.create(
        school=w.school, teacher=w.rossi, week_start=MON, slots=grid
    )
    r = post(admin, w.slots["LUNEDI", "a_mat"], "MARTEDI", 10, 1)
    assert r.status_code == 200
    items = kinds(w, kind="H4", docente_id=w.rossi.pk, classe_id=w.a.pk)
    assert [i["giorno"] for i in items] == ["MARTEDI"]
    assert conflicted(html(r), ("MARTEDI", 10))


def test_a_hole_in_the_class_day_is_reported(w, admin):
    # 1A has 8 and 9 on Monday; moving the 9 to 11 leaves 9-10 empty in the block.
    r = post(admin, w.slots["LUNEDI", "a_ita"], "LUNEDI", 11, 1)
    holes = kinds(w, kind="classe_unfilled", classe_id=w.a.pk, giorno="LUNEDI")
    assert sorted(h["ore_slot"] for h in holes) == [1, 2]
    text = html(r)
    assert conflicted(text, ("LUNEDI", 9)) and conflicted(text, ("LUNEDI", 10))
    assert not conflicted(text, ("LUNEDI", 11))


def practical_day(w):
    """1A on Monday: 8 Rossi, then a three-hour practical block 9-11."""
    Slot.objects.filter(pk=w.slots["LUNEDI", "a_ita"].pk).delete()
    return [w.slot(w.a, w.bianchi, w.lab, "LUNEDI", h) for h in (9, 10, 11)]


def test_practical_block_broken_is_accepted_and_reported(w, admin):
    block = practical_day(w)
    r = post(admin, block[2], "LUNEDI", 13, 1)
    assert r.status_code == 200
    assert kinds(w, kind="H10", classe_id=w.a.pk, giorno="LUNEDI")
    text = html(r)
    assert conflicted(text, ("LUNEDI", 13))  # the practical lessons are marked
    assert conflicted(text, ("LUNEDI", 9))
    assert not conflicted(text, ("LUNEDI", 8))  # the theory lesson is not


def test_unbroken_practical_block_is_not_reported(w, admin):
    block = practical_day(w)
    r = post(admin, block[0], "LUNEDI", 12, 1)  # block 10-12 stays whole
    assert r.status_code == 200
    assert not kinds(w, kind="H10")


def test_over_the_hours_budget_is_accepted_and_reported(w, admin):
    w.asg["a_mat"].ore_totali = 1
    w.asg["a_mat"].save()
    r = post(admin, w.slots["LUNEDI", "a_mat"], "MARTEDI", 10, 1)
    assert r.status_code == 200
    items = kinds(w, kind="monte_ore_exceeded", classe_id=w.a.pk)
    assert [(i["giorno"], i["ore_slot"]) for i in items] == [("MARTEDI", 2)]
    assert conflicted(html(r), ("MARTEDI", 10))
    assert not conflicted(html(r), ("MARTEDI", 8))


# ---- rejected moves: message, nothing changes ----------------------------


def assert_rejected(w, response, status, message_part=None, before=None):
    assert response.status_code == status
    assert state(w) == before
    text = html(response)
    assert 'id="timetable-body"' in text  # always the current grid
    assert 'role="alert"' in text
    if message_part:
        assert message_part in text


def test_move_onto_an_occupied_hour_is_rejected(w, admin):
    before = state(w)
    r = post(admin, w.slots["LUNEDI", "a_mat"], "LUNEDI", 9, 1)
    assert_rejected(w, r, 409, "già occupata", before)


def test_move_onto_an_occupied_hour_of_another_day_is_rejected(w, admin):
    before = state(w)
    r = post(admin, w.slots["LUNEDI", "a_mat"], "MARTEDI", 9, 1)
    assert_rejected(w, r, 409, "già occupata", before)


def test_a_lesson_of_another_class_at_the_hour_does_not_block(w, admin):
    # 1B has a lesson on Monday at 8 and 9, 1A at 10 is free
    r = post(admin, w.slots["LUNEDI", "a_ita"], "LUNEDI", 10, 1)
    assert r.status_code == 200


def test_move_onto_a_stage_day_is_rejected(w, admin):
    CalendarEntry.objects.create(
        school=w.school, date=MON + datetime.timedelta(days=1), stage_class=w.a
    )
    before = state(w)
    r = post(admin, w.slots["LUNEDI", "a_mat"], "MARTEDI", 12, 1)
    assert_rejected(w, r, 409, "stage", before)


def test_move_onto_a_group_stage_day_is_rejected(w, admin):
    w.a.group = "PRIME"
    w.a.save()
    CalendarEntry.objects.create(
        school=w.school,
        date=MON + datetime.timedelta(days=1),
        group="PRIME",
        is_group_stage=True,
    )
    before = state(w)
    r = post(admin, w.slots["LUNEDI", "a_mat"], "MARTEDI", 12, 1)
    assert_rejected(w, r, 409, "stage", before)


def test_move_to_a_day_without_hours_is_rejected(w, admin):
    before = state(w)
    r = post(admin, w.slots["LUNEDI", "a_mat"], "MERCOLEDI", 8, 1)
    assert_rejected(w, r, 409, "non ha ore di lezione", before)


def test_a_zero_hour_day_that_is_not_a_stage_is_not_called_stage(w, admin):
    r = post(admin, w.slots["LUNEDI", "a_mat"], "GIOVEDI", 8, 1)
    assert "stage" not in html(r).lower().split('role="alert"')[1].split("</div>")[0]


BAD_INPUT = [
    {"ora_inizio": 7},
    {"ora_inizio": 14},
    {"ora_inizio": 0},
    {"ora_inizio": "abc"},
    {"ora_inizio": ""},
    {"ora_inizio": "9.5"},
    {"ora_inizio": "-1"},
    {"ora_inizio": " 9"},
    {"ora_inizio": None},
    {"giorno": "SABATO"},
    {"giorno": "DOMENICA"},
    {"giorno": "lunedi"},
    {"giorno": ""},
    {"giorno": None},
    {"slot_id": 999999},
    {"slot_id": "abc"},
    {"slot_id": ""},
    {"slot_id": None},
    {"slot_id": "-3"},
    {"version": "abc"},
    {"version": ""},
    {"version": None},
]


@pytest.mark.parametrize("override", BAD_INPUT, ids=repr)
def test_invalid_input_is_rejected_with_400(w, admin, override):
    data = {
        "slot_id": w.slots["LUNEDI", "a_mat"].pk,
        "giorno": "LUNEDI",
        "ora_inizio": 11,
        "version": 1,
    }
    data.update(override)
    before = state(w)
    r = admin.post(MOVE, {k: v for k, v in data.items() if v is not None})
    assert_rejected(w, r, 400, None, before)


def test_a_slot_of_another_weeks_timetable_is_rejected(w, admin):
    other = Timetable.objects.create(
        school=w.school, week_start=MON + datetime.timedelta(days=7), version=1
    )
    foreign = Slot.objects.create(
        timetable=other,
        school_class=w.a,
        teacher=w.rossi,
        subject=w.mat,
        day="LUNEDI",
        start_hour=8,
        end_hour=9,
    )
    before = state(w)
    r = post(admin, foreign, "LUNEDI", 11, 1)
    assert_rejected(w, r, 400, "non trovata", before)
    assert where(foreign) == ("LUNEDI", 8, 9)
    other.refresh_from_db()
    assert other.version == 1


@pytest.mark.parametrize("hour", [8, 13])
def test_hours_8_and_13_are_accepted(w, admin, hour):
    slot = w.slots["MARTEDI", "a_ita"]
    r = post(admin, slot, "MARTEDI", hour if hour != 8 else 10, 1)
    assert r.status_code == 200
    if hour == 13:
        assert where(slot) == ("MARTEDI", 13, 14)


def test_a_move_on_an_approved_week_is_rejected(w, admin):
    Timetable.objects.filter(pk=w.tt.pk).update(
        state=TimetableState.APPROVATO, approved_at=timezone.now()
    )
    before = state(w)
    r = post(admin, w.slots["LUNEDI", "a_ita"], "LUNEDI", 11, 1)
    assert_rejected(w, r, 409, "approvata", before)
    assert "data-slot-id" not in html(r)


def test_a_stale_version_gets_409_and_the_current_grid(w, admin):
    Timetable.objects.filter(pk=w.tt.pk).update(version=3)
    before = state(w)
    r = post(admin, w.slots["LUNEDI", "a_ita"], "LUNEDI", 11, 2)
    assert_rejected(w, r, 409, "modificato", before)
    assert 'data-version="3"' in html(r)
    assert f'data-slot-id="{w.slots["LUNEDI", "a_ita"].pk}"' in cell(
        html(r), "LUNEDI", 9
    )


def test_a_newer_version_than_the_current_one_is_rejected_too(w, admin):
    before = state(w)
    r = post(admin, w.slots["LUNEDI", "a_ita"], "LUNEDI", 11, 7)
    assert_rejected(w, r, 409, "modificato", before)


def test_a_version_change_while_waiting_for_the_lock_is_rejected(w):
    real = moves.apply_change

    def bumped(timetable_id, fn):
        Timetable.objects.filter(pk=timetable_id).update(version=2)
        return real(timetable_id, fn)

    with mock.patch.object(moves, "apply_change", bumped):
        with pytest.raises(MoveRejected) as exc:
            move_lesson(w.tt.pk, w.slots["LUNEDI", "a_ita"].pk, "LUNEDI", 11, 1)
    assert exc.value.status == 409
    assert where(w.slots["LUNEDI", "a_ita"]) == ("LUNEDI", 9, 10)


def test_a_lesson_without_assignment_is_rejected(w, admin):
    w.asg["a_ita"].delete()
    before = state(w)
    r = post(admin, w.slots["LUNEDI", "a_ita"], "LUNEDI", 11, 1)
    assert_rejected(w, r, 409, "assegnazione", before)


def test_unknown_week_and_missing_timetable_are_404(w, admin):
    assert admin.post("/orario/2026-10-13/sposta/", {}).status_code == 404
    Timetable.objects.all().delete()
    assert admin.post(MOVE, {}).status_code == 404


def test_the_endpoint_takes_post_only(w, admin):
    assert admin.get(MOVE).status_code == 405


# ---- atomicity -------------------------------------------------------------


def test_if_the_evaluation_raises_nothing_is_saved(w):
    before = state(w)
    with mock.patch.object(moves, "solve_exclusive", side_effect=RuntimeError("x")):
        with pytest.raises(RuntimeError):
            move_lesson(w.tt.pk, w.slots["LUNEDI", "a_ita"].pk, "LUNEDI", 11, 1)
    assert state(w) == before


def test_if_the_evaluation_has_no_solution_nothing_is_saved(w, admin):
    before = state(w)
    with mock.patch.object(
        moves, "solve_exclusive", return_value=SolveResult(SolveStatus.UNKNOWN)
    ):
        r = post(admin, w.slots["LUNEDI", "a_ita"], "LUNEDI", 11, 1)
    assert_rejected(w, r, 409, "rivalutare", before)


def test_if_writing_the_result_fails_the_slot_move_is_rolled_back(w):
    before = state(w)
    with mock.patch.object(Timetable, "save", side_effect=RuntimeError("disk")):
        with pytest.raises(RuntimeError):
            move_lesson(w.tt.pk, w.slots["LUNEDI", "a_ita"].pk, "LUNEDI", 11, 1)
    assert state(w) == before


def test_the_evaluation_pins_every_lesson_with_the_moved_one_in_place(w):
    seen = {}
    real = moves.solve_exclusive

    def spy(ctx, pinned=None):
        seen["pinned"] = pinned
        seen["ctx"] = ctx
        return real(ctx, pinned=pinned)

    with mock.patch.object(moves, "solve_exclusive", spy):
        move_lesson(w.tt.pk, w.slots["LUNEDI", "a_ita"].pk, "LUNEDI", 11, 1)
    cells = {(p.assignment_id, p.day, p.hour) for p in seen["pinned"]}
    assert len(cells) == 8
    assert (w.asg["a_ita"].pk, 0, 3) in cells  # moved: Monday 11
    assert (w.asg["a_ita"].pk, 0, 1) not in cells
    assert (w.asg["a_mat"].pk, 0, 0) in cells  # the others stay
    assert seen["ctx"].availability_relaxed is True
    assert set(seen["ctx"].weekly_caps) == {a.pk for a in (w.rossi, w.bianchi)}


# ---- paired lessons --------------------------------------------------------


def paired(w, rows=2):
    ClassPairing.objects.create(
        school=w.school,
        class_a=w.a,
        class_b=w.b,
        subject=w.mat,
        teacher=w.rossi,
    )
    Slot.objects.filter(subject=w.mat).delete()
    pa = w.slot(w.a, w.rossi, w.mat, "LUNEDI", 8, paired=True, paired_class=w.b)
    pb = (
        w.slot(w.b, w.rossi, w.mat, "LUNEDI", 8, paired=True, paired_class=w.a)
        if rows == 2
        else None
    )
    return pa, pb


@pytest.mark.parametrize("from_partner", [False, True])
def test_moving_a_paired_lesson_moves_both_classes(w, admin, from_partner):
    pa, pb = paired(w)
    r = post(admin, pb if from_partner else pa, "LUNEDI", 11, 1)
    assert r.status_code == 200
    assert where(pa) == where(pb) == ("LUNEDI", 11, 12)
    w.tt.refresh_from_db()
    assert w.tt.version == 2
    text = html(r)
    assert "tt-subject" in cell(text, "LUNEDI", 11)
    # both rows of the grid show the lesson at 11
    assert len(re.findall(r'data-day="LUNEDI" data-hour="11"[^>]*><div', text)) == 2


def test_moving_a_single_row_paired_lesson_shows_in_both_classes(w, admin):
    pa, _ = paired(w, rows=1)
    r = post(admin, pa, "LUNEDI", 11, 1)
    assert r.status_code == 200
    assert where(pa) == ("LUNEDI", 11, 12)
    assert len(re.findall(r'data-day="LUNEDI" data-hour="11"[^>]*><div', html(r))) == 2


def test_a_paired_move_is_rejected_when_the_partner_class_is_busy(w, admin):
    pa, pb = paired(w)
    # 1B has Bianchi at Monday 10; 1A is free there
    w.slot(w.b, w.bianchi, w.ita, "LUNEDI", 10)
    before = state(w)
    r = post(admin, pa, "LUNEDI", 10, 1)
    assert_rejected(w, r, 409, "già occupata", before)
    assert where(pa) == where(pb) == ("LUNEDI", 8, 9)


def test_a_paired_move_is_rejected_on_a_stage_day_of_the_partner(w, admin):
    pa, pb = paired(w)
    CalendarEntry.objects.create(
        school=w.school, date=MON + datetime.timedelta(days=1), stage_class=w.b
    )
    before = state(w)
    r = post(admin, pa, "MARTEDI", 12, 1)
    assert_rejected(w, r, 409, "stage", before)


# ---- who may move ----------------------------------------------------------


def test_segreteria_gets_403_and_nothing_changes(w, seg):
    before = state(w)
    r = post(seg, w.slots["LUNEDI", "a_ita"], "LUNEDI", 11, 1)
    assert r.status_code == 403
    assert state(w) == before


def test_anonymous_is_sent_to_login(w, client):
    before = state(w)
    r = post(client, w.slots["LUNEDI", "a_ita"], "LUNEDI", 11, 1)
    assert r.status_code == 302 and r.url.startswith("/login/")
    assert state(w) == before


def draggable_markers(text):
    return [m for m in ("data-slot-id", "tt-draggable", "data-drop") if m in text]


def test_admin_grid_is_draggable(w, admin):
    text = html(admin.get(PAGE))
    assert draggable_markers(text) == ["data-slot-id", "tt-draggable", "data-drop"]
    assert text.count("data-slot-id") == 8
    assert f'data-move-url="{MOVE}"' in text
    assert 'data-movable="true"' in text
    assert "vendor/sortable-1.15.6.min.js" in text
    assert "js/timetable_move.js" in text
    assert f'data-drop="classe-{w.a.pk}"' in text


def test_segreteria_grid_has_nothing_draggable(w, seg):
    text = html(seg.get(PAGE))
    assert "tt-lesson" in text
    assert draggable_markers(text) == []
    assert "data-move-url" not in text and "data-csrf" not in text
    assert "sortable" not in text.lower() and "timetable_move" not in text
    assert draggable_markers(html(seg.get(GRID))) == []


def test_an_approved_week_has_nothing_draggable_even_for_admin(w, admin):
    Timetable.objects.filter(pk=w.tt.pk).update(
        state=TimetableState.APPROVATO, approved_at=timezone.now()
    )
    assert draggable_markers(html(admin.get(PAGE))) == []
    assert draggable_markers(html(admin.get(GRID))) == []


def test_stage_cells_are_not_drop_targets(w, admin):
    CalendarEntry.objects.create(
        school=w.school, date=MON + datetime.timedelta(days=1), stage_class=w.a
    )
    text = html(admin.get(PAGE))
    stage = re.findall(r'<td class="tt-stage"[^>]*>', text)
    assert stage and all("data-drop" not in s for s in stage)


def test_the_grid_poll_after_a_move_carries_the_new_version(w, admin):
    post(admin, w.slots["LUNEDI", "a_ita"], "LUNEDI", 11, 1)
    assert admin.get(GRID + "?versione=1").status_code == 200
    assert admin.get(GRID + "?versione=2").status_code == 204


# ---- the vendored file ------------------------------------------------------


def test_sortablejs_is_vendored_with_its_version_and_licence():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "static"
    lib = root / "vendor" / "sortable-1.15.6.min.js"
    assert lib.read_text().startswith("/*! Sortable 1.15.6 - MIT")
    readme = (root / "vendor" / "README.txt").read_text()
    assert "sortable-1.15.6.min.js" in readme and "MIT" in readme
    js = (root / "js" / "timetable_move.js").read_text()
    assert "http://" not in js and "https://" not in js


def test_invalidmove_is_a_400_and_a_rejection_a_409():
    assert InvalidMove("x").status == 400
    assert MoveRejected("x").status == 409

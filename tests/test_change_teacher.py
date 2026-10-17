import datetime
import re
from unittest import mock

import pytest

from availability import grid as g
from availability.models import Availability
from ledger.models import LedgerEntry
from scheduling import teacher_change, views
from scheduling.models import (
    AuditLog,
    ConflictDecision,
    Slot,
    Timetable,
    TimetableState,
)
from scheduling.solver import SolveResult, SolveStatus
from school.models import Assignment, ClassPairing, Teacher

D = datetime.date
MON = D(2026, 10, 12)
WEEK = f"/orario/{MON:%Y-%m-%d}"
CHANGE = f"{WEEK}/cambia-docente/"
PAGE = "/orario/"


def panel(slot):
    return f"{WEEK}/docenti/{slot.pk}/"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(views, "today", lambda: D(2026, 10, 14))


@pytest.fixture
def verdi(w):
    """A third matematica teacher of 1A (and of 1B)."""
    teacher = Teacher.objects.create(
        school=w.school, name="VERDI LUCA", type="ASSUNTO", first_time=False
    )
    for klass in (w.a, w.b):
        Assignment.objects.create(
            school=w.school,
            teacher=teacher,
            school_class=klass,
            subject=w.mat,
            ore_totali=4,
        )
    return teacher


def change(client, slot, teacher, version=1, **extra):
    data = {
        "slot_id": getattr(slot, "pk", slot),
        "docente": getattr(teacher, "pk", teacher),
        "version": version,
    }
    data.update(extra)
    return client.post(CHANGE, {k: v for k, v in data.items() if v is not None})


def state(w):
    w.tt.refresh_from_db()
    return (
        w.tt.version,
        w.tt.quality_score,
        w.tt.quality_level,
        w.tt.soft_conflicts,
        w.tt.conflicts,
        sorted(Slot.objects.values_list("pk", "teacher_id", "day", "start_hour")),
        AuditLog.objects.count(),
    )


def html(response):
    return response.content.decode()


def kinds(w, **where_):
    w.tt.refresh_from_db()
    return [c for c in w.tt.conflicts if all(c.get(k) == v for k, v in where_.items())]


def refused(w, response, status, message, before):
    assert response.status_code == status
    assert state(w) == before
    text = html(response)
    assert 'id="timetable-body"' in text
    assert 'role="alert"' in text
    assert message in text


def lesson(w):
    return w.slots["LUNEDI", "a_mat"]


def pair_up(w, teacher=None):
    Slot.objects.filter(teacher=w.rossi).delete()
    ClassPairing.objects.create(
        school=w.school,
        class_a=w.a,
        class_b=w.b,
        subject=w.mat,
        teacher=teacher or w.rossi,
    )
    rows = {}
    for day in ("LUNEDI", "MARTEDI"):
        rows[day, "a"] = w.slot(
            w.a, w.rossi, w.mat, day, 8, paired=True, paired_class=w.b
        )
        rows[day, "b"] = w.slot(
            w.b, w.rossi, w.mat, day, 8, paired=True, paired_class=w.a
        )
    return rows


# ---- the panel ---------------------------------------------------------------


def test_panel_lists_the_other_candidates_with_remaining_hours(w, admin, verdi):
    other = Teacher.objects.create(
        school=w.school, name="ALFA ZITA", type="ASSUNTO", first_time=False
    )
    Assignment.objects.create(
        school=w.school,
        teacher=other,
        school_class=w.a,
        subject=w.mat,
        ore_totali=1,
    )
    r = admin.get(panel(lesson(w)))
    assert r.status_code == 200
    text = html(r)
    names = re.findall(r'class="tt-pair-teacher">([^<]*)<', text)
    assert names == ["ALFA ZITA", "VERDI LUCA"]  # not ROSSI (current), BIANCHI (ita)
    hours = re.findall(r"ore rimanenti: (-?\d+)", text)
    assert hours == ["1", "4"]
    assert f'name="slot_id" value="{lesson(w).pk}"' in text
    assert f'name="docente" value="{verdi.pk}"' in text
    assert 'name="version" value="1"' in text


def test_panel_leaves_out_inactive_and_other_class_teachers(w, admin, verdi):
    verdi.active = False
    verdi.save()
    text = html(admin.get(panel(lesson(w))))
    assert "VERDI" not in text and "BIANCHI" not in text
    assert "Nessun altro docente" in text


def test_panel_remaining_hours_count_the_lessons_placed(w, admin, verdi):
    w.slot(w.a, verdi, w.mat, "LUNEDI", 10)
    text = html(admin.get(panel(lesson(w))))
    assert re.findall(r"ore rimanenti: (-?\d+)", text) == ["3"]


def test_paired_panel_needs_partner_assignment_and_pairing(w, admin, verdi):
    rows = pair_up(w)
    assert "VERDI" not in html(admin.get(panel(rows["LUNEDI", "a"])))  # no pairing
    ClassPairing.objects.create(
        school=w.school, class_a=w.a, class_b=w.b, subject=w.mat, teacher=verdi
    )
    for key in ("a", "b"):
        assert "VERDI" in html(admin.get(panel(rows["LUNEDI", key])))
    Assignment.objects.filter(teacher=verdi, school_class=w.b).delete()
    assert "VERDI" not in html(admin.get(panel(rows["LUNEDI", "a"])))


def test_panel_of_an_approved_week_shows_the_message(w, admin, verdi):
    w.tt.state = TimetableState.APPROVATO
    w.tt.save()
    r = admin.get(panel(lesson(w)))
    assert r.status_code == 409
    assert "approvata" in html(r) and "VERDI" not in html(r)


def test_panel_of_an_unknown_slot_is_a_400(w, admin):
    r = admin.get(f"{WEEK}/docenti/999999/")
    assert r.status_code == 400
    assert "non trovata" in html(r)


def test_panel_of_another_weeks_slot_is_a_400(w, admin):
    other = Timetable.objects.create(
        school=w.school, week_start=MON + datetime.timedelta(days=7)
    )
    s = Slot.objects.create(
        timetable=other,
        school_class=w.a,
        teacher=w.rossi,
        subject=w.mat,
        day="LUNEDI",
        start_hour=8,
        end_hour=9,
    )
    assert admin.get(panel(s)).status_code == 400


def test_panel_permissions_and_methods(w, seg, admin):
    from django.test import Client

    assert seg.get(panel(lesson(w))).status_code == 403
    r = Client().get(panel(lesson(w)))
    assert r.status_code == 302 and "login" in r["Location"]
    assert admin.post(panel(lesson(w))).status_code == 405


# ---- the change --------------------------------------------------------------


def test_change_gives_the_lesson_to_the_new_teacher(w, admin, verdi):
    s = lesson(w)
    r = change(admin, s, verdi)
    assert r.status_code == 200
    text = html(r)
    assert 'id="timetable-body"' in text and "VERDI" in text
    s.refresh_from_db()
    assert s.teacher_id == verdi.pk
    assert (s.day, s.start_hour, s.end_hour) == ("LUNEDI", 8, 9)
    assert (s.school_class_id, s.subject_id) == (w.a.pk, w.mat.pk)


def test_change_bumps_the_version_once_and_keeps_the_slots(w, admin, verdi):
    count = Slot.objects.count()
    change(admin, lesson(w), verdi)
    w.tt.refresh_from_db()
    assert w.tt.version == 2
    assert Slot.objects.count() == count


def test_change_stores_a_fresh_evaluation(w, admin, verdi):
    from scheduling import moves

    real, seen = moves.solve_exclusive, []

    def spy(*args, **kwargs):
        seen.append(real(*args, **kwargs))
        return seen[-1]

    with mock.patch("scheduling.moves.solve_exclusive", spy):
        change(admin, lesson(w), verdi)
    result = seen[0]
    w.tt.refresh_from_db()
    assert w.tt.quality_score == result.quality_score
    assert w.tt.quality_level == result.quality_level
    assert w.tt.soft_conflicts == result.n_soft_conflicts
    assert w.tt.conflicts == result.conflict_items


def test_the_pin_uses_the_new_teachers_assignment(w, admin, verdi):
    with mock.patch("scheduling.moves.solve_exclusive") as solve:
        solve.return_value = SolveResult(SolveStatus.INFEASIBLE)
        change(admin, lesson(w), verdi)
    pinned = solve.call_args.kwargs["pinned"]
    mine = Assignment.objects.get(teacher=verdi, school_class=w.a).pk
    theirs = w.asg["a_mat"].pk
    ids = {p.assignment_id for p in pinned}
    assert mine in ids
    # the only remaining a_mat pin is the Tuesday lesson of the old teacher
    assert sum(p.assignment_id == theirs for p in pinned) == 1


def test_paired_change_moves_both_rows_from_either_side(w, admin, verdi):
    ClassPairing.objects.create(
        school=w.school, class_a=w.a, class_b=w.b, subject=w.mat, teacher=verdi
    )
    rows = pair_up(w)
    r = change(admin, rows["LUNEDI", "b"], verdi)
    assert r.status_code == 200
    for key in ("a", "b"):
        rows["LUNEDI", key].refresh_from_db()
        assert rows["LUNEDI", key].teacher_id == verdi.pk
        rows["MARTEDI", key].refresh_from_db()
        assert rows["MARTEDI", key].teacher_id == w.rossi.pk
    w.tt.refresh_from_db()
    assert w.tt.version == 2
    r = change(admin, rows["MARTEDI", "a"], verdi, version=2)
    assert r.status_code == 200
    assert Slot.objects.filter(teacher=verdi).count() == 4


def test_paired_change_without_the_partner_assignment_is_409(w, admin, verdi):
    ClassPairing.objects.create(
        school=w.school, class_a=w.a, class_b=w.b, subject=w.mat, teacher=verdi
    )
    rows = pair_up(w)
    Assignment.objects.filter(teacher=verdi, school_class=w.b).delete()
    before = state(w)
    refused(
        w, change(admin, rows["LUNEDI", "a"], verdi), 409, "classe abbinata", before
    )


def test_paired_change_without_the_pairing_is_409(w, admin, verdi):
    rows = pair_up(w)
    before = state(w)
    refused(w, change(admin, rows["LUNEDI", "a"], verdi), 409, "abbinamento", before)


# ---- rule violations are conflicts -------------------------------------------


def test_a_double_booked_new_teacher_is_reported(w, admin, verdi):
    w.slots["LUNEDI", "b_ita"].delete()
    w.slot(w.b, verdi, w.mat, "LUNEDI", 8)
    r = change(admin, lesson(w), verdi)
    assert r.status_code == 200
    items = kinds(w, kind="docente_double_booking", docente_id=verdi.pk)
    assert [i["giorno"] for i in items] == ["LUNEDI"]
    assert 'data-conflict="true"' in html(r)


def test_an_unavailable_new_teacher_is_reported(w, admin, verdi):
    verdi.type = "CONTRATTO"
    verdi.save()
    grid = g.default_grid()
    for slot in grid["lunedi"]:
        slot["disponibile"] = False
    Availability.objects.create(
        school=w.school, teacher=verdi, week_start=MON, slots=grid
    )
    r = change(admin, lesson(w), verdi)
    assert r.status_code == 200
    items = kinds(w, kind="H4", docente_id=verdi.pk, classe_id=w.a.pk)
    assert [i["giorno"] for i in items] == ["LUNEDI"]


def test_over_the_hours_budget_is_reported(w, admin, verdi):
    Assignment.objects.filter(teacher=verdi, school_class=w.a).update(ore_totali=0)
    r = change(admin, lesson(w), verdi)
    assert r.status_code == 200
    assert len(kinds(w, kind="monte_ore_exceeded", classe_id=w.a.pk)) == 1


# ---- audit, no-op, ledger ----------------------------------------------------


def test_one_audit_row_records_the_change(w, admin, verdi):
    change(admin, lesson(w), verdi)
    entry = AuditLog.objects.get()
    assert entry.action == "MODIFY_SLOT"
    assert entry.details == {
        "azione": "cambia_docente",
        "slot": lesson(w).pk,
        "da": w.rossi.pk,
        "a": verdi.pk,
    }
    assert entry.user.role == "ADMIN"


def test_the_same_teacher_is_a_no_op(w, admin):
    before = state(w)
    r = change(admin, lesson(w), w.rossi)
    assert r.status_code == 200
    assert state(w) == before
    assert 'id="timetable-body"' in html(r)


def test_ledger_and_decisions_are_untouched(w, admin, verdi):
    ConflictDecision.objects.create(
        timetable=w.tt, chiave=f"x:{w.rossi.pk}", action="APPROVATO"
    )
    ledger = list(LedgerEntry.objects.values_list("pk", flat=True))
    change(admin, lesson(w), verdi)
    assert list(LedgerEntry.objects.values_list("pk", flat=True)) == ledger
    assert ConflictDecision.objects.count() == 1


# ---- refusals ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["slot_id", "docente", "version"])
@pytest.mark.parametrize("bad", [None, "abc", "-1", ""])
def test_bad_input_is_a_400_and_changes_nothing(w, admin, verdi, name, bad):
    before = state(w)
    r = change(admin, lesson(w), verdi, **{name: bad})
    if bad is None:
        r = admin.post(
            CHANGE,
            {
                k: v
                for k, v in {
                    "slot_id": lesson(w).pk,
                    "docente": verdi.pk,
                    "version": 1,
                }.items()
                if k != name
            },
        )
    refused(w, r, 400, "Campo non valido", before)


def test_unknown_slot_and_other_weeks_slot_are_400(w, admin, verdi):
    before = state(w)
    refused(w, change(admin, 999999, verdi), 400, "non trovata", before)
    other = Timetable.objects.create(
        school=w.school, week_start=MON + datetime.timedelta(days=7)
    )
    s = Slot.objects.create(
        timetable=other,
        school_class=w.a,
        teacher=w.rossi,
        subject=w.mat,
        day="LUNEDI",
        start_hour=8,
        end_hour=9,
    )
    before = state(w)
    refused(w, change(admin, s, verdi), 400, "non trovata", before)


def test_unknown_inactive_or_unassigned_teacher_is_400(w, admin, verdi):
    before = state(w)
    refused(w, change(admin, lesson(w), 999999), 400, "assegnazione", before)
    refused(w, change(admin, lesson(w), w.bianchi), 400, "assegnazione", before)
    verdi.active = False
    verdi.save()
    refused(w, change(admin, lesson(w), verdi), 400, "assegnazione", before)


def test_stale_version_is_409(w, admin, verdi):
    before = state(w)
    refused(w, change(admin, lesson(w), verdi, version=0), 409, "modificato", before)


def test_version_changing_before_the_lock_is_409(w, admin, verdi):
    from scheduling import services

    real = services.apply_change

    def racing(timetable_id, fn):
        Timetable.objects.filter(pk=timetable_id).update(version=5)
        return real(timetable_id, fn)

    before_slots = sorted(Slot.objects.values_list("pk", "teacher_id"))
    with mock.patch("scheduling.teacher_change.apply_change", racing):
        r = change(admin, lesson(w), verdi)
    assert r.status_code == 409
    assert "modificato" in html(r)
    assert sorted(Slot.objects.values_list("pk", "teacher_id")) == before_slots
    assert AuditLog.objects.count() == 0


def test_approved_week_is_refused(w, admin, verdi):
    w.tt.state = TimetableState.APPROVATO
    w.tt.save()
    before = state(w)
    refused(w, change(admin, lesson(w), verdi), 409, "approvata", before)


def test_failed_evaluation_rolls_back(w, admin, verdi):
    before = state(w)
    with mock.patch(
        "scheduling.moves.solve_exclusive",
        return_value=SolveResult(SolveStatus.INFEASIBLE),
    ):
        r = change(admin, lesson(w), verdi)
    refused(w, r, 409, "Impossibile rivalutare", before)


def test_audit_failure_rolls_back(w, admin, verdi):
    before = state(w)
    with (
        mock.patch("scheduling.audit.record_audit", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        teacher_change.change_teacher(w.tt.pk, lesson(w).pk, verdi.pk, 1)
    assert state(w) == before
    assert lesson(w).__class__.objects.get(pk=lesson(w).pk).teacher_id == w.rossi.pk


def test_permissions_and_methods(w, seg, admin, verdi):
    from django.test import Client

    before = state(w)
    assert change(seg, lesson(w), verdi).status_code == 403
    r = change(Client(), lesson(w), verdi)
    assert r.status_code == 302 and "login" in r["Location"]
    assert state(w) == before
    assert admin.get(CHANGE).status_code == 405


# ---- grid markup -------------------------------------------------------------


def test_admin_sees_one_control_per_lesson_card(w, admin):
    text = html(admin.get(PAGE + f"?settimana={MON:%Y-%m-%d}"))
    lessons = Slot.objects.count()
    assert text.count("Cambia docente</button>") == lessons
    assert f'hx-get="{panel(lesson(w))}"' in text
    assert 'aria-label="Cambia docente: Matematica"' in text
    assert 'hx-target="#tt-assign-panel"' in text
    assert text.count(">assegna</button>") > 0


def test_segreteria_and_approved_weeks_show_none(w, seg, admin):
    assert "Cambia docente</button>" not in html(seg.get(PAGE))
    w.tt.state = TimetableState.APPROVATO
    w.tt.save()
    assert "Cambia docente</button>" not in html(admin.get(PAGE))

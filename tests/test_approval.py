import datetime
import threading
from unittest import mock

import pytest
from django.db import connection
from django.test import Client

from accounts.models import User
from ledger.models import LedgerEntry
from projects.permissions import PREDICATES, can_approve, can_undo_approval
from scheduling import audit
from scheduling.context import build_week_context
from scheduling.models import Slot, Timetable, TimetableState
from scheduling.services import approve_timetable, undo_approval
from school.assignment_hours import adjust_delivered_hours, delivered_hours_map
from school.models import Assignment, Class

D = datetime.date
MON = D(2026, 10, 12)
URL = f"/orario/{MON:%Y-%m-%d}/approva/"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    from scheduling import views

    monkeypatch.setattr(views, "today", lambda: D(2026, 10, 14))


@pytest.fixture
def user(db):
    return User.objects.create_user("boss@example.com", "pw-long-enough", "ADMIN")


@pytest.fixture
def audit_mock():
    with mock.patch("scheduling.audit.record_audit") as m:
        yield m


def entries():
    return LedgerEntry.objects.all()


# --- ledger model ---------------------------------------------------------


def make_entry(w):
    return LedgerEntry.objects.create(
        assignment=w.asg["a_mat"], timetable=w.tt, week=MON, hours=1, kind="DELIVERED"
    )


def test_entry_save_update_refused(w):
    entry = make_entry(w)
    entry.hours = 5
    with pytest.raises(NotImplementedError):
        entry.save()


def test_entry_queryset_update_refused(w):
    make_entry(w)
    with pytest.raises(NotImplementedError):
        LedgerEntry.objects.update(hours=9)


def test_entry_instance_delete_refused(w):
    with pytest.raises(NotImplementedError):
        make_entry(w).delete()


def test_entry_queryset_delete_refused(w):
    make_entry(w)
    with pytest.raises(NotImplementedError):
        LedgerEntry.objects.all().delete()


# --- approve_timetable ----------------------------------------------------


def test_approve_writes_one_entry_per_lesson(w, user, audit_mock):
    result = approve_timetable(w.tt.pk, user)
    assert result == {
        "ore_scalate": 8,
        "assegnazioni_aggiornate": 4,
        "senza_monte_ore": 0,
    }
    assert entries().count() == 8
    assert {(e.hours, e.kind, e.week, e.timetable_id) for e in entries()} == {
        (1, "DELIVERED", MON, w.tt.pk)
    }
    w.tt.refresh_from_db()
    assert w.tt.state == TimetableState.APPROVATO and w.tt.approved_at
    assert w.tt.version == 2
    audit_mock.assert_called_once_with(audit.SCHEDULE_APPROVED, user, MON, result)


def test_paired_lesson_writes_for_both_classes(w, user):
    Slot.objects.all().delete()
    w.slot(w.a, w.rossi, w.mat, "LUNEDI", 8, paired=True, paired_class=w.b)
    result = approve_timetable(w.tt.pk, user)
    assert result["ore_scalate"] == 2 and result["assegnazioni_aggiornate"] == 2
    delivered = delivered_hours_map(w.school)
    for name in ("a_mat", "b_mat"):
        a = w.asg[name]
        assert a.ore_totali - delivered[a.pk] == 3


def test_lesson_without_assignment_is_counted(w, user):
    Assignment.objects.filter(pk=w.asg["a_mat"].pk).delete()
    result = approve_timetable(w.tt.pk, user)
    assert result["senza_monte_ore"] == 2 and result["ore_scalate"] == 6
    w.tt.refresh_from_db()
    assert w.tt.state == TimetableState.APPROVATO


def test_failure_rolls_everything_back(w, user):
    with mock.patch("scheduling.audit.record_audit", side_effect=RuntimeError):
        with pytest.raises(RuntimeError):
            approve_timetable(w.tt.pk, user)
    w.tt.refresh_from_db()
    assert not entries().exists()
    assert w.tt.state == TimetableState.BOZZA and w.tt.version == 1
    assert w.tt.approved_at is None


def test_repeat_is_idempotent(w, user, audit_mock):
    approve_timetable(w.tt.pk, user)
    w.tt.refresh_from_db()
    approved_at, version = w.tt.approved_at, w.tt.version
    audit_mock.reset_mock()
    assert approve_timetable(w.tt.pk, user)["ore_scalate"] == 0
    w.tt.refresh_from_db()
    assert (w.tt.approved_at, w.tt.version) == (approved_at, version)
    assert entries().count() == 8
    audit_mock.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_simultaneous_approvals_write_one_set(w, user):
    barrier = threading.Barrier(2)
    errors = []

    def run():
        try:
            barrier.wait()
            approve_timetable(w.tt.pk, user)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert entries().count() == 8
    w.tt.refresh_from_db()
    assert w.tt.version == 2


def test_overdelivered_assignment_does_not_fail(w, user):
    Assignment.objects.filter(pk=w.asg["a_mat"].pk).update(ore_totali=1)
    approve_timetable(w.tt.pk, user)
    ctx = build_week_context(D(2026, 10, 19), w.school)
    by_id = {a.id: a for a in ctx.assignments}
    assert by_id[w.asg["a_mat"].pk].residual_hours == 0
    assert w.asg["a_mat"].pk not in ctx.targets


def test_approving_week_one_lowers_next_week_target(w, user):
    week2 = D(2026, 10, 19)
    key = w.asg["a_mat"].pk
    Assignment.objects.filter(pk=key).update(ore_totali=40)
    before = build_week_context(week2, w.school)
    approve_timetable(w.tt.pk, user)
    after = build_week_context(week2, w.school)
    by_id = {a.id: a for a in after.assignments}
    assert by_id[key].residual_hours == 38
    assert before.target_sources[key] != after.target_sources[key]


def test_approved_week_lowers_next_week_target_value(w, user):
    # 36 weeks from week 2 to the year end: 73 // 36 = 2, 71 // 36 = 1
    week2 = D(2026, 10, 19)
    key = w.asg["a_mat"].pk
    Assignment.objects.filter(pk=key).update(ore_totali=73)
    assert build_week_context(week2, w.school).targets[key] == 2
    approve_timetable(w.tt.pk, user)
    after = build_week_context(week2, w.school)
    assert after.targets[key] == 1
    assert after.target_sources[key] == "residuo 71 ore su 36 settimane"


def _next_week_state(w):
    ctx = build_week_context(D(2026, 10, 19), w.school)
    residual = {a.id: a.residual_hours for a in ctx.assignments}
    return dict(ctx.targets), residual


def test_approval_drops_residual_hours_by_lessons_approved(w, user):
    key = w.asg["a_mat"].pk
    Assignment.objects.filter(pk=key).update(ore_totali=73)
    _, before = _next_week_state(w)
    approve_timetable(w.tt.pk, user)
    _, after = _next_week_state(w)
    lessons = Slot.objects.filter(
        timetable=w.tt, school_class=w.a, subject=w.mat
    ).count()
    assert lessons == 2
    assert before[key] == 73 and after[key] == 71


def test_paired_lesson_lowers_both_classes_in_next_week_context(w, user):
    Slot.objects.all().delete()
    w.slot(w.a, w.rossi, w.mat, "LUNEDI", 8, paired=True, paired_class=w.b)
    keys = [w.asg["a_mat"].pk, w.asg["b_mat"].pk]
    Assignment.objects.filter(pk__in=keys).update(ore_totali=72)
    targets_before, residual_before = _next_week_state(w)
    approve_timetable(w.tt.pk, user)
    targets_after, residual_after = _next_week_state(w)
    for key in keys:
        assert residual_before[key] == 72 and residual_after[key] == 71
        assert targets_before[key] == 2 and targets_after[key] == 1


def test_undo_restores_next_week_target_and_residual(w, user):
    key = w.asg["a_mat"].pk
    Assignment.objects.filter(pk=key).update(ore_totali=73)
    before = _next_week_state(w)
    approve_timetable(w.tt.pk, user)
    assert _next_week_state(w) != before
    undo_approval(w.tt.pk, user, "errore")
    assert _next_week_state(w) == before
    assert before[0][key] == 2 and before[1][key] == 73


def test_assignable_pairs_remaining_drops_after_approval(w, user):
    from scheduling.assign import assignable_pairs

    week2 = Timetable.objects.create(school=w.school, week_start=WEEK2, version=1)

    def remaining():
        return {
            (r["teacher_id"], r["subject_id"]): r["remaining"]
            for r in assignable_pairs(w.school, w.a, week2)
        }

    before = remaining()
    approve_timetable(w.tt.pk, user)
    after = remaining()
    key = (w.rossi.pk, w.mat.pk)
    assert before[key] - after[key] == 2


def test_only_approve_timetable_sets_approved_state():
    import pathlib
    import re

    pattern = re.compile(
        r"\.state\s*=[^=\n]*APPROVATO|update\([^)]*state=[^,)]*APPROVATO", re.DOTALL
    )
    skipped = {"migrations", "tests", ".venv", "node_modules", "frontend"}
    hits = []
    for path in pathlib.Path(".").rglob("*.py"):
        if skipped & set(path.parts):
            continue
        hits += [path for _ in pattern.findall(path.read_text())]
    assert hits == [pathlib.Path("scheduling/services.py")]
    body = pathlib.Path("scheduling/services.py").read_text()
    approve_body = body.split("def approve_timetable")[1].split("def undo_approval")[0]
    assert "BOZZA" not in approve_body


# --- endpoint -------------------------------------------------------------


def admin_client(user):
    c = Client()
    c.force_login(user)
    return c


def test_admin_approves(w, user):
    r = admin_client(user).post(URL)
    assert r.status_code == 200
    w.tt.refresh_from_db()
    assert w.tt.state == TimetableState.APPROVATO and entries().count() == 8


def test_secretary_forbidden(w, seg):
    assert seg.post(URL).status_code == 403
    assert not entries().exists()
    w.tt.refresh_from_db()
    assert w.tt.state == TimetableState.BOZZA


def test_anonymous_redirected(w):
    r = Client().post(URL)
    assert r.status_code == 302 and "login" in r["Location"]
    assert not entries().exists()


def test_can_approve_is_listed_and_admin_only(user, seg):
    assert can_approve in PREDICATES
    assert can_approve(user) and not can_approve(None)
    assert not can_approve(User.objects.get(email="segreteria@example.com"))


def test_absent_week_is_404(w, user):
    Timetable.objects.all().delete()
    assert admin_client(user).post(URL).status_code == 404
    assert not Timetable.objects.exists()


def test_empty_week_needs_confirm(w, user):
    Slot.objects.all().delete()
    c = admin_client(user)
    r = c.post(URL)
    assert r.status_code == 409
    assert "Nessuna lezione: approvare comunque?" in r.content.decode()
    w.tt.refresh_from_db()
    assert w.tt.state == TimetableState.BOZZA and w.tt.version == 1
    r = c.post(URL, {"confirm": "1"})
    assert r.status_code == 200
    w.tt.refresh_from_db()
    assert w.tt.state == TimetableState.APPROVATO and not entries().exists()


def test_conflicts_need_confirm(w, user):
    Timetable.objects.filter(pk=w.tt.pk).update(
        conflicts=[{"chiave": "k1", "tipo": "X"}, {"chiave": "k2", "tipo": "X"}]
    )
    c = admin_client(user)
    r = c.post(URL)
    assert r.status_code == 409
    assert "2 conflitti" in r.content.decode()
    assert not entries().exists()
    assert c.post(URL, {"confirm": "1"}).status_code == 200
    assert entries().count() == 8


LOCKED = [
    ("genera/", {}),
    (
        "sposta/",
        {"slot_id": "1", "version": "1", "giorno": "LUNEDI", "ora_inizio": "10"},
    ),
    ("assegna/", {"classe": "1", "giorno": "LUNEDI", "ora_inizio": "10"}),
    ("conflitti/approva/", {"version": "1", "chiavi": "k"}),
    (
        "conflitti/rifiuta/",
        {"version": "1", "classe": "1", "giorno": "LUNEDI", "ora_inizio": "8"},
    ),
    ("deroga/", {"azione": "FORCE_3_HOURS_THEORY"}),
    ("sostituzione/", {}),
]


@pytest.mark.parametrize(("path", "data"), LOCKED)
def test_approved_week_refuses_edits(w, user, path, data):
    approve_timetable(w.tt.pk, user)
    before = Timetable.objects.values().get(pk=w.tt.pk)
    slots = list(Slot.objects.order_by("pk").values())
    r = admin_client(user).post(f"/orario/{MON:%Y-%m-%d}/{path}", data)
    assert r.status_code in (400, 409)
    assert Timetable.objects.values().get(pk=w.tt.pk) == before
    assert list(Slot.objects.order_by("pk").values()) == slots
    assert Class.objects.count() == 2


# --- undo_approval (#37) --------------------------------------------------

UNDO = f"/orario/{MON:%Y-%m-%d}/annulla-approvazione/"
WEEK2 = D(2026, 10, 19)


def delivered_by_name(w):
    d = delivered_hours_map(w.school)
    return {name: d.get(a.pk, 0) for name, a in w.asg.items()}


def test_undo_restores_every_assignment_including_paired(w, user, audit_mock):
    Slot.objects.all().delete()
    w.slot(w.a, w.rossi, w.mat, "LUNEDI", 8, paired=True, paired_class=w.b)
    w.slot(w.a, w.bianchi, w.ita, "LUNEDI", 9)
    before = delivered_by_name(w)
    approve_timetable(w.tt.pk, user)
    assert delivered_by_name(w) != before
    audit_mock.reset_mock()
    undo_approval(w.tt.pk, user, "errore")
    assert delivered_by_name(w) == before
    w.tt.refresh_from_db()
    assert w.tt.state == TimetableState.BOZZA and w.tt.approved_at is None
    assert w.tt.version == 3
    assert audit_mock.call_count == 1
    assert audit_mock.call_args.args[:3] == (audit.APPROVAL_UNDONE, user, MON)
    assert audit_mock.call_args.args[3]["motivo"] == "errore"


def test_undo_adds_reversals_and_keeps_rows(w, user):
    approve_timetable(w.tt.pk, user)
    ids = list(entries().order_by("pk").values_list("pk", "hours", "kind"))
    undo_approval(w.tt.pk, user, "  errore  ")
    assert (
        list(entries().order_by("pk").values_list("pk", "hours", "kind"))[: len(ids)]
        == ids
    )
    reversals = entries().filter(kind="REVERSAL")
    assert reversals.count() == 4
    assert {(e.reason, e.created_by_id, e.week, e.timetable_id) for e in reversals} == {
        ("errore", user.pk, MON, w.tt.pk)
    }
    assert all(e.hours < 0 for e in reversals)


def test_undo_reapprove_undo_again(w, user):
    before = delivered_by_name(w)
    approve_timetable(w.tt.pk, user)
    once = delivered_by_name(w)
    undo_approval(w.tt.pk, user, "uno")
    approve_timetable(w.tt.pk, user)
    assert delivered_by_name(w) == once
    undo_approval(w.tt.pk, user, "due")
    assert delivered_by_name(w) == before
    approve_timetable(w.tt.pk, user)
    assert approve_timetable(w.tt.pk, user)["ore_scalate"] == 0


def test_undo_refused_on_draft_writes_nothing(w, user, audit_mock):
    from scheduling.services import NotApproved

    with pytest.raises(NotApproved):
        undo_approval(w.tt.pk, user, "x")
    w.tt.refresh_from_db()
    assert w.tt.version == 1 and not entries().exists()
    audit_mock.assert_not_called()


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_undo_blank_reason_writes_nothing(w, user, audit_mock, reason):
    from scheduling.services import ReasonRequired

    approve_timetable(w.tt.pk, user)
    audit_mock.reset_mock()
    with pytest.raises(ReasonRequired):
        undo_approval(w.tt.pk, user, reason)
    w.tt.refresh_from_db()
    assert w.tt.state == TimetableState.APPROVATO and w.tt.version == 2
    assert entries().count() == 8
    audit_mock.assert_not_called()


def test_undo_earlier_week_leaves_later_week(w, user):
    tt2 = Timetable.objects.create(school=w.school, week_start=WEEK2, version=1)
    Slot.objects.create(
        timetable=tt2,
        school_class=w.a,
        teacher=w.rossi,
        subject=w.mat,
        day="LUNEDI",
        start_hour=8,
        end_hour=9,
    )
    approve_timetable(w.tt.pk, user)
    approve_timetable(tt2.pk, user)
    undo_approval(w.tt.pk, user, "errore")
    assert not entries().filter(timetable=tt2, kind="REVERSAL").exists()
    assert delivered_by_name(w)["a_mat"] == 1
    tt2.refresh_from_db()
    assert tt2.state == TimetableState.APPROVATO


def test_undo_keeps_adjustments(w, user):
    a = w.asg["a_mat"]
    adjust_delivered_hours(a, 3, user, "pregresso")
    before = delivered_by_name(w)
    approve_timetable(w.tt.pk, user)
    undo_approval(w.tt.pk, user, "errore")
    assert delivered_by_name(w) == before
    assert entries().filter(kind="ADJUSTMENT").count() == 1


def test_undo_can_leave_negative_after_adjustment_down(w, user):
    a = w.asg["a_mat"]
    approve_timetable(w.tt.pk, user)
    adjust_delivered_hours(a, 0, user, "azzera")
    undo_approval(w.tt.pk, user, "errore")
    assert delivered_by_name(w)["a_mat"] == -2


# --- undo endpoint --------------------------------------------------------


def test_admin_undoes_via_endpoint(w, user):
    approve_timetable(w.tt.pk, user)
    c = admin_client(user)
    page = c.get(f"/orario/{MON:%Y-%m-%d}/griglia/").content.decode()
    assert "Annulla approvazione" in page
    r = c.post(UNDO, {"motivo": "sbagliato"})
    assert r.status_code == 200
    body = r.content.decode()
    assert "Approvata" not in body and "Annulla approvazione" not in body
    w.tt.refresh_from_db()
    assert w.tt.state == TimetableState.BOZZA


def test_endpoint_blank_reason_is_field_error(w, user):
    approve_timetable(w.tt.pk, user)
    r = admin_client(user).post(UNDO, {"motivo": "  "})
    assert r.status_code == 400
    assert "Indica il motivo" in r.content.decode()
    w.tt.refresh_from_db()
    assert w.tt.state == TimetableState.APPROVATO and w.tt.version == 2


def test_endpoint_draft_is_refused_with_message(w, user):
    r = admin_client(user).post(UNDO, {"motivo": "x"})
    assert r.status_code == 409
    assert "non è approvata" in r.content.decode()
    w.tt.refresh_from_db()
    assert w.tt.version == 1 and not entries().exists()


def test_endpoint_unknown_week_404(w, user):
    Timetable.objects.all().delete()
    assert admin_client(user).post(UNDO, {"motivo": "x"}).status_code == 404


def test_endpoint_secretary_forbidden_and_button_absent(w, user, seg):
    approve_timetable(w.tt.pk, user)
    assert seg.post(UNDO, {"motivo": "x"}).status_code == 403
    assert (
        "Annulla approvazione"
        not in seg.get(f"/orario/{MON:%Y-%m-%d}/griglia/").content.decode()
    )
    w.tt.refresh_from_db()
    assert w.tt.state == TimetableState.APPROVATO and w.tt.version == 2


def test_endpoint_anonymous_redirected(w, user):
    approve_timetable(w.tt.pk, user)
    r = Client().post(UNDO, {"motivo": "x"})
    assert r.status_code == 302 and "login" in r["Location"]
    assert entries().count() == 8


def test_can_undo_approval_is_listed_and_admin_only(user, seg):
    assert can_undo_approval in PREDICATES
    assert can_undo_approval(user) and not can_undo_approval(None)
    assert not can_undo_approval(User.objects.get(email="segreteria@example.com"))

import datetime
import json
from unittest import mock

import pytest
from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory

from scheduling import audit, tasks, views
from scheduling.assign import assign_lesson
from scheduling.decisions import approve_conflicts, free_key, reject_lesson
from scheduling.models import AuditLog, SolveJob, Timetable
from scheduling.moves import move_lesson
from scheduling.quick_actions import apply_quick_action
from scheduling.services import approve_timetable, undo_approval
from scheduling.solver import SolveResult, SolveStatus
from school.assignment_hours import adjust_delivered_hours
from school.models import Teacher

D = datetime.date
MON = D(2026, 10, 12)
User = get_user_model()
QUICK = {
    "action_type": "OVERRIDE_AVAILABILITY",
    "class_id": None,
    "teacher_id": None,
    "day": None,
    "max_hours": None,
}


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(views, "today", lambda: D(2026, 10, 14))


@pytest.fixture
def user(db):
    return User.objects.create_user("boss@example.com", "pw-long-enough", "ADMIN")


def login(user):
    client = Client()
    client.force_login(user)
    return client


def only():
    return AuditLog.objects.get()


# ---- the model and record_audit ---------------------------------------------


def test_record_audit_stores_every_field(w, user):
    audit.record_audit(audit.GENERATE, user, MON, {"n": D(2026, 1, 2)})
    entry = only()
    assert entry.school_id == w.school.pk
    assert (entry.timetable_id, entry.week_start) == (w.tt.pk, MON)
    assert (entry.action, entry.user_id) == ("GENERATE", user.pk)
    assert entry.details == {"n": "2026-01-02"}
    assert entry.created_at is not None


def test_record_audit_without_week_or_user(w):
    audit.record_audit(audit.HOURS_ADJUSTED, None, None, {})
    entry = only()
    assert entry.timetable is None and entry.week_start is None and entry.user is None


def test_unknown_action_raises_and_writes_nothing(w, user):
    with pytest.raises(ValueError):
        audit.record_audit("DELETE_EVERYTHING", user, MON, {})
    assert AuditLog.objects.count() == 0


def test_every_constant_is_a_known_action():
    names = {
        audit.QUICK_ACTION,
        audit.SCHEDULE_APPROVED,
        audit.APPROVAL_UNDONE,
        audit.HOURS_ADJUSTED,
        audit.GENERATE,
        audit.MODIFY_SLOT,
        audit.CONFLICT_DECISION,
        audit.REPAIR_CONFIRMED,
        audit.ALTERNATIVE_CHOSEN,
        audit.FEED_REGENERATED,
    }
    assert names == audit.ACTIONS


def test_entries_are_append_only(w, user):
    audit.record_audit(audit.GENERATE, user, MON, {})
    entry = only()
    entry.action = "QUICK_ACTION"
    with pytest.raises(NotImplementedError):
        entry.save()
    with pytest.raises(NotImplementedError):
        entry.delete()
    with pytest.raises(NotImplementedError):
        AuditLog.objects.update(action="QUICK_ACTION")
    with pytest.raises(NotImplementedError):
        AuditLog.objects.all().delete()
    assert only().action == "GENERATE"


def test_deleting_the_user_or_timetable_keeps_the_entry(w, user):
    audit.record_audit(audit.GENERATE, user, MON, {})
    user.delete()
    Timetable.objects.filter(pk=w.tt.pk).delete()
    entry = only()
    assert entry.user is None and entry.timetable is None
    assert entry.week_start == MON


def test_admin_shows_the_model_with_no_add_change_or_delete(w, user):
    model_admin = django_admin.site._registry[AuditLog]
    request = RequestFactory().get("/admin/")
    request.user = user
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)


# ---- one entry per successful action ------------------------------------------


def test_generation_writes_one_entry_and_failures_none(w, user):
    job = SolveJob.objects.create(school=w.school, week_start=MON, requested_by=user)
    tasks.run_job(job.pk)
    entry = only()
    assert (entry.action, entry.user_id, entry.week_start) == ("GENERATE", user.pk, MON)
    assert entry.details["job"] == job.pk and entry.details["slot"] > 0

    SolveJob.objects.filter(pk=job.pk).delete()
    other = SolveJob.objects.create(school=w.school, week_start=MON, requested_by=user)
    with mock.patch.object(
        tasks, "solve", return_value=SolveResult(SolveStatus.INFEASIBLE)
    ):
        tasks.run_job(other.pk)
    other.refresh_from_db()
    assert other.status == "FAILED"
    assert AuditLog.objects.count() == 1


def test_generation_rolled_back_writes_no_entry(w, user):
    job = SolveJob.objects.create(school=w.school, week_start=MON, requested_by=user)
    real = audit.record_audit

    def write_then_fail(*args):
        real(*args)
        raise RuntimeError("boom")

    with mock.patch("scheduling.audit.record_audit", side_effect=write_then_fail):
        tasks.run_job(job.pk)
    job.refresh_from_db()
    assert job.status == "FAILED"
    assert AuditLog.objects.count() == 0


def test_move_writes_one_entry_a_no_op_and_a_stale_none(w, user):
    slot = w.slots["LUNEDI", "a_ita"]
    move_lesson(w.tt.pk, slot.pk, "LUNEDI", 9, 1, user)  # its own cell
    assert AuditLog.objects.count() == 0
    from scheduling.moves import MoveRejected

    with pytest.raises(MoveRejected):
        move_lesson(w.tt.pk, slot.pk, "LUNEDI", 11, 99, user)  # stale
    assert AuditLog.objects.count() == 0
    move_lesson(w.tt.pk, slot.pk, "LUNEDI", 11, 1, user)
    entry = only()
    assert (entry.action, entry.user_id) == ("MODIFY_SLOT", user.pk)
    assert entry.details["slot"] == slot.pk and entry.details["ora"] == 11


def test_a_failure_after_the_audit_call_removes_the_entry(w, user):
    slot = w.slots["LUNEDI", "a_ita"]
    real = audit.record_audit

    def write_then_fail(*args):
        real(*args)
        raise RuntimeError("after the audit write")

    with mock.patch("scheduling.audit.record_audit", side_effect=write_then_fail):
        with pytest.raises(RuntimeError):
            move_lesson(w.tt.pk, slot.pk, "LUNEDI", 11, 1, user)
    assert AuditLog.objects.count() == 0
    slot.refresh_from_db()
    assert slot.start_hour == 9
    w.tt.refresh_from_db()
    assert w.tt.version == 1


def test_assign_writes_one_entry(w, user):
    assign_lesson(w.tt.pk, w.a.pk, "LUNEDI", 10, w.rossi.pk, w.mat.pk, 1, user)
    entry = only()
    assert (entry.action, entry.user_id) == ("MODIFY_SLOT", user.pk)
    assert entry.details["azione"] == "assegna" and entry.details["ora"] == 10


def test_refused_assign_writes_none(w, user):
    from scheduling.moves import MoveRejected

    with pytest.raises(MoveRejected):
        # hour 8 of class A is taken
        assign_lesson(w.tt.pk, w.a.pk, "LUNEDI", 8, w.rossi.pk, w.mat.pk, 1, user)
    assert AuditLog.objects.count() == 0


def test_conflict_decisions_write_one_entry_each(w, user):
    key = free_key(w.a.pk, "LUNEDI", 2)
    w.tt.conflicts = [{"kind": "classe_unfilled", "chiave": key, "description": "x"}]
    w.tt.save()
    approve_conflicts(w.tt.pk, [key], 1, user)
    entry = only()
    assert entry.action == "CONFLICT_DECISION" and entry.details["chiavi"] == [key]
    # repeated approval: nothing new, nothing written
    approve_conflicts(w.tt.pk, [key], 2, user)
    assert AuditLog.objects.count() == 1
    reject_lesson(w.tt.pk, w.a.pk, "LUNEDI", 8, 2, user)
    assert AuditLog.objects.count() == 2
    assert AuditLog.objects.first().details["azione"] == "rifiuta"


def test_quick_action_writes_one_entry_and_a_refusal_none(w, user):
    from scheduling.jobs import GenerationRefused

    job, _ = apply_quick_action(MON, user, QUICK)
    entry = only()
    assert (entry.action, entry.user_id, entry.details) == (
        "QUICK_ACTION",
        user.pk,
        QUICK,
    )
    assert SolveJob.objects.filter(pk=job.pk).exists()
    with pytest.raises(GenerationRefused):  # a job is already active
        apply_quick_action(MON, user, QUICK)
    assert AuditLog.objects.count() == 1


def test_quick_action_audit_failure_leaves_no_enqueued_job(w, user):
    with mock.patch("scheduling.audit.record_audit", side_effect=RuntimeError):
        with pytest.raises(RuntimeError):
            apply_quick_action(MON, user, QUICK)
    assert SolveJob.objects.count() == 0
    assert AuditLog.objects.count() == 0


def test_approval_undo_and_repeated_approval(w, user):
    approve_timetable(w.tt.pk, user)
    assert only().action == "SCHEDULE_APPROVED"
    approve_timetable(w.tt.pk, user)  # repeated: writes nothing
    assert AuditLog.objects.count() == 1
    undo_approval(w.tt.pk, user, "errore")
    assert AuditLog.objects.count() == 2
    assert AuditLog.objects.first().action == "APPROVAL_UNDONE"


def test_hours_adjustment_writes_one_entry_and_a_no_change_none(w, user):
    assignment = w.asg["a_mat"]
    adjust_delivered_hours(assignment, 0, user, "")  # no change
    assert AuditLog.objects.count() == 0
    adjust_delivered_hours(assignment, 3, user, "carryover")
    entry = only()
    assert (entry.action, entry.week_start, entry.timetable) == (
        "HOURS_ADJUSTED",
        None,
        None,
    )
    assert entry.details["ore"] == 3


def test_details_never_hold_secrets(w, user):
    slot = w.slots["LUNEDI", "a_ita"]
    move_lesson(w.tt.pk, slot.pk, "LUNEDI", 11, 1, user)
    assign_lesson(w.tt.pk, w.a.pk, "LUNEDI", 12, w.rossi.pk, w.mat.pk, 2, user)
    reject_lesson(w.tt.pk, w.a.pk, "LUNEDI", 12, 3, user)
    apply_quick_action(MON, user, QUICK)
    approve_timetable(w.tt.pk, user)
    undo_approval(w.tt.pk, user, "errore")
    adjust_delivered_hours(w.asg["a_mat"], 2, user, "carryover")
    assert AuditLog.objects.count() == 7
    text = json.dumps([e.details for e in AuditLog.objects.all()]).lower()
    for forbidden in ("password", "token", "http", "pw-long-enough", "/disponibilita"):
        assert forbidden not in text
    assert Teacher.objects.count()  # fixture sanity


# ---- the screen ---------------------------------------------------------------


def seed(user, n, action=audit.GENERATE, week=MON):
    for i in range(n):
        audit.record_audit(action, user, week, {"i": i})


def test_admin_sees_the_table_newest_first_25_per_page(w, user):
    seed(user, 30)
    r = login(user).get("/audit/")
    assert r.status_code == 200
    page = r.content.decode()
    assert page.count("<tr>") == 26  # header + 25
    assert page.index("i: 29") < page.index("i: 28")
    assert "12/10/2026" in page and user.email in page
    assert "Generazione orario" in page
    second = login(user).get("/audit/?page=2").content.decode()
    assert second.count("<tr>") == 6 and "i: 0" in second


def test_empty_log_and_empty_filter_show_italian_empty_states(w, user):
    c = login(user)
    assert "Il registro è vuoto" in c.get("/audit/").content.decode()
    seed(user, 1)
    r = c.get("/audit/?azione=QUICK_ACTION")
    assert "Nessuna voce corrisponde ai filtri" in r.content.decode()


def test_filters_by_week_and_action(w, user):
    seed(user, 2, audit.GENERATE, MON)
    seed(user, 3, audit.QUICK_ACTION, MON + datetime.timedelta(days=7))
    c = login(user)
    assert c.get("/audit/?azione=GENERATE").content.decode().count("<tr>") == 3
    # any day of the week finds the week
    r = c.get("/audit/?settimana=2026-10-14").content.decode()
    assert r.count("<tr>") == 3 and "Azione rapida" not in r.split("</form>")[1]
    both = c.get("/audit/?settimana=2026-10-19&azione=QUICK_ACTION")
    assert both.content.decode().count("<tr>") == 4
    junk = c.get("/audit/?settimana=boh&azione=NOPE")
    assert junk.status_code == 200 and junk.content.decode().count("<tr>") == 6


def test_segreteria_gets_403_anonymous_login_and_menu_entry(w, user):
    seg = User.objects.create_user("seg@example.com", "pw-long-enough", "SEGRETERIA")
    c = login(seg)
    assert c.get("/audit/").status_code == 403
    assert "Registro" not in c.get("/").content.decode()
    r = Client().get("/audit/")
    assert r.status_code == 302 and r.url.startswith("/login/")
    assert "Registro" in login(user).get("/").content.decode()


def test_the_screen_is_read_only(w, user):
    assert login(user).post("/audit/").status_code == 405

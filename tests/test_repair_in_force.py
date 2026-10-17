"""Confirmed repairs stay in force for their week (#72).

A confirmed "sostituzione" is applied to every later re-evaluation and repair of
the same timetable, until the timetable is regenerated or an alternative is
chosen. Stored nowhere new: read from the REPAIR jobs and the audit log.
"""

import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from scheduling import audit, tasks, views
from scheduling import perturbations as pert
from scheduling.models import AuditLog, JobKind, JobStatus, Slot, SolveJob, Timetable
from scheduling.solver import CLASSE_UNFILLED
from school.models import Assignment, School
from tests import test_repair as base
from tests.test_repair import MON, WEEK, absent, closed, confirm_url, removed, start

# the fixtures of the repair tests, reused as they are
rw = base.rw
admin = base.admin

D = datetime.date
MOVE = f"/orario/{WEEK}/sposta/"
ASSIGN = f"/orario/{WEEK}/assegna/"
CHANGE = f"/orario/{WEEK}/cambia-docente/"
REJECT = f"/orario/{WEEK}/conflitti/rifiuta/"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(views, "today", lambda: D(2026, 10, 14))


def confirm(admin, r, params):
    job = start(admin, params)
    assert admin.post(confirm_url(job)).status_code == 200
    r.tt.refresh_from_db()
    return job


def version(r):
    r.tt.refresh_from_db()
    return r.tt.version


def move(admin, r, slot, day, hour):
    return admin.post(
        MOVE,
        {
            "slot_id": slot.pk,
            "giorno": day,
            "ora_inizio": hour,
            "version": version(r),
        },
    )


def assign(admin, r, klass, day, hour, teacher, subject):
    return admin.post(
        ASSIGN,
        {
            "classe": r.classes[klass].pk,
            "giorno": day,
            "ora_inizio": hour,
            "docente": r.teachers[teacher].pk,
            "materia": r.subjects[subject].pk,
            "version": version(r),
        },
    )


def change(admin, r, slot, teacher):
    return admin.post(
        CHANGE,
        {
            "slot_id": slot.pk,
            "docente": r.teachers[teacher].pk,
            "version": version(r),
        },
    )


def slot_of(r, klass, day, hour=None, teacher=None):
    qs = Slot.objects.filter(timetable=r.tt, school_class=r.classes[klass], day=day)
    if hour is not None:
        qs = qs.filter(start_hour=hour)
    if teacher is not None:
        qs = qs.filter(teacher=r.teachers[teacher])
    return qs.first()


def unfilled(r, klass, day):
    r.tt.refresh_from_db()
    name = r.classes[klass].name
    return [
        c
        for c in r.tt.conflicts
        if c["kind"] == CLASSE_UNFILLED and name in c["description"] and day in str(c)
    ]


def frozen(r):
    r.tt.refresh_from_db()
    return (
        r.tt.version,
        r.tt.conflicts,
        r.tt.quality_score,
        sorted(Slot.objects.values_list("pk", "teacher", "day", "start_hour")),
    )


def edit_once(admin, r, how):
    """One later edit of the kind `how`, none of them touching class 1 on Thursday."""
    if how == "move":
        return move(admin, r, slot_of(r, 0, "LUNEDI", 8), "LUNEDI", 12)
    if how == "assign":
        gone = slot_of(r, 0, "LUNEDI", 11)
        t, s = r.teachers.index(gone.teacher), r.subjects.index(gone.subject)
        gone.delete()
        return assign(admin, r, 0, "LUNEDI", 11, t, s)
    if how == "change":
        Assignment.objects.create(
            school=r.school,
            teacher=r.teachers[4],
            school_class=r.classes[0],
            subject=r.subjects[0],
            ore_totali=40,
        )
        slot = Slot.objects.filter(
            timetable=r.tt, school_class=r.classes[0], subject=r.subjects[0]
        ).first()
        return change(admin, r, slot, 4)
    return admin.post(
        REJECT,
        {
            "classe": r.classes[0].pk,
            "giorno": "LUNEDI",
            "ora_inizio": 9,
            "version": version(r),
        },
    )


EDITS = ["move", "assign", "change", "reject"]


# ---- carried into later edits ------------------------------------------------------


@pytest.mark.parametrize("how", EDITS)
def test_a_closed_class_day_stays_closed_after_a_later_edit(admin, rw, how):
    confirm(admin, rw, closed(rw, klass=1, day="GIOVEDI"))
    response = edit_once(admin, rw, how)
    assert response.status_code == 200
    assert unfilled(rw, 1, "GIOVEDI") == []
    page = response.content.decode()
    assert "Ora non coperta per la classe 1B GIOVEDI" not in page
    assert not Slot.objects.filter(
        timetable=rw.tt, school_class=rw.classes[1], day="GIOVEDI"
    )


@pytest.mark.parametrize("how", EDITS)
def test_a_removed_slot_stays_removed_after_a_later_edit(admin, rw, how):
    confirm(admin, rw, removed(rw, klass=0, day="LUNEDI", hour=11))
    response = (
        move(admin, rw, slot_of(rw, 1, "LUNEDI", 8), "LUNEDI", 12)
        if how == "move"
        else edit_once(admin, rw, how)
        if how != "assign"
        else None
    )
    if response is None:
        free = slot_of(rw, 0, "MARTEDI", 11)
        t, s = rw.teachers.index(free.teacher), rw.subjects.index(free.subject)
        free.delete()
        response = assign(admin, rw, 0, "MARTEDI", 11, t, s)
    assert response.status_code == 200
    assert unfilled(rw, 0, "LUNEDI") == []
    assert "Ora non coperta per la classe 1A LUNEDI" not in response.content.decode()
    assert Slot.objects.filter(
        timetable=rw.tt, school_class=rw.classes[0], day="LUNEDI"
    ).count() in (3, 2)  # 3, or 2 after the explicit "rifiuta" of one more
    assert not Slot.objects.filter(
        timetable=rw.tt, school_class=rw.classes[0], day="LUNEDI", start_hour=11
    )


# ---- refusals ----------------------------------------------------------------------


def refused(admin, rw, response, text):
    before = frozen(rw)
    assert response.status_code == 409
    assert text in response.content.decode()
    assert before == frozen(rw)


def test_moving_or_assigning_onto_a_closed_day_is_refused(admin, rw):
    confirm(admin, rw, closed(rw, klass=1, day="GIOVEDI"))
    slot = slot_of(rw, 1, "LUNEDI", 8)
    before = frozen(rw)
    r1 = move(admin, rw, slot, "GIOVEDI", 12)
    assert r1.status_code == 409 and "non ha ore di lezione" in r1.content.decode()
    assert before == frozen(rw)
    r2 = assign(admin, rw, 1, "GIOVEDI", 8, 0, 0)
    assert r2.status_code == 409 and "non ha ore di lezione" in r2.content.decode()
    assert before == frozen(rw)


def test_moving_or_assigning_onto_a_removed_slot_is_refused(admin, rw):
    confirm(admin, rw, removed(rw, klass=0, day="LUNEDI", hour=11))
    before = frozen(rw)
    r1 = move(admin, rw, slot_of(rw, 0, "MARTEDI", 8), "LUNEDI", 11)
    assert r1.status_code == 409 and r1.content.decode().count("eliminata") >= 1
    assert before == frozen(rw)
    r2 = assign(admin, rw, 0, "LUNEDI", 11, 0, 0)
    assert r2.status_code == 409 and "eliminata" in r2.content.decode()
    assert before == frozen(rw)
    # the other classes may still use that hour
    assert (
        move(admin, rw, slot_of(rw, 1, "MARTEDI", 8), "LUNEDI", 12).status_code == 200
    )


def test_a_teacher_absent_day_refuses_move_assign_and_change(admin, rw):
    confirm(admin, rw, absent(rw, teacher=0, days=("MERCOLEDI",)))
    mine = slot_of(rw, 0, "LUNEDI", teacher=0)
    before = frozen(rw)
    r1 = move(admin, rw, mine, "MERCOLEDI", 12)
    assert r1.status_code == 409 and "assente" in r1.content.decode()
    assert before == frozen(rw)

    gone = slot_of(rw, 0, "MERCOLEDI", teacher=1)
    gone_cell = gone.start_hour
    subject = rw.subjects.index(gone.subject)
    gone.delete()
    before = frozen(rw)
    r2 = assign(admin, rw, 0, "MERCOLEDI", gone_cell, 0, 0)
    assert r2.status_code == 409 and "assente" in r2.content.decode()
    assert before == frozen(rw)

    other = slot_of(rw, 0, "MERCOLEDI", teacher=2)
    Assignment.objects.create(
        school=rw.school,
        teacher=rw.teachers[0],
        school_class=rw.classes[0],
        subject=other.subject,
        ore_totali=40,
    )
    before = frozen(rw)
    r3 = change(admin, rw, other, 0)
    assert r3.status_code == 409 and "assente" in r3.content.decode()
    assert before == frozen(rw)

    # a day the teacher is present is fine
    assert move(admin, rw, mine, "MARTEDI", 12).status_code == 200
    assert assign(admin, rw, 0, "MERCOLEDI", gone_cell, 1, subject).status_code == 200


# ---- two repairs -------------------------------------------------------------------


def test_a_second_repair_knows_the_first(admin, rw):
    confirm(admin, rw, closed(rw, klass=1, day="GIOVEDI"))
    job = start(admin, absent(rw, teacher=0, days=("MERCOLEDI",)))
    assert job.status == JobStatus.DONE
    for c in job.result["changes"]:
        if c["class_id"] == rw.classes[1].pk and c["new"]:
            assert c["new"]["day"] != "GIOVEDI"
    assert admin.post(confirm_url(job)).status_code == 200
    rw.tt.refresh_from_db()
    assert not Slot.objects.filter(
        timetable=rw.tt, school_class=rw.classes[1], day="GIOVEDI"
    )
    assert not Slot.objects.filter(
        timetable=rw.tt, teacher=rw.teachers[0], day="MERCOLEDI"
    )
    assert unfilled(rw, 1, "GIOVEDI") == []


# ---- the boundary ------------------------------------------------------------------


def regenerate(rw):
    job = SolveJob.objects.create(school=rw.school, week_start=MON)
    tasks.run_job(job.pk)


def test_a_new_generation_ends_the_repairs_in_force(admin, rw):
    confirm(admin, rw, closed(rw, klass=1, day="GIOVEDI"))
    assert len(pert.repairs_in_force(rw.school, MON)) == 1
    regenerate(rw)
    rw.tt = Timetable.objects.get(school=rw.school, week_start=MON)
    assert pert.repairs_in_force(rw.school, MON) == []
    response = move(admin, rw, slot_of(rw, 1, "LUNEDI", 8), "GIOVEDI", 12)
    assert response.status_code == 200
    # and a repair confirmed afterwards is in force again
    confirm(admin, rw, closed(rw, klass=2, day="GIOVEDI"))
    assert len(pert.repairs_in_force(rw.school, MON)) == 1


def test_choosing_an_alternative_ends_the_repairs_in_force(admin, rw):
    confirm(admin, rw, closed(rw, klass=1, day="GIOVEDI"))
    audit.record_audit(audit.ALTERNATIVE_CHOSEN, None, MON, {})
    assert pert.repairs_in_force(rw.school, MON) == []
    confirm(admin, rw, absent(rw, teacher=0, days=("MERCOLEDI",)))
    assert len(pert.repairs_in_force(rw.school, MON)) == 1


def test_repairs_that_are_not_confirmed_in_this_week_have_no_effect(admin, rw):
    start(admin, closed(rw, klass=1, day="GIOVEDI"))  # proposed, never confirmed
    assert pert.repairs_in_force(rw.school, MON) == []
    job = confirm(admin, rw, closed(rw, klass=1, day="GIOVEDI"))
    assert len(pert.repairs_in_force(rw.school, MON)) == 1
    other_school = School.objects.create(
        name="B",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2026, 12, 21),
    )
    SolveJob.objects.filter(pk=job.pk).update(status=JobStatus.FAILED)
    assert pert.repairs_in_force(rw.school, MON) == []
    SolveJob.objects.filter(pk=job.pk).update(status=JobStatus.DONE)
    SolveJob.objects.filter(pk=job.pk).update(school=other_school)
    assert pert.repairs_in_force(rw.school, MON) == []
    SolveJob.objects.filter(pk=job.pk).update(
        school=rw.school, week_start=MON + datetime.timedelta(days=7)
    )
    assert pert.repairs_in_force(rw.school, MON) == []


def test_a_deleted_repair_job_is_skipped(admin, rw):
    job = confirm(admin, rw, closed(rw, klass=1, day="GIOVEDI"))
    SolveJob.objects.filter(pk=job.pk).delete()
    assert pert.repairs_in_force(rw.school, MON) == []
    assert move(admin, rw, slot_of(rw, 0, "LUNEDI", 8), "LUNEDI", 12).status_code == 200


# ---- nothing else is written -------------------------------------------------------


def test_audit_rows_and_the_job_are_not_modified(admin, rw):
    job = confirm(admin, rw, absent(rw, teacher=0, days=("MERCOLEDI",)))
    job.refresh_from_db()
    audit_before = list(AuditLog.objects.order_by("pk").values())
    params, result = job.params, job.result
    assert move(admin, rw, slot_of(rw, 0, "LUNEDI", 8), "LUNEDI", 12).status_code == 200
    job.refresh_from_db()
    assert (job.params, job.result) == (params, result)
    after = list(AuditLog.objects.order_by("pk").values())
    assert after[: len(audit_before)] == audit_before
    assert [a["action"] for a in after[len(audit_before) :]] == [audit.MODIFY_SLOT]
    assert SolveJob.objects.filter(kind=JobKind.REPAIR).count() == 1


def test_a_refusal_does_not_bump_the_version_and_a_success_bumps_it_once(admin, rw):
    confirm(admin, rw, closed(rw, klass=1, day="GIOVEDI"))
    v = version(rw)
    assert (
        move(admin, rw, slot_of(rw, 1, "LUNEDI", 8), "GIOVEDI", 12).status_code == 409
    )
    assert version(rw) == v
    assert move(admin, rw, slot_of(rw, 0, "LUNEDI", 8), "LUNEDI", 12).status_code == 200
    assert version(rw) == v + 1


# ---- cost --------------------------------------------------------------------------


def lookup_queries(rw):
    with CaptureQueriesContext(connection) as ctx:
        pert.repairs_in_force(rw.school, MON)
    return len(ctx)


def test_the_lookup_costs_the_same_for_zero_one_and_three_repairs(admin, rw):
    zero = lookup_queries(rw)
    confirm(admin, rw, closed(rw, klass=1, day="GIOVEDI"))
    one = lookup_queries(rw)
    confirm(admin, rw, absent(rw, teacher=0, days=("MERCOLEDI",)))
    confirm(admin, rw, removed(rw, klass=2, day="MARTEDI", hour=11))
    assert len(pert.repairs_in_force(rw.school, MON)) == 3
    three = lookup_queries(rw)
    assert zero == one == three

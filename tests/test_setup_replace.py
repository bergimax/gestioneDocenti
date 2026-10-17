import datetime
from unittest import mock

import pytest
from django.test import Client

from accounts.models import User
from availability.links import create_link
from availability.models import Availability, AvailabilityLink
from feeds.models import FeedToken
from feeds.services import ensure_tokens
from ledger.models import EntryKind, LedgerEntry
from scheduling.models import (
    AlternativePick,
    AuditLog,
    ConflictDecision,
    JobKind,
    Slot,
    SoftWeightAdjustment,
    SolveJob,
    Timetable,
    TimetableFeedback,
    TimetableState,
)
from school.models import (
    Assignment,
    CalendarEntry,
    Class,
    ClassPairing,
    ParkedUpload,
    School,
    Subject,
    Teacher,
)
from tests.test_setup_wizard import COUNTS, SAVE, SETUP, html, login, upload_all

MONDAY = datetime.date(2026, 9, 14)
EVIL = "<script>alert(1)</script>"


@pytest.fixture
def admin(db):
    return login("ADMIN")


@pytest.fixture
def seg(db):
    return login("SEGRETERIA")


@pytest.fixture
def old(admin):
    """A school with a bit of everything hanging off it."""
    user = User.objects.get(email="admin@example.com")
    school = School.objects.create(
        name="Vecchia",
        formative_year="2025",
        year_start=datetime.date(2026, 9, 1),
        year_end=datetime.date(2027, 6, 30),
    )
    c1 = Class.objects.create(school=school, name="9Z", student_count=10)
    c2 = Class.objects.create(school=school, name="9Y", student_count=10)
    t = Teacher.objects.create(school=school, name="OLD T", type="ASSUNTO")
    sub = Subject.objects.create(
        school=school, name="Old", kind="TEORIA", cognitive_weight="ALTO"
    )
    a = Assignment.objects.create(
        school=school, teacher=t, school_class=c1, subject=sub, ore_totali=10
    )
    ClassPairing.objects.create(
        school=school, class_a=c1, class_b=c2, subject=sub, note=""
    )
    CalendarEntry.objects.create(
        school=school, date=MONDAY, group="PRIME", max_hours=6, is_closure=False
    )
    tt = Timetable.objects.create(school=school, week_start=MONDAY)
    Slot.objects.create(
        timetable=tt,
        school_class=c1,
        teacher=t,
        subject=sub,
        day="LUNEDI",
        start_hour=8,
        end_hour=9,
    )
    ConflictDecision.objects.create(timetable=tt, chiave="k")
    job = SolveJob.objects.create(
        school=school, week_start=MONDAY, kind=JobKind.GENERATE
    )
    AlternativePick.objects.create(job=job, school=school, week_start=MONDAY)
    Availability.objects.create(school=school, teacher=t, week_start=MONDAY, slots={})
    _, token = create_link(t, MONDAY, user)
    ensure_tokens(school)
    AuditLog.objects.create(
        school=school, timetable=tt, action="x", user=user, details={"a": 1}
    )
    SoftWeightAdjustment.objects.create(
        school=school, kind="k", old_weight=1, new_weight=2, user=user
    )
    TimetableFeedback.objects.create(
        school=school,
        timetable=tt,
        week_start=MONDAY,
        user=user,
        note="ok",
        timetable_state="BOZZA",
    )
    return {
        "school": school,
        "tt": tt,
        "assignment": a,
        "token": token,
        "feed": FeedToken.objects.first().token,
    }


def confirm(client, name="Vecchia", **extra):
    data = {
        "school_name": "Nuova",
        "school_year": "2026-2027",
        "confirm_name": name,
    }
    data.update(extra)
    return client.post(SAVE, data)


def total_rows():
    return sum(
        m.objects.count()
        for m in (Class, Teacher, Subject, CalendarEntry, Assignment, ClassPairing)
    )


def test_access(seg, client, old):
    assert seg.get(SETUP).status_code == 403
    assert seg.post(SAVE).status_code == 403
    assert client.get(SETUP).status_code == 302
    assert "Sostituisci i dati" not in html(seg.get("/dati-scuola/"))


def test_link_visible_to_admin(admin, old):
    page = html(admin.get("/dati-scuola/"))
    assert "Sostituisci i dati della scuola" in page
    assert 'href="/setup/"' in page


def test_page_warns_and_lists_counts(admin, old):
    upload_all(admin)
    page = html(admin.get(SETUP))
    assert "esiste già una scuola" in page
    assert "Verranno eliminati" in page
    assert "Classi: 2" in page and "Orari: 1" in page
    assert 'name="confirm_name"' in page


@pytest.mark.parametrize("name", ["", "sbagliata"])
def test_wrong_confirmation_writes_nothing(admin, old, name):
    upload_all(admin)
    before = total_rows()
    r = confirm(admin, name)
    assert r.status_code == 422
    page = html(r)
    assert 'aria-invalid="true"' in page
    assert 'aria-describedby="id_confirm_name-error"' in page
    assert 'id="id_confirm_name-error"' in page
    assert total_rows() == before
    assert Class.objects.filter(name="9Z").exists()
    assert ParkedUpload.objects.count() == 6
    assert School.objects.get().name == "Vecchia"


def test_happy_path_replaces_everything(admin, old):
    upload_all(admin)
    users = list(User.objects.values_list("pk", flat=True))
    old_ids = {
        m: set(m.objects.values_list("pk", flat=True))
        for m in (Class, Teacher, Subject, CalendarEntry, Assignment, ClassPairing)
    }
    audit = list(AuditLog.objects.values("pk", "action", "details", "user_id"))
    weights = list(SoftWeightAdjustment.objects.values("pk", "kind", "new_weight"))
    r = confirm(admin)
    assert r.status_code == 302
    school = School.objects.get()
    assert school.pk == old["school"].pk
    assert (school.name, school.formative_year) == ("Nuova", "2026-2027")
    assert school.year_start == datetime.date(2026, 9, 14)
    assert school.year_end == datetime.date(2026, 12, 25)
    assert Class.objects.count() == COUNTS["classes"]
    assert Teacher.objects.count() == COUNTS["teachers"]
    assert Subject.objects.count() == COUNTS["subjects"]
    assert CalendarEntry.objects.count() == COUNTS["calendar"]
    assert Assignment.objects.count() == COUNTS["assignments"]
    assert ClassPairing.objects.count() == COUNTS["pairings"]
    for m, ids in old_ids.items():
        assert not ids & set(m.objects.values_list("pk", flat=True))
    for m in (
        Timetable,
        Slot,
        ConflictDecision,
        Availability,
        AvailabilityLink,
        FeedToken,
        SolveJob,
        AlternativePick,
    ):
        assert m.objects.count() == 0
    assert ParkedUpload.objects.count() == 0
    # append-only like the audit log: kept, only the timetable link is cleared
    assert TimetableFeedback.objects.get().note == "ok"
    assert list(User.objects.values_list("pk", flat=True)) == users
    assert list(AuditLog.objects.values("pk", "action", "details", "user_id")) == audit
    assert (
        list(SoftWeightAdjustment.objects.values("pk", "kind", "new_weight")) == weights
    )
    done = html(admin.get("/setup/completato/"))
    assert "sostituiti" in done and "Dati eliminati" in done

    # old tokens and pages
    assert Client().get(f"/feeds/{old['feed']}.ics").status_code == 404
    assert Client().get(f"/l/{old['token']}/").status_code == 404
    for url in ("/dati-scuola/", "/disponibilita/", "/orario/", "/disponibilita/link/"):
        assert admin.get(url).status_code == 200


def test_refused_with_approved_timetable_and_ledger(admin, old):
    Timetable.objects.filter(pk=old["tt"].pk).update(state=TimetableState.APPROVATO)
    LedgerEntry.objects.create(
        assignment=old["assignment"], hours=1, kind=EntryKind.DELIVERED
    )
    upload_all(admin)
    before = total_rows()
    page = html(admin.get(SETUP))
    assert "1 orario approvato, 1 movimento nel registro ore" in page
    r = confirm(admin)
    assert r.status_code == 422
    assert "1 orario approvato, 1 movimento nel registro ore" in html(r)
    assert total_rows() == before
    assert Timetable.objects.count() == 1
    assert ParkedUpload.objects.count() == 6


def test_refused_with_only_a_ledger_entry(admin, old):
    LedgerEntry.objects.create(
        assignment=old["assignment"], hours=1, kind=EntryKind.DELIVERED
    )
    upload_all(admin)
    r = confirm(admin)
    assert r.status_code == 422
    assert "1 movimento nel registro ore" in html(r)
    assert "approvato" not in html(r).split("non possono")[1].split("</")[0]
    assert Class.objects.filter(name="9Z").exists()


def test_failure_midway_keeps_all_old_rows(admin, old):
    upload_all(admin)
    before = total_rows()
    with mock.patch(
        "school.setup_service._create_pairings", side_effect=RuntimeError("boom")
    ):
        r = confirm(admin)
    assert r.status_code == 500
    assert total_rows() == before
    assert Timetable.objects.count() == 1
    assert Slot.objects.count() == 1
    assert FeedToken.objects.count() > 0
    assert School.objects.get().name == "Vecchia"
    assert ParkedUpload.objects.count() == 6


def test_double_submit_replaces_once(admin, old):
    upload_all(admin)
    assert confirm(admin).status_code == 302
    second = confirm(admin)  # the old name no longer matches, uploads are gone
    assert second.status_code == 422
    assert Class.objects.count() == COUNTS["classes"]
    assert School.objects.count() == 1


def test_first_setup_has_no_confirmation_field(admin, db):
    upload_all(admin)
    assert 'name="confirm_name"' not in html(admin.get(SETUP))
    r = admin.post(SAVE, {"school_name": "ITS", "school_year": "2026-2027"})
    assert r.status_code == 302
    assert "sostituiti" not in html(admin.get("/setup/completato/"))


def test_school_name_is_escaped(admin, old):
    School.objects.update(name=EVIL)
    upload_all(admin)
    for page in (html(admin.get(SETUP)), html(admin.get("/dati-scuola/"))):
        assert EVIL not in page
    r = confirm(admin, "sbagliata")
    assert EVIL not in html(r)

"""The audit screen joins reason and names on the ids (#143)."""

import datetime

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from ledger.models import EntryKind, LedgerEntry
from scheduling import audit
from scheduling.models import AuditLog
from school.assignment_hours import adjust_delivered_hours
from school.models import Assignment, Class, School, Subject, Teacher

D = datetime.date
GONE = "(non disponibile)"


def make_school(name):
    return School.objects.create(
        name=name,
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )


def make_assignment(school, teacher_name="ROSSI MARIO", class_name="1A"):
    return Assignment.objects.create(
        school=school,
        teacher=Teacher.objects.create(
            school=school, name=teacher_name, type="ASSUNTO"
        ),
        school_class=Class.objects.create(school=school, name=class_name),
        subject=Subject.objects.create(
            school=school, name="Mat", kind="TEORIA", cognitive_weight="ALTO"
        ),
        ore_totali=40,
    )


@pytest.fixture
def w(db):
    class W:
        pass

    w = W()
    w.school = make_school("Scuola")
    w.asg = make_assignment(w.school)
    w.teacher = w.asg.teacher
    w.klass = w.asg.school_class
    w.admin = User.objects.create_user("admin@example.com", "pw-long-enough", "ADMIN")
    return w


def page(user, url="/audit/"):
    c = Client()
    c.force_login(user)
    r = c.get(url)
    assert r.status_code == 200
    return r.content.decode()


def write(action, details, school=None):
    AuditLog.objects.create(
        school=school or School.objects.order_by("pk").first(),
        action=action,
        details=details,
    )


def test_new_adjustment_shows_the_reason_from_the_ledger(w):
    adjust_delivered_hours(w.asg, 5, w.admin, "carryover")
    html = page(w.admin)
    assert "motivo: carryover" in html
    assert html.count("carryover") == 1
    assert "voce: " in html


def test_new_regeneration_shows_teacher_and_class_names(w):
    c = Client()
    c.force_login(w.admin)
    c.get("/feeds/")  # creates the tokens
    assert c.post(f"/feeds/rigenera/teacher/{w.teacher.pk}/").status_code == 302
    assert c.post(f"/feeds/rigenera/class/{w.klass.pk}/").status_code == 302
    html = page(w.admin)
    assert "nome: ROSSI MARIO" in html
    assert "nome: 1A" in html


def test_old_shape_entries_render_once_without_a_join(w):
    write(
        audit.HOURS_ADJUSTED,
        {
            "assegnazione": 1,
            "ore_erogate_prima": 0,
            "ore_erogate_dopo": 5,
            "ore": 5,
            "motivo": "vecchio motivo",
        },
    )
    write(audit.FEED_REGENERATED, {"tipo": "teacher", "nome": "ROSSI MARIO"})
    with CaptureQueriesContext(connection) as ctx:
        html = page(w.admin)
    assert html.count("motivo: vecchio motivo") == 1
    assert html.count("nome: ROSSI MARIO") == 1
    assert GONE not in html
    joins = [q["sql"] for q in ctx.captured_queries if "ledger_ledgerentry" in q["sql"]]
    assert joins == []


def test_missing_ledger_entry_or_other_school_reason(w):
    other = make_school("Altra")
    foreign = make_assignment(other, "BIANCHI", "2B")
    secret = LedgerEntry.objects.create(
        assignment=foreign, hours=3, kind=EntryKind.ADJUSTMENT, reason="segreto altrui"
    )
    write(audit.HOURS_ADJUSTED, {"assegnazione": 1, "voce": 999999, "ore": 1})
    write(audit.HOURS_ADJUSTED, {"assegnazione": 1, "voce": secret.pk, "ore": 1})
    html = page(w.admin)
    assert html.count(f"motivo: {GONE}") == 2
    assert "segreto altrui" not in html


def test_missing_feed_targets_and_unknown_tipo(w):
    other = make_school("Altra")
    foreign = make_assignment(other, "BIANCHI LUIGI", "2B")
    gone = Teacher.objects.create(school=w.school, name="CANCELLATO", type="ASSUNTO")
    gone_pk = gone.pk
    gone.delete()
    write(audit.FEED_REGENERATED, {"tipo": "teacher", "id": gone_pk})
    write(audit.FEED_REGENERATED, {"tipo": "teacher", "id": foreign.teacher.pk})
    write(audit.FEED_REGENERATED, {"tipo": "class", "id": foreign.school_class.pk})
    write(audit.FEED_REGENERATED, {"tipo": "class", "id": 999999})
    write(audit.FEED_REGENERATED, {"tipo": "altro", "id": 1})
    write(audit.FEED_REGENERATED, {"tipo": "teacher", "id": "abc"})
    html = page(w.admin)
    assert html.count(f"nome: {GONE}") == 6
    assert "BIANCHI" not in html and "2B" not in html


def seed_new_shape(w, n):
    for i in range(n):
        entry = LedgerEntry.objects.create(
            assignment=w.asg, hours=1, kind=EntryKind.ADJUSTMENT, reason=f"r{i}"
        )
        kind = ("voce", "teacher", "class")[i % 3]
        if kind == "voce":
            write(audit.HOURS_ADJUSTED, {"assegnazione": w.asg.pk, "voce": entry.pk})
        else:
            target = w.teacher.pk if kind == "teacher" else w.klass.pk
            write(audit.FEED_REGENERATED, {"tipo": kind, "id": target})


def count_queries(user):
    with CaptureQueriesContext(connection) as ctx:
        page(user)
    return len(ctx.captured_queries)


def test_queries_are_bounded_whatever_the_page_content(w):
    plain = count_queries(w.admin)  # empty log
    write(audit.GENERATE, {"i": 0})
    without_joins = count_queries(w.admin)
    seed_new_shape(w, 3)  # smallest page using all three lookups
    two = count_queries(w.admin)
    seed_new_shape(w, 22)
    full = count_queries(w.admin)
    assert plain <= without_joins
    assert full == two
    assert full - without_joins <= 3


def test_free_text_is_escaped(w):
    adjust_delivered_hours(w.asg, 2, w.admin, "<script>alert(1)</script>")
    w.teacher.name = "<b>x</b>"
    w.teacher.save()
    c = Client()
    c.force_login(w.admin)
    c.get("/feeds/")
    c.post(f"/feeds/rigenera/teacher/{w.teacher.pk}/")
    html = page(w.admin)
    assert "&lt;script&gt;" in html and "<script>alert(1)" not in html
    assert "&lt;b&gt;" in html and "<b>x</b>" not in html


def test_long_reason_is_shown_whole(w):
    reason = "x" * 300
    adjust_delivered_hours(w.asg, 2, w.admin, reason)
    assert f"motivo: {reason}" in page(w.admin)


def test_segreteria_is_still_refused(w):
    sec = User.objects.create_user("sec@example.com", "pw-long-enough", "SEGRETERIA")
    c = Client()
    c.force_login(sec)
    assert c.get("/audit/").status_code in (302, 403)

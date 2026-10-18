# ruff: noqa: E501, F811
"""H10 conflict text names the subject (issue 137); nothing else changes."""

import dataclasses
import datetime
import re

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from scheduling import tasks
from scheduling.context import build_week_context
from scheduling.models import SolveJob, Timetable
from scheduling.solver import ASSUNTO, CONTRATTO, PRATICA, solve
from school.models import Assignment, CalendarEntry, Class, Subject, Teacher
from tests.test_solver_quasi_hard import MON, A, cells, day_cap, make
from tests.test_timetable_view import (  # noqa: F401
    PAGE,
    admin,
    fixed_today,
    html,
    school,
)

TEXT = "Le ore di pratica di {s} per la classe A lunedì non formano un unico blocco di almeno 3 ore"


def ctx_h10(**kw):
    """2 practice hours on a 6-hour day: the minimum block of 3 is unreachable."""
    return make(
        [A(1, "P", "A", 2, "LAB", PRATICA), A(2, "TT", "A", 4, "TEO")],
        {"A": day_cap(MON, 6)},
        {"P": ASSUNTO, "TT": CONTRATTO},
        availability={"TT": cells(MON, [0, 1, 2, 3])},
        **kw,
    )


def h10(res):
    (c,) = [c for c in res.conflicts if c.kind == "H10"]
    return c


def test_description_uses_subject_name():
    c = h10(solve(ctx_h10(subject_names={"LAB": "Laboratorio di cucina"})))
    assert c.description == TEXT.format(s="Laboratorio di cucina")
    assert "LAB" not in c.description


@pytest.mark.parametrize("names", [{}, {"OTHER": "Altro"}])
def test_description_falls_back_to_id(names):
    c = h10(solve(ctx_h10(subject_names=names)))
    assert c.description == TEXT.format(s="LAB")


def test_semantics_unchanged_with_names():
    plain = solve(ctx_h10())
    named = solve(ctx_h10(subject_names={"LAB": "Laboratorio di cucina"}))
    assert named.status == plain.status
    assert named.lessons == plain.lessons
    assert named.override_penalty == plain.override_penalty
    assert named.violations == plain.violations
    assert named.quality_score == plain.quality_score
    strip = [dataclasses.replace(c, description="") for c in named.conflicts]
    assert strip == [dataclasses.replace(c, description="") for c in plain.conflicts]
    assert [i["chiave"] for i in named.conflict_items] == [
        i["chiave"] for i in plain.conflict_items
    ]


def test_two_subjects_same_day_each_conflict_has_its_own_name():
    ctx = make(
        [
            A(1, "P", "A", 2, "LAB1", PRATICA),
            A(2, "P", "A", 2, "LAB2", PRATICA),
            A(3, "TT", "A", 2, "TEO"),
        ],
        {"A": day_cap(MON, 6)},
        {"P": ASSUNTO, "TT": CONTRATTO},
        availability={"TT": cells(MON, [2, 3])},
        subject_names={"LAB1": "Cucina", "LAB2": "Sala"},
    )
    res = solve(ctx)
    text = " ".join(c.description for c in res.conflicts)
    assert "di Cucina per" in text and "di Sala per" in text
    assert "LAB" not in text


# ------------------------------------------------------------ Django level

WEEK = datetime.date(2026, 10, 12)


@pytest.fixture
def world(school):
    """Subject and class pks above 9 and different from each other."""

    class W:
        pass

    w = W()
    w.school = school
    for i in range(12):
        Class.objects.create(school=school, name=f"Z{i}").delete()
        Subject.objects.create(
            school=school, name=f"Z{i}", kind="TEORIA", cognitive_weight="MEDIO"
        ).delete()
    Class.objects.create(school=school, name="SPARE")
    Subject.objects.create(
        school=school, name="SPARE", kind="TEORIA", cognitive_weight="MEDIO"
    ).delete()
    Subject.objects.create(
        school=school, name="SPARE2", kind="TEORIA", cognitive_weight="MEDIO"
    ).delete()
    w.klass = Class.objects.create(school=school, name="1A", group="PRIME")
    w.lab = Subject.objects.create(
        school=school,
        name="Laboratorio di cucina",
        kind="PRATICA",
        cognitive_weight="MEDIO",
    )
    w.teo = Subject.objects.create(
        school=school, name="Italiano", kind="TEORIA", cognitive_weight="MEDIO"
    )
    assert w.lab.pk > 9 and w.lab.pk != w.klass.pk
    w.p = Teacher.objects.create(
        school=school, name="ROSSI MARIO", type="ASSUNTO", first_time=False
    )
    w.t = Teacher.objects.create(
        school=school, name="BIANCHI ANNA", type="ASSUNTO", first_time=False
    )
    Assignment.objects.create(
        school=school, teacher=w.p, school_class=w.klass, subject=w.lab, ore_totali=2
    )
    Assignment.objects.create(
        school=school, teacher=w.t, school_class=w.klass, subject=w.teo, ore_totali=4
    )
    # Only Monday is a school day, so the 2 practice hours cannot reach 3.
    for i in range(1, 5):
        CalendarEntry.objects.create(
            school=school, date=WEEK + datetime.timedelta(days=i), is_closure=True
        )
    return w


def test_builder_fills_subject_names_of_the_school(world):
    other = type(world.school).objects.create(
        name="O",
        formative_year="2026-2027",
        year_start=world.school.year_start,
        year_end=world.school.year_end,
    )
    foreign = Subject.objects.create(
        school=other, name="Estranea", kind="TEORIA", cognitive_weight="MEDIO"
    )
    ctx = build_week_context(WEEK, world.school)
    assert ctx.subject_names == {
        world.lab.pk: "Laboratorio di cucina",
        world.teo.pk: "Italiano",
    }
    assert foreign.pk not in ctx.subject_names


def test_builder_adds_no_subject_query(world):
    with CaptureQueriesContext(connection) as q:
        build_week_context(WEEK, world.school)
    assert not [x for x in q.captured_queries if 'FROM "school_subject"' in x["sql"]]


def generate(world):
    job = SolveJob.objects.create(school=world.school, week_start=WEEK)
    tasks.run_job(job.pk)
    return Timetable.objects.get(school=world.school, week_start=WEEK)


def conflict_section(page):
    return page.split('aria-label="Conflitti"')[1].split("</section>")[0]


def test_end_to_end_stored_item_and_page(world, admin):
    tt = generate(world)
    (item,) = [i for i in tt.conflicts if i["kind"] == "H10"]
    assert (
        "Le ore di pratica di Laboratorio di cucina per la classe"
        in item["description"]
    )
    assert not re.search(r"pratica di \d+", item["description"])
    section = re.sub(r"<[^>]+>", "", conflict_section(html(admin.get(PAGE))))
    assert (
        "Le ore di pratica di Laboratorio di cucina per la classe 1A lunedì non formano"
        in section
    )
    assert not re.search(r"pratica di \d+", section)


def test_subject_name_is_escaped(world, admin):
    Subject.objects.filter(pk=world.lab.pk).update(name="<script>alert(1)</script>")
    generate(world)
    page = html(admin.get(PAGE))
    assert "&lt;script&gt;" in conflict_section(page)
    assert "<script>alert(1)" not in page

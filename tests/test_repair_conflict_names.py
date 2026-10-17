# ruff: noqa: E501, F811
"""Repair proposal panel: class and teacher names in the conflicts, at render time."""

import dataclasses
import datetime
import re

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from scheduling import repair
from scheduling.models import JobStatus, SolveJob
from school.models import Class, School, Teacher
from tests.test_conflict_names import TEXTS
from tests.test_repair import (  # noqa: F401
    MON,
    PAGE,
    STATUS,
    absent,
    admin,
    confirm_url,
    dismiss_url,
    html,
    job_of,
    rw,
    seg,
    start,
)

REF_RE = re.compile(r"(classe|docente|contrattista) \d+(?![\dA-Za-z])")


@pytest.fixture
def named(rw):
    """Class 1A / teacher ROSSI MARIO with pks above 9 and different."""
    Class.objects.filter(pk=rw.classes[0].pk).update(name="OLD-A")
    for i in range(12):
        Class.objects.create(school=rw.school, name=f"Z{i}").delete()
        Teacher.objects.create(
            school=rw.school, name=f"T{i}", type="ASSUNTO", first_time=False
        ).delete()
    Class.objects.create(school=rw.school, name="SPARE")
    klass = Class.objects.create(school=rw.school, name="1A")
    teacher = Teacher.objects.create(
        school=rw.school, name="ROSSI MARIO", type="ASSUNTO", first_time=False
    )
    assert klass.pk > 9 and teacher.pk > 9 and klass.pk != teacher.pk
    return klass, teacher


def solver_item(klass, teacher, text):
    return {
        "kind": "H4",
        "chiave": "x",
        "description": text,
        "suggested_action": {"action_type": "override_availability"},
        "classe_id": klass.pk if klass else None,
        "docente_id": teacher.pk if teacher else None,
    }


def finished_job(admin, rw, monkeypatch, items):
    """A real REPAIR job whose solver output reports `items` as conflicts."""
    real = repair.solve_exclusive

    def fake(ctx):
        return dataclasses.replace(real(ctx), conflict_items=items)

    monkeypatch.setattr(repair, "solve_exclusive", fake)
    return start(admin, absent(rw))


def set_result(job, **keys):
    job.refresh_from_db()
    job.result = {**job.result, **keys}
    job.save(update_fields=["result"])
    return job


def panel(page):
    return page.split('aria-label="Conflitti della proposta"')[1].split("</section>")[0]


def lis(page):
    return re.findall(r"<li>(.*?)</li>", panel(page), re.S)


def test_names_in_page_and_poll(named, rw, admin, monkeypatch):
    klass, teacher = named
    text = TEXTS["H4"].format(c=klass.pk, t=teacher.pk)
    job = finished_job(admin, rw, monkeypatch, [solver_item(klass, teacher, text)])
    for url in (PAGE, STATUS):
        page = html(admin.get(url))
        assert "Il docente ROSSI MARIO ha una lezione con la classe 1A" in page
        assert text not in page
        assert "Conflitti della proposta (1)" in page
    assert job.result["conflicts"] == [text]


def test_stored_shape_and_untouched_by_render_confirm_dismiss(
    named, rw, admin, monkeypatch
):
    klass, teacher = named
    text = TEXTS["H4"].format(c=klass.pk, t=teacher.pk)
    job = finished_job(admin, rw, monkeypatch, [solver_item(klass, teacher, text)])
    assert job.result["conflicts"] == [text]
    assert job.result["conflict_refs"] == [
        {"classe_id": klass.pk, "docente_id": teacher.pk}
    ]
    before = SolveJob.objects.get(pk=job.pk).result["conflicts"]
    admin.get(PAGE)
    admin.get(STATUS)
    for url in (confirm_url(job), dismiss_url(job)):
        admin.post(url)
        job.refresh_from_db()
        assert job.result["conflicts"] == before == [text]
        assert "ROSSI" not in str(job.result) and "1A" not in str(
            job.result["conflicts"]
        )


def test_base_result_has_empty_refs(rw, admin):
    job = start(admin, absent(rw))
    assert len(job.result["conflict_refs"]) == len(job.result["conflicts"])


@pytest.mark.parametrize("mode", ["old", "mismatch"])
def test_old_and_mismatched_results_show_stored_text(
    named, rw, admin, monkeypatch, mode
):
    klass, teacher = named
    texts = [f"Il docente {teacher.pk} con la classe {klass.pk}", "Altro 5", ""]
    job = finished_job(admin, rw, monkeypatch, [])
    if mode == "old":
        job.refresh_from_db()
        job.result.pop("conflict_refs", None)
        job.result["conflicts"] = texts
        job.save(update_fields=["result"])
    else:
        set_result(
            job,
            conflicts=texts,
            conflict_refs=[{"classe_id": klass.pk, "docente_id": 1}],
        )
    for url in (PAGE, STATUS):
        response = admin.get(url)
        assert response.status_code == 200
        page = html(response)
        assert f"Il docente {teacher.pk} con la classe {klass.pk}" in panel(page)
        assert "ROSSI MARIO" not in panel(page) and "1A" not in panel(page)
        assert "Conflitti della proposta (3)" in page


def test_unknown_null_and_foreign_ids(named, rw, admin, monkeypatch):
    klass, teacher = named
    other = School.objects.create(
        name="O",
        formative_year="2026-2027",
        year_start=MON,
        year_end=MON + datetime.timedelta(days=300),
    )
    foreign = Class.objects.create(school=other, name="FOREIGN")
    foreign_t = Teacher.objects.create(
        school=other, name="STRANIERO X", type="ASSUNTO", first_time=False
    )
    gone = Class.objects.create(school=rw.school, name="GONE")
    gone_pk = gone.pk
    gone.delete()
    texts = [
        f"Il docente {foreign_t.pk} con la classe {foreign.pk}",
        f"Il docente {teacher.pk} con la classe {gone_pk}",
        f"Il docente {teacher.pk} con la classe {klass.pk}",
    ]
    items = [
        solver_item(foreign, foreign_t, texts[0]),
        solver_item(type("K", (), {"pk": gone_pk}), teacher, texts[1]),
        solver_item(None, None, texts[2]),
    ]
    finished_job(admin, rw, monkeypatch, items)
    response = admin.get(PAGE)
    assert response.status_code == 200
    page = html(response)
    assert "FOREIGN" not in page and "STRANIERO" not in page
    assert texts[0] in page
    assert f"Il docente ROSSI MARIO con la classe {gone_pk}" in page
    assert texts[2] in page


@pytest.mark.parametrize("kind", sorted(TEXTS))
def test_every_solver_pattern(named, rw, admin, monkeypatch, kind):
    klass, teacher = named
    text = TEXTS[kind].format(c=klass.pk, t=teacher.pk)
    finished_job(admin, rw, monkeypatch, [solver_item(klass, teacher, text)])
    plain = re.sub(r"<[^>]+>", "", " ".join(lis(html(admin.get(PAGE)))))
    assert not REF_RE.search(plain)
    assert "ROSSI MARIO" in plain or "classe 1A" in plain


def test_whole_number_and_merged_texts(named, rw, admin, monkeypatch):
    klass, teacher = named
    k = klass.pk
    texts = [
        f"classe {k}1 e classe {k} con {k} ore, 3 ore, classe {k}.5h; docente {teacher.pk}0",
        f"La classe {k} lunedì x; La classe {k} martedì y",
    ]
    finished_job(
        admin, rw, monkeypatch, [solver_item(klass, teacher, t) for t in texts]
    )
    found = lis(html(admin.get(PAGE)))
    assert found == [
        f"classe {k}1 e classe 1A con {k} ore, 3 ore, classe {k}.5h; docente {teacher.pk}0",
        "La classe 1A lunedì x; La classe 1A martedì y",
    ]


def test_names_are_escaped(named, rw, admin, monkeypatch):
    klass, teacher = named
    Teacher.objects.filter(pk=teacher.pk).update(name="<script>alert(1)</script>")
    Class.objects.filter(pk=klass.pk).update(name='"><img src=x onerror=alert(1)>')
    text = TEXTS["H4"].format(c=klass.pk, t=teacher.pk)
    finished_job(admin, rw, monkeypatch, [solver_item(klass, teacher, text)])
    for url in (PAGE, STATUS):
        page = html(admin.get(url))
        assert "&lt;script&gt;" in page and "&lt;img src=x" in page
        assert "<script>alert(1)" not in page and "<img src=x" not in page


def test_no_id_leak(named, rw, admin, monkeypatch):
    klass, teacher = named
    items = [
        solver_item(klass, teacher, TEXTS[k].format(c=klass.pk, t=teacher.pk))
        for k in sorted(TEXTS)
    ]
    finished_job(admin, rw, monkeypatch, items)
    for li in lis(html(admin.get(PAGE))):
        assert not REF_RE.search(re.sub(r"<[^>]+>", "", li))


def test_query_count_is_flat(admin, rw, monkeypatch):
    def count(n):
        SolveJob.objects.filter(kind="REPAIR").delete()
        classes = [
            Class.objects.create(school=rw.school, name=f"C{n}-{i}") for i in range(n)
        ]
        teachers = [
            Teacher.objects.create(
                school=rw.school, name=f"D{n}-{i}", type="ASSUNTO", first_time=False
            )
            for i in range(n)
        ]
        items = [
            solver_item(c, t, f"Il docente {t.pk} con la classe {c.pk}")
            for c, t in zip(classes, teachers, strict=True)
        ]
        finished_job(admin, rw, monkeypatch, items)
        with CaptureQueriesContext(connection) as ctx:
            assert admin.get(STATUS).status_code == 200
        return len(ctx)

    one, many = count(1), count(30)
    assert one == many


def test_lookup_adds_at_most_two_queries_and_none_without_proposal(
    named, rw, admin, monkeypatch
):
    klass, teacher = named
    text = TEXTS["H4"].format(c=klass.pk, t=teacher.pk)
    job = finished_job(admin, rw, monkeypatch, [solver_item(klass, teacher, text)])

    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert admin.get(STATUS).status_code == 200
        return len(ctx)

    with_names = count()
    set_result(job, conflict_refs=[])
    base = count()
    assert 0 < with_names - base <= 2
    set_result(job, conflict_refs=[{"classe_id": klass.pk, "docente_id": teacher.pk}])
    SolveJob.objects.filter(pk=job.pk).update(status=JobStatus.RUNNING)
    running = count()
    assert running < base
    SolveJob.objects.filter(pk=job.pk).update(status=JobStatus.DONE)
    set_result(job, conflicts=[], conflict_refs=[])
    assert count() <= base


def test_segreteria_gets_no_proposal(named, rw, admin, seg, monkeypatch):
    klass, teacher = named
    text = TEXTS["H4"].format(c=klass.pk, t=teacher.pk)
    finished_job(admin, rw, monkeypatch, [solver_item(klass, teacher, text)])
    assert "Conflitti della proposta" not in html(seg.get(PAGE))

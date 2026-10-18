# ruff: noqa: E501, F811
"""Conflict list of the timetable page: names instead of ids, at render time."""

import datetime
import re

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from scheduling.board import build_board, conflict_rows
from scheduling.models import ConflictDecision, Timetable
from school.models import Class, School, Teacher
from tests.test_timetable_view import (  # noqa: F401
    MON,
    PAGE,
    admin,
    fixed_today,
    html,
    school,
    seg,
    set_conflicts,
    world,
)  # noqa: F401

# Kind -> real description templates of the solver ({c} class id, {t} teacher id).
TEXTS = {
    "classe_unfilled": "Ora non coperta per la classe {c} lunedì alle 09:00: nessun docente",
    "classe_unfilled_hours": "3 ore non coperte per la classe {c} lunedì",
    "docente_double_booking": "Il docente {t} lunedì alle 09:00 è in due classi nella stessa ora",
    "monte_ore": "Il docente {t} supera il monte ore con la classe {c} lunedì alle 09:00 (1 ora oltre il residuo)",
    "target_deviation": "Il docente {t} con la classe {c}: 3 ore assegnate, target 5, scarto -2 (target derivato da: x)",
    "teoria_consecutive": "Troppe ore di teoria consecutive del docente {t} con la classe {c} lunedì",
    "contractor_gap": "Il contrattista {t} lunedì ha un'ora buca tra due lezioni",
    "single_classe_day": "Il docente {t} passa la giornata lunedì con la sola classe {c}",
    "H4": "Il docente {t} ha una lezione con la classe {c} LUNEDI alle 09:00 ma non è disponibile",
    "H9": "La classe {c} non inizia alle 08:00 il venerdì (il blocco di lezioni dovrebbe iniziare alle 8:00)",
    "H10": "Le ore di pratica di Laboratorio di cucina per la classe {c} LUNEDI non formano un unico blocco di almeno 3 ore",
    "H11": "Il docente {t} ha 4 ore con la classe {c} LUNEDI, oltre il massimo di 3",
}


@pytest.fixture
def named(world):
    """Class 1A / teacher ROSSI MARIO with pks above 9 and different."""
    Class.objects.filter(pk=world.a.pk).update(name="OLD-A")
    Teacher.objects.filter(pk=world.rossi.pk).update(name="OLD ROSSI")
    for i in range(12):
        Class.objects.create(school=world.school, name=f"Z{i}").delete()
        Teacher.objects.create(
            school=world.school, name=f"T{i}", type="ASSUNTO", first_time=False
        ).delete()
    Class.objects.create(school=world.school, name="SPARE")  # offset the pks
    klass = Class.objects.create(school=world.school, name="1A", group="PRIME")
    teacher = Teacher.objects.create(
        school=world.school, name="ROSSI MARIO", type="ASSUNTO", first_time=False
    )
    assert klass.pk > 9 and teacher.pk > 9 and klass.pk != teacher.pk
    return klass, teacher


def item(klass, teacher, text, **kw):
    return {
        "kind": "H4",
        "chiave": f"H4|{klass.pk}|{teacher.pk}|LUNEDI|1",
        "description": text,
        "suggested_action": {"action_type": "override_availability", "label": "Ignora"},
        "classe_id": klass.pk,
        "docente_id": teacher.pk,
        "giorno": "LUNEDI",
        **kw,
    }


def conflict_text(page):
    section = page.split('aria-label="Conflitti"')[1].split("</section>")[0]
    return re.findall(r"<li>(.*?)</li>", section, re.S)


def test_names_replace_ids(named, world, admin):
    klass, teacher = named
    set_conflicts(
        world, [item(klass, teacher, TEXTS["H4"].format(c=klass.pk, t=teacher.pk))]
    )
    text = html(admin.get(PAGE))
    assert "Il docente ROSSI MARIO ha una lezione con la classe 1A" in text
    assert f"docente {teacher.pk} " not in text
    assert f"classe {klass.pk} " not in text


@pytest.mark.parametrize("kind", sorted(TEXTS))
def test_every_solver_pattern(named, world, admin, kind):
    klass, teacher = named
    set_conflicts(
        world,
        [item(klass, teacher, TEXTS[kind].format(c=klass.pk, t=teacher.pk), kind=kind)],
    )
    li = " ".join(conflict_text(html(admin.get(PAGE))))
    plain = re.sub(r"<[^>]+>", "", li)
    assert not re.search(r"(classe|docente|contrattista) \d+\b", plain)
    assert "ROSSI MARIO" in plain or "classe 1A" in plain


def test_whole_number_match(named):
    klass, teacher = named
    k = klass.pk
    text = f"classe {k}1 e classe {k} con {k} ore, 3 ore, classe {k}.5h; docente {teacher.pk}0"
    rows = conflict_rows([item(klass, teacher, text)], klass.school)
    assert rows[0]["text"] == (
        f"classe {k}1 e classe 1A con {k} ore, 3 ore, classe {k}.5h; docente {teacher.pk}0"
    )


def test_merged_description_all_occurrences(named):
    klass, teacher = named
    c = klass.pk
    text = f"La classe {c} lunedì x; La classe {c} martedì y"
    rows = conflict_rows([item(klass, teacher, text)], klass.school)
    assert rows[0]["text"] == "La classe 1A lunedì x; La classe 1A martedì y"


def test_unknown_or_missing_ids_unchanged(named, world, admin):
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
    items = [
        item(
            foreign, foreign_t, f"Il docente {foreign_t.pk} con la classe {foreign.pk}"
        ),
        item(
            klass,
            teacher,
            "Il docente 99999 con la classe 99998",
            classe_id=99998,
            docente_id=99999,
        ),
        item(klass, teacher, f"classe {klass.pk}", classe_id=None, docente_id=None),
        item(klass, teacher, "nessun id qui"),
    ]
    set_conflicts(world, items)
    response = admin.get(PAGE)
    assert response.status_code == 200
    text = html(response)
    assert f"Il docente {foreign_t.pk} con la classe {foreign.pk}" in text
    assert "FOREIGN" not in text and "STRANIERO" not in text
    assert "Il docente 99999 con la classe 99998" in text
    assert f"classe {klass.pk}" in text
    assert "nessun id qui" in text


def test_names_are_escaped(world, admin):
    klass = Class.objects.create(
        school=world.school, name='"><img src=x onerror=alert(1)>'
    )
    teacher = Teacher.objects.create(
        school=world.school,
        name="<script>alert(1)</script>",
        type="ASSUNTO",
        first_time=False,
    )
    set_conflicts(
        world,
        [item(klass, teacher, f"Il docente {teacher.pk} con la classe {klass.pk}")],
    )
    text = html(admin.get(PAGE))
    section = text.split('aria-label="Conflitti"')[1].split("</section>")[0]
    assert "&lt;script&gt;" in section
    assert "<script>alert(1)" not in text
    assert "<img src=x onerror" not in text


def test_stored_data_untouched_and_deterministic(named, world, admin):
    klass, teacher = named
    items = [item(klass, teacher, TEXTS["H4"].format(c=klass.pk, t=teacher.pk))]
    set_conflicts(world, items)
    before = Timetable.objects.get(pk=world.tt.pk)
    first = conflict_text(html(admin.get(PAGE)))
    second = conflict_text(html(admin.get(PAGE)))
    after = Timetable.objects.get(pk=world.tt.pk)
    assert first == second
    assert after.conflicts == before.conflicts == items
    assert after.version == before.version


def test_approval_by_chiave_still_hides(named, world, admin):
    klass, teacher = named
    it = item(klass, teacher, TEXTS["H4"].format(c=klass.pk, t=teacher.pk))
    set_conflicts(world, [it])
    ConflictDecision.objects.create(
        timetable=Timetable.objects.get(pk=world.tt.pk), chiave=it["chiave"]
    )
    assert "Conflitti (" not in html(admin.get(PAGE))


def test_admin_and_secretary_views(named, world, admin, seg):
    klass, teacher = named
    set_conflicts(
        world, [item(klass, teacher, TEXTS["H4"].format(c=klass.pk, t=teacher.pk))]
    )
    a = html(admin.get(PAGE))
    s_response = seg.get(PAGE)
    s = html(s_response)
    assert s_response.status_code == 200
    assert "Approva" in a and "tt-quick-btn" in a
    assert "Approva" not in s and "tt-quick-btn" not in s
    assert (
        conflict_text(a)[0].split("<button")[0].strip() == conflict_text(s)[0].strip()
    )
    for page in (a, s):
        plain = re.sub(r"<[^>]+>", " ", " ".join(conflict_text(page)))
        assert not re.search(r"(classe|docente|contrattista) \d+\b", plain)
        assert "ROSSI MARIO" in plain


def test_query_count_is_flat(world, admin):
    def make(n):
        out = []
        for i in range(n):
            k = Class.objects.create(school=world.school, name=f"C{n}-{i}")
            t = Teacher.objects.create(
                school=world.school,
                name=f"TEA {n} {i}",
                type="ASSUNTO",
                first_time=False,
            )
            out.append(
                item(
                    k,
                    t,
                    f"Il docente {t.pk} con la classe {k.pk}",
                    chiave=f"H4|{k.pk}|{t.pk}|LUNEDI|1",
                )
            )
        return out

    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert admin.get(PAGE).status_code == 200
        return len(ctx)

    set_conflicts(
        world, [item(*_pair(world), "nessun id", classe_id=None, docente_id=None)]
    )
    none = count()  # a conflict is listed but no name is looked up
    set_conflicts(world, make(1))
    one = count()
    set_conflicts(world, make(30))
    many = count()
    assert one == many
    assert one - none <= 2


def test_classes_ordered_by_name(world):
    # (school, name) is unique, so the pk tie-break of order_by("name", "pk")
    # cannot be observed within one school; the name order is what is pinned.
    school = world.school
    created = [Class.objects.create(school=school, name=n) for n in ("2B", "1C", "1B0")]
    days = build_board(school, MON, world.tt)
    names = [r["school_class"].name for r in days[0]["rows"]]
    assert names == sorted(names)
    assert names == ["1A", "1B", "1B0", "1C", "2B"]
    assert len(created) == 3


def _pair(world):
    return world.a, world.rossi

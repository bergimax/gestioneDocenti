# ruff: noqa: F811  (fixtures imported from test_timetable_view)
import re
from pathlib import Path

import pytest

from scheduling.board import build_board
from school.models import Subject, Teacher
from tests.test_timetable_view import (  # noqa: F401
    GRID,
    MON,
    PAGE,
    admin,
    fixed_today,
    html,
    school,
    seg,
    set_conflicts,
    slot,
    world,
)

CSS = (Path(__file__).resolve().parent.parent / "static/css/app.css").read_text()


def card(text, **attrs):
    return re.findall(r'<div class="tt-card[^"]*"[^>]*>', text)


@pytest.fixture
def prat(world):
    return Subject.objects.create(
        school=world.school, name="Lab", kind="PRATICA", cognitive_weight="BASSO"
    )


def test_cells_carry_teacher_id_and_kind(world, prat):
    slot(world, world.a, world.rossi)
    days = build_board(world.school, MON, world.tt)
    cells = [c for r in days[0]["rows"] for c in r["cells"]]
    lesson = next(c for c in cells if c["kind"] == "lesson")
    assert lesson["teacher_id"] == world.rossi.pk
    assert lesson["subject_kind"] == "TEORIA"
    other = next(c for c in cells if c["kind"] != "lesson")
    assert "teacher_id" not in other and "subject_kind" not in other


def test_card_attributes_and_partner_copy(world, admin):
    slot(world, world.a, world.rossi, paired=True, paired_class=world.b)
    text = html(admin.get(PAGE))
    cards = card(text)
    assert len(cards) == 2
    for c in cards:
        assert f'data-teacher-id="{world.rossi.pk}"' in c
        assert 'data-kind="TEORIA"' in c
        assert 'title="Teoria"' in c
    assert text.count("data-teacher-id") == 2  # empty and stage cells: none


def test_pratica_kind(world, admin, prat):
    s = slot(world, world.a, world.rossi)
    s.subject = prat
    s.save()
    assert 'data-kind="PRATICA"' in html(admin.get(PAGE))


def test_same_surname_different_ids(world, admin):
    luca = Teacher.objects.create(
        school=world.school, name="ROSSI LUCA", type="ASSUNTO", first_time=False
    )
    slot(world, world.a, world.rossi)
    slot(world, world.b, luca)
    text = html(admin.get(PAGE))
    assert text.count('<span class="tt-teacher">ROSSI</span>') == 2
    ids = set(re.findall(r'data-teacher-id="(\d+)"', text))
    assert len(ids) == 2


@pytest.mark.parametrize("who", ["admin", "seg"])
def test_button_is_alpine_only(world, who, request):
    client = request.getfixturevalue(who)
    slot(world, world.a, world.rossi)
    text = html(client.get(PAGE))
    button = re.search(r'<button type="button" class="tt-teacher-btn"[^>]*>', text)
    assert button
    b = button.group(0)
    assert "x-on:click" in b and ":aria-pressed" in b and "hx-" not in b
    assert "aria-label=" in b
    assert ":class=" in card(text)[0]
    assert "Genera" not in text or who == "admin"
    for word in ("Approva", "Rifiuta", "assegna"):
        assert who == "admin" or word not in text


def test_admin_keeps_move_attributes(world, admin):
    s = slot(world, world.a, world.rossi)
    text = html(admin.get(PAGE))
    assert f'data-slot-id="{s.pk}"' in text
    assert f'data-drop="classe-{world.a.pk}"' in text


def test_state_outside_polled_fragment(world, admin):
    slot(world, world.a, world.rossi)
    assert "x-data" not in html(admin.get(GRID))
    text = html(admin.get(PAGE))
    assert text.count("teacher: null") == 1
    assert text.index("teacher: null") < text.index('id="timetable-body"')


@pytest.mark.parametrize("who", ["admin", "seg"])
def test_legend_with_timetable(world, who, request):
    client = request.getfixturevalue(who)
    text = html(client.get(PAGE))
    assert "Teoria" in text and "Pratica" in text


def test_empty_state_renders(school, admin):
    response = admin.get(PAGE)
    assert response.status_code == 200
    assert "tt-legend" not in html(response)


def rule(selector, exact=False):
    m = re.search(
        r"^" + re.escape(selector) + (r" \{" if exact else r"[^{]*\{") + r"([^}]*)\}",
        CSS,
        re.M,
    )
    assert m, selector
    return re.search(r"background:\s*(#\w+)", m.group(1)).group(1)


def test_css_colours_distinct():
    teoria = rule('.tt-card[data-kind="TEORIA"]')
    pratica = rule('.tt-card[data-kind="PRATICA"]')
    conflict = rule(".tt-conflict", exact=True)
    stage = rule(".tt-stage")
    assert len({teoria, pratica, conflict, stage}) == 4
    assert ".tt-highlight" in CSS


def test_pratica_conflict_stays_conflict(world, admin, prat):
    s = slot(world, world.a, world.rossi)
    s.subject = prat
    s.save()
    set_conflicts(
        world,
        [
            {
                "giorno": "LUNEDI",
                "ore_slot": 0,
                "classe_id": world.a.pk,
                "description": "x",
                "chiave": "k",
                "kind": "H1",
            }
        ],
    )
    text = html(admin.get(PAGE))
    assert 'class="tt-lesson tt-conflict" data-conflict="true"' in text
    assert 'data-kind="PRATICA"' in text

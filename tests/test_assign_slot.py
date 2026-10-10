import datetime
import re
from unittest import mock

import pytest

from availability import grid as g
from availability.models import Availability
from scheduling import views
from scheduling.models import ConflictDecision, Slot, TimetableState
from scheduling.solver import SolveResult, SolveStatus
from school.models import Assignment, CalendarEntry, Class, ClassPairing

D = datetime.date
MON = D(2026, 10, 12)
ASSIGN = f"/orario/{MON:%Y-%m-%d}/assegna/"
LIST = f"/orario/{MON:%Y-%m-%d}/assegnabili/"
APPROVE = f"/orario/{MON:%Y-%m-%d}/conflitti/approva/"
REJECT = f"/orario/{MON:%Y-%m-%d}/conflitti/rifiuta/"
PAGE = "/orario/"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(views, "today", lambda: D(2026, 10, 14))


def assign(
    client,
    w,
    klass=None,
    day="LUNEDI",
    hour=10,
    teacher=None,
    subject=None,
    version=1,
    **extra,
):
    data = {
        "classe": (klass or w.a).pk,
        "giorno": day,
        "ora_inizio": hour,
        "docente": (teacher or w.rossi).pk,
        "materia": (subject or w.mat).pk,
        "version": version,
    }
    data.update(extra)
    return client.post(ASSIGN, {k: v for k, v in data.items() if v is not None})


def state(w):
    w.tt.refresh_from_db()
    return (
        w.tt.version,
        w.tt.quality_score,
        w.tt.conflicts,
        sorted(
            Slot.objects.values_list(
                "pk", "school_class_id", "day", "start_hour", "paired_class_id"
            )
        ),
        sorted(ConflictDecision.objects.values_list("chiave", "action")),
    )


def html(response):
    return response.content.decode()


def kinds(w, **where_):
    w.tt.refresh_from_db()
    return [c for c in w.tt.conflicts if all(c.get(k) == v for k, v in where_.items())]


def cell(text, day, hour, klass):
    row = re.search(
        rf"<tr>\s*<th scope=\"row\">{klass}</th>(.*?)</tr>",
        text.split(f'aria-label="{DAY[day]}"')[1],
        re.S,
    ).group(1)
    m = re.search(
        rf'<td class="[^"]*" (?:data-conflict="true" )?data-day="{day}" '
        rf'data-hour="{hour}"[^>]*>(.*?)</td>',
        row,
        re.S,
    )
    assert m, f"no cell {day} {hour}"
    return m.group(0)


DAY = {"LUNEDI": "Lunedì", "MARTEDI": "Martedì"}


def refused(w, response, status, message, before):
    assert response.status_code == status
    assert state(w) == before
    text = html(response)
    assert 'id="timetable-body"' in text
    assert 'role="alert"' in text
    assert message in text


# ---- the list of assignable pairs ------------------------------------------


def test_list_has_each_assignment_once_sorted_by_subject(w, admin):
    r = admin.get(f"{LIST}{w.a.pk}/?giorno=LUNEDI&ora_inizio=10")
    assert r.status_code == 200
    text = html(r)
    subjects = re.findall(r'class="tt-pair-subject">([^<]*)<', text)
    assert subjects == ["Italiano", "Laboratorio", "Matematica"]
    assert text.count('name="docente"') == 3
    assert "BIANCHI ANNA" in text and "ROSSI MARIO" in text


def test_list_shows_the_remaining_hours(w, admin):
    # a_mat: 4 hours, 2 placed this week; a_lab: 4 hours, none placed
    text = html(admin.get(f"{LIST}{w.a.pk}/?giorno=LUNEDI&ora_inizio=10"))
    hours = dict(
        re.findall(
            r'class="tt-pair-subject">([^<]*)</span>.*?ore rimanenti: (-?\d+)',
            text,
            re.S,
        )
    )
    assert hours == {"Italiano": "2", "Laboratorio": "4", "Matematica": "2"}


def test_list_carries_what_the_assign_form_posts(w, admin):
    text = html(admin.get(f"{LIST}{w.a.pk}/?giorno=MARTEDI&ora_inizio=11"))
    assert f'name="classe" value="{w.a.pk}"' in text
    assert 'name="giorno" value="MARTEDI"' in text
    assert 'name="ora_inizio" value="11"' in text
    assert f'name="docente" value="{w.rossi.pk}"' in text
    assert f'name="materia" value="{w.mat.pk}"' in text
    assert 'name="version" value="1"' in text


def test_a_class_without_assignments_says_so(w, admin):
    empty = Class.objects.create(school=w.school, name="1C")
    r = admin.get(f"{LIST}{empty.pk}/?giorno=LUNEDI&ora_inizio=10")
    assert r.status_code == 200
    text = html(r)
    assert "Nessuna assegnazione per questa classe" in text
    assert 'name="docente"' not in text


def test_list_ignores_inactive_teachers_and_other_classes(w, admin):
    w.bianchi.active = False
    w.bianchi.save()
    text = html(admin.get(f"{LIST}{w.b.pk}/?giorno=LUNEDI&ora_inizio=10"))
    assert re.findall(r'class="tt-pair-subject">([^<]*)<', text) == ["Matematica"]


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?giorno=SABATO&ora_inizio=10",
        "?giorno=LUNEDI&ora_inizio=7",
        "?giorno=LUNEDI&ora_inizio=14",
        "?giorno=LUNEDI&ora_inizio=x",
    ],
)
def test_list_refuses_a_bad_cell(w, admin, query):
    r = admin.get(f"{LIST}{w.a.pk}/{query}")
    assert r.status_code == 400
    assert 'role="alert"' in html(r)


def test_list_of_an_unknown_class_is_404(w, admin):
    assert admin.get(f"{LIST}99999/?giorno=LUNEDI&ora_inizio=10").status_code == 404


def test_list_is_forbidden_to_segreteria(w, seg):
    assert seg.get(f"{LIST}{w.a.pk}/?giorno=LUNEDI&ora_inizio=10").status_code == 403


def test_list_refuses_an_approved_week(w, admin):
    w.tt.state = TimetableState.APPROVATO
    w.tt.save()
    r = admin.get(f"{LIST}{w.a.pk}/?giorno=LUNEDI&ora_inizio=10")
    assert r.status_code == 409
    assert "approvata" in html(r)
    assert 'name="docente"' not in html(r)


# ---- the grid offers "assegna" to ADMIN only -------------------------------


def test_admin_sees_assegna_on_empty_cells_only(w, admin):
    text = html(admin.get(f"{PAGE}?settimana={MON:%Y-%m-%d}"))
    free = cell(text, "LUNEDI", 10, "1A")
    assert "assegna" in free
    assert f"/assegnabili/{w.a.pk}/?giorno=LUNEDI&amp;ora_inizio=10" in free
    assert "assegna" not in cell(text, "LUNEDI", 8, "1A")


def test_segreteria_sees_no_assegna(w, seg):
    text = html(seg.get(f"{PAGE}?settimana={MON:%Y-%m-%d}"))
    assert "assegna" not in text and "tt-assign-btn" not in text


def test_stage_cells_offer_no_assegna(w, admin):
    CalendarEntry.objects.create(
        school=w.school, date=MON + datetime.timedelta(days=1), stage_class=w.a
    )
    text = html(admin.get(f"{PAGE}?settimana={MON:%Y-%m-%d}"))
    stage_cells = re.findall(r'<td class="tt-stage">.*?</td>', text, re.S)
    assert len(stage_cells) == 4  # 1A keeps its two Tuesday lessons
    assert not any("assegna" in c for c in stage_cells)
    assert "assegna" in cell(text, "MARTEDI", 10, "1B")


def test_approved_week_offers_no_assegna(w, admin):
    w.tt.state = TimetableState.APPROVATO
    w.tt.save()
    assert "tt-assign-btn" not in html(admin.get(f"{PAGE}?settimana={MON:%Y-%m-%d}"))


# ---- assigning ------------------------------------------------------------


def test_assign_creates_the_lesson_and_returns_the_grid(w, admin):
    r = assign(admin, w)
    assert r.status_code == 200
    slot = Slot.objects.get(school_class=w.a, day="LUNEDI", start_hour=10)
    assert (slot.teacher, slot.subject, slot.end_hour) == (w.rossi, w.mat, 11)
    assert (slot.paired, slot.paired_class) == (False, None)
    text = html(r)
    assert 'id="timetable-body"' in text
    assert f'data-slot-id="{slot.pk}"' in cell(text, "LUNEDI", 10, "1A")
    assert 'data-version="2"' in text


def test_assign_bumps_the_version_exactly_once(w, admin):
    assign(admin, w)
    w.tt.refresh_from_db()
    assert w.tt.version == 2


def test_assign_returns_the_updated_score_and_conflicts(w, admin):
    w.tt.quality_score, w.tt.quality_level = 12.5, "C"
    w.tt.save()
    text = html(assign(admin, w))
    w.tt.refresh_from_db()
    assert w.tt.quality_score != 12.5
    assert f"Conflitti ({len(w.tt.conflicts)})" in text


def test_assign_adds_only_the_chosen_lesson(w, admin):
    before = Slot.objects.count()
    assign(admin, w)
    assert Slot.objects.count() == before + 1


def test_assign_closes_the_panel_in_the_answer(w, admin):
    assert 'id="tt-assign-panel" hx-swap-oob="true"' in html(assign(admin, w))


# ---- a placement that breaks rules is accepted and reported ---------------


def test_teacher_already_teaching_elsewhere_is_accepted_and_reported(w, admin):
    w.slot(w.b, w.rossi, w.mat, "LUNEDI", 10)
    r = assign(admin, w)
    assert r.status_code == 200
    items = kinds(w, kind="docente_double_booking", docente_id=w.rossi.pk)
    assert [(i["giorno"], i["ore_slot"]) for i in items] == [("LUNEDI", 2)]
    assert Slot.objects.filter(school_class=w.a, day="LUNEDI", start_hour=10).exists()
    assert 'data-conflict="true"' in cell(html(r), "LUNEDI", 10, "1A")


def test_teacher_unavailable_is_accepted_and_reported(w, admin):
    w.rossi.type = "CONTRATTO"
    w.rossi.save()
    grid = g.default_grid()
    for slot in grid["martedi"]:
        slot["disponibile"] = False
    Availability.objects.create(
        school=w.school, teacher=w.rossi, week_start=MON, slots=grid
    )
    r = assign(admin, w, day="MARTEDI", hour=10)
    assert r.status_code == 200
    items = kinds(w, kind="H4", docente_id=w.rossi.pk, classe_id=w.a.pk)
    assert [i["giorno"] for i in items] == ["MARTEDI"]
    assert 'data-conflict="true"' in cell(html(r), "MARTEDI", 10, "1A")


def test_over_the_hours_budget_is_accepted_and_reported(w, admin):
    w.asg["a_mat"].ore_totali = 2  # both hours already placed
    w.asg["a_mat"].save()
    r = assign(admin, w)
    assert r.status_code == 200
    items = kinds(w, kind="monte_ore_exceeded", classe_id=w.a.pk)
    assert len(items) == 1  # one hour over the residual of two
    assert Slot.objects.filter(school_class=w.a, day="LUNEDI", start_hour=10).exists()


def practical_day(w):
    """1A on Monday: 8 Rossi, then a two-hour practical block at 10-11."""
    Slot.objects.filter(pk=w.slots["LUNEDI", "a_ita"].pk).delete()
    for hour in (10, 11):
        w.slot(w.a, w.bianchi, w.lab, "LUNEDI", hour)


def test_breaking_the_contiguous_practical_block_is_reported(w, admin):
    practical_day(w)
    ok = assign(admin, w, teacher=w.bianchi, subject=w.lab, hour=12)
    assert ok.status_code == 200
    assert not kinds(w, kind="H10", classe_id=w.a.pk)  # 10-12 is still whole
    Slot.objects.filter(school_class=w.a, day="LUNEDI", start_hour=12).delete()
    w.tt.refresh_from_db()
    r = assign(
        admin, w, teacher=w.bianchi, subject=w.lab, hour=13, version=w.tt.version
    )
    assert r.status_code == 200
    assert kinds(w, kind="H10", classe_id=w.a.pk, giorno="LUNEDI")
    assert 'data-conflict="true"' in cell(html(r), "LUNEDI", 13, "1A")


# ---- refused: the class cannot hold another hour ---------------------------


def fill_monday(w):
    w.slot(w.a, w.rossi, w.mat, "LUNEDI", 10)
    w.slot(w.a, w.bianchi, w.ita, "LUNEDI", 11)


def test_a_lesson_over_the_class_daily_maximum_is_refused(w, admin):
    fill_monday(w)  # 4 lessons in a 4-hour day
    before = state(w)
    r = assign(admin, w, hour=12)
    refused(w, r, 409, "tutte le ore di lezione", before)


def test_the_maximum_follows_the_calendar_hours(w, admin):
    CalendarEntry.objects.filter(date=MON).update(max_hours=6)
    fill_monday(w)
    assert assign(admin, w, hour=12).status_code == 200  # the 5th of 6
    w.slot(w.a, w.bianchi, w.ita, "LUNEDI", 13)
    w.tt.refresh_from_db()
    before = state(w)
    r = assign(admin, w, hour=9, version=w.tt.version)  # 7th hour of a 6-hour day
    assert r.status_code == 409 and state(w) == before


def test_a_stage_day_is_refused_with_the_stage_message(w, admin):
    CalendarEntry.objects.create(
        school=w.school, date=MON + datetime.timedelta(days=1), stage_class=w.a
    )
    before = state(w)
    r = assign(admin, w, day="MARTEDI", hour=12)
    refused(w, r, 409, "in stage", before)
    assert "non ha ore di lezione" not in html(r)


def test_a_group_stage_day_is_refused_with_the_stage_message(w, admin):
    w.a.group = "PRIME"
    w.a.save()
    CalendarEntry.objects.create(
        school=w.school,
        date=MON + datetime.timedelta(days=1),
        group="PRIME",
        is_group_stage=True,
    )
    before = state(w)
    refused(w, assign(admin, w, day="MARTEDI", hour=12), 409, "in stage", before)


def test_a_closed_day_is_refused_without_calling_it_stage(w, admin):
    before = state(w)
    r = assign(admin, w, day="MERCOLEDI", hour=8)
    refused(w, r, 409, "non ha ore di lezione", before)
    assert "stage" not in html(r).lower().split('role="alert"')[1].split("</div>")[0]


# ---- refused: bad input -----------------------------------------------------


def test_an_occupied_cell_is_refused(w, admin):
    before = state(w)
    refused(w, assign(admin, w, hour=9), 409, "già occupata", before)


def test_a_cell_taken_by_the_paired_row_of_another_class_is_occupied(w, admin):
    Slot.objects.filter(school_class=w.a, day="LUNEDI", start_hour=9).delete()
    w.slot(w.b, w.rossi, w.mat, "LUNEDI", 9, paired=True, paired_class=w.a)
    before = state(w)
    refused(w, assign(admin, w, hour=9), 409, "già occupata", before)


def bad_inputs(w):
    other = Class.objects.create(school=w.school, name="1C")
    return {
        "pair_not_an_assignment": dict(teacher=w.rossi, subject=w.ita),
        "other_class_assignment": dict(klass=other),
        "hour_7": dict(hour=7),
        "hour_14": dict(hour=14),
        "hour_text": dict(hour="x"),
        "hour_missing": dict(hour=None),
        "unknown_day": dict(day="SABATO"),
        "lowercase_day": dict(day="lunedi"),
        "unknown_class": dict(classe="99999"),
        "unknown_teacher": dict(docente="99999"),
        "unknown_subject": dict(materia="99999"),
        "text_teacher": dict(docente="abc"),
        "missing_version": dict(version=None),
    }


@pytest.mark.parametrize(
    "name",
    [
        "pair_not_an_assignment",
        "other_class_assignment",
        "hour_7",
        "hour_14",
        "hour_text",
        "hour_missing",
        "unknown_day",
        "lowercase_day",
        "unknown_class",
        "unknown_teacher",
        "unknown_subject",
        "text_teacher",
        "missing_version",
    ],
)
def test_bad_input_is_a_400_and_changes_nothing(w, admin, name):
    kw = bad_inputs(w)[name]
    before = state(w)
    r = assign(admin, w, **kw)
    assert r.status_code == 400
    assert state(w) == before
    assert 'role="alert"' in html(r)


def test_an_inactive_teacher_cannot_be_assigned(w, admin):
    w.rossi.active = False
    w.rossi.save()
    before = state(w)
    assert assign(admin, w).status_code == 400
    assert state(w) == before


def test_assign_without_a_timetable_is_404(w, admin):
    w.tt.delete()
    assert assign(admin, w).status_code == 404


# ---- paired lessons ------------------------------------------------------------


def pair_up(w):
    """Rossi teaches matematica to 1A and 1B together on Monday and Tuesday at 8."""
    Slot.objects.filter(teacher=w.rossi).delete()
    ClassPairing.objects.create(
        school=w.school,
        class_a=w.a,
        class_b=w.b,
        subject=w.mat,
        teacher=w.rossi,
    )
    for day in ("LUNEDI", "MARTEDI"):
        w.slot(w.a, w.rossi, w.mat, day, 8, paired=True, paired_class=w.b)
        w.slot(w.b, w.rossi, w.mat, day, 8, paired=True, paired_class=w.a)
    # the other teacher's hours were placed around them: keep 9 for both classes


def test_a_paired_teacher_is_placed_in_both_classes(w, admin):
    pair_up(w)
    r = assign(admin, w)
    assert r.status_code == 200
    rows = Slot.objects.filter(day="LUNEDI", start_hour=10)
    assert {(s.school_class_id, s.paired_class_id) for s in rows} == {
        (w.a.pk, w.b.pk),
        (w.b.pk, w.a.pk),
    }
    assert all(s.paired and s.teacher_id == w.rossi.pk for s in rows)
    text = html(r)
    assert "data-slot-id" in cell(text, "LUNEDI", 10, "1A")
    assert "data-slot-id" in cell(text, "LUNEDI", 10, "1B")


def test_a_paired_placement_bumps_once_and_counts_the_budget_once(w, admin):
    pair_up(w)
    for a in (w.asg["a_mat"], w.asg["b_mat"]):
        a.ore_totali = 3  # two placed, one left for each class
        a.save()
    assign(admin, w)
    w.tt.refresh_from_db()
    assert w.tt.version == 2
    assert not kinds(w, kind="monte_ore_exceeded")
    assert not kinds(w, kind="docente_double_booking")


def test_a_paired_placement_over_the_budget_is_reported(w, admin):
    pair_up(w)
    for a in (w.asg["a_mat"], w.asg["b_mat"]):
        a.ore_totali = 2
        a.save()
    assign(admin, w)
    assert {i["classe_id"] for i in kinds(w, kind="monte_ore_exceeded")} == {
        w.a.pk,
        w.b.pk,
    }


def test_a_paired_placement_is_refused_when_the_partner_cell_is_occupied(w, admin):
    pair_up(w)
    w.slot(w.b, w.bianchi, w.ita, "LUNEDI", 10)
    before = state(w)
    refused(w, assign(admin, w), 409, "già occupata", before)


def test_a_paired_placement_is_refused_when_the_partner_day_is_full(w, admin):
    pair_up(w)
    for hour in (9, 10, 11):
        w.slot(w.b, w.bianchi, w.ita, "LUNEDI", hour)
    before = state(w)
    # 1B is full on Monday (4 lessons), 1A is not: the whole request is refused
    r = assign(admin, w, hour=12)
    refused(w, r, 409, "tutte le ore di lezione", before)


def test_a_paired_placement_is_refused_when_the_partner_is_in_stage(w, admin):
    pair_up(w)
    CalendarEntry.objects.create(
        school=w.school, date=MON + datetime.timedelta(days=1), stage_class=w.b
    )
    before = state(w)
    refused(w, assign(admin, w, day="MARTEDI", hour=10), 409, "in stage", before)


def test_assigning_from_the_partner_side_pairs_the_same_way(w, admin):
    pair_up(w)
    assert assign(admin, w, klass=w.b).status_code == 200
    rows = Slot.objects.filter(day="LUNEDI", start_hour=10)
    assert rows.count() == 2 and all(s.paired for s in rows)


def test_an_unpaired_teacher_is_not_pulled_into_other_classes(w, admin):
    for klass in (w.a, w.b):
        Assignment.objects.create(
            school=w.school,
            teacher=w.bianchi,
            school_class=klass,
            subject=w.mat,
            ore_totali=4,
        )
    ClassPairing.objects.create(
        school=w.school, class_a=w.a, class_b=w.b, subject=w.mat, teacher=w.bianchi
    )
    assign(admin, w)  # Rossi + matematica: the pairing is Bianchi's
    assert Slot.objects.filter(day="LUNEDI", start_hour=10).count() == 1


def test_a_pairing_whose_partner_has_no_assignment_is_refused(w, admin):
    pair_up(w)
    w.asg["b_mat"].delete()
    before = state(w)
    refused(w, assign(admin, w), 409, "classe abbinata", before)


# ---- state, role and sync --------------------------------------------------


def test_assign_clears_a_libera_decision_of_the_cell(w, admin):
    from scheduling.decisions import free_key

    ConflictDecision.objects.create(
        timetable=w.tt, chiave=free_key(w.a.pk, "LUNEDI", 2), action="LIBERA"
    )
    keep = ConflictDecision.objects.create(
        timetable=w.tt, chiave=free_key(w.a.pk, "LUNEDI", 3), action="LIBERA"
    )
    assign(admin, w)
    assert list(ConflictDecision.objects.values_list("pk", flat=True)) == [keep.pk]


def test_a_stale_version_is_a_409_with_the_current_grid(w, admin):
    before = state(w)
    r = assign(admin, w, version=0)
    refused(w, r, 409, "modificato nel frattempo", before)


def test_a_failed_evaluation_rolls_the_new_lesson_back(w, admin):
    before = state(w)
    with mock.patch(
        "scheduling.moves.solve_exclusive",
        return_value=SolveResult(SolveStatus.INFEASIBLE),
    ):
        r = assign(admin, w)
    refused(w, r, 409, "Impossibile rivalutare", before)


def test_assign_refuses_an_approved_week(w, admin):
    w.tt.state = TimetableState.APPROVATO
    w.tt.save()
    before = state(w)
    refused(w, assign(admin, w), 409, "approvata", before)


def test_assign_is_forbidden_to_segreteria(w, seg):
    before = state(w)
    assert assign(seg, w).status_code == 403
    assert state(w) == before


def test_assign_needs_a_post(w, admin):
    assert admin.get(ASSIGN).status_code == 405

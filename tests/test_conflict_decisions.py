import datetime
import re
from unittest import mock

import pytest

from scheduling import tasks, views
from scheduling.board import visible_conflicts
from scheduling.decisions import free_key
from scheduling.models import (
    ConflictDecision,
    DecisionAction,
    JobStatus,
    Slot,
    SolveJob,
    TimetableState,
)
from scheduling.solver import SolveResult, SolveStatus
from school.models import ClassPairing

D = datetime.date
MON = D(2026, 10, 12)
BASE = f"/orario/{MON:%Y-%m-%d}/"
APPROVE = BASE + "conflitti/approva/"
REJECT = BASE + "conflitti/rifiuta/"
MOVE = BASE + "sposta/"
ASSIGN = BASE + "assegna/"
PAGE = "/orario/"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(views, "today", lambda: D(2026, 10, 14))


def html(response):
    return response.content.decode()


def approve(client, chiavi, version=1):
    data = {"chiavi": chiavi}
    if version is not None:
        data["version"] = version
    return client.post(APPROVE, data)


def reject(client, w, klass=None, day="LUNEDI", hour=9, version=1, **extra):
    data = {
        "classe": (klass or w.a).pk,
        "giorno": day,
        "ora_inizio": hour,
        "version": version,
    }
    data.update(extra)
    return client.post(REJECT, {k: v for k, v in data.items() if v is not None})


def state(w):
    w.tt.refresh_from_db()
    return (
        w.tt.version,
        w.tt.quality_score,
        w.tt.quality_level,
        w.tt.soft_conflicts,
        w.tt.conflicts,
        sorted(Slot.objects.values_list("pk", "school_class_id", "day", "start_hour")),
        sorted(ConflictDecision.objects.values_list("chiave", "action")),
    )


def keys(w):
    w.tt.refresh_from_db()
    return [c["chiave"] for c in w.tt.conflicts]


def with_double_booking(w, admin):
    """Rossi is in 1B at 10 and gets placed in 1A at 10: a teacher conflict."""
    w.slot(w.b, w.rossi, w.mat, "LUNEDI", 10)
    w.tt.refresh_from_db()
    r = admin.post(
        ASSIGN,
        {
            "classe": w.a.pk,
            "giorno": "LUNEDI",
            "ora_inizio": 10,
            "docente": w.rossi.pk,
            "materia": w.mat.pk,
            "version": w.tt.version,
        },
    )
    assert r.status_code == 200
    w.tt.refresh_from_db()
    return next(c for c in w.tt.conflicts if c["kind"] == "docente_double_booking")


def listed(text):
    return re.findall(r"<li>([^<]*?)(?: <form|</li>)", text)


DAY = {"LUNEDI": "Lunedì", "MARTEDI": "Martedì"}


def marked(text, day, hour, klass="1A"):
    """Whether the cell of class `klass` carries the conflict highlight."""
    section = text.split(f'aria-label="{DAY[day]}"')[1]
    row = re.search(rf'<th scope="row">{klass}</th>(.*?)</tr>', section, re.S).group(1)
    m = re.search(
        rf'<td class="[^"]*" (data-conflict="true" )?data-day="{day}" '
        rf'data-hour="{hour}"',
        row,
    )
    assert m, f"no cell {day} {hour}"
    return bool(m.group(1))


# ---- approve ------------------------------------------------------------------


def test_approve_stores_one_decision_and_hides_the_conflict(w, admin):
    conflict = with_double_booking(w, admin)
    n = len(w.tt.conflicts)
    version = w.tt.version
    r = approve(admin, [conflict["chiave"]], version)
    assert r.status_code == 200
    decision = ConflictDecision.objects.get()
    assert (decision.chiave, decision.action) == (conflict["chiave"], "APPROVATO")
    text = html(r)
    assert conflict["description"] not in text
    assert f"Conflitti ({n - 1})" in text
    assert not marked(text, "LUNEDI", 10)  # cell highlight gone, in both classes
    assert not marked(text, "LUNEDI", 10, "1B")


def test_approve_reports_only_and_leaves_score_and_stored_conflicts(w, admin):
    conflict = with_double_booking(w, admin)
    before = state(w)
    approve(admin, [conflict["chiave"]], before[0])
    after = state(w)
    assert after[1:5] == before[1:5]  # score, level, soft count, stored conflicts
    assert after[0] == before[0] + 1
    assert after[5] == before[5]


def test_approve_bumps_the_version_once_for_several_keys(w, admin):
    with_double_booking(w, admin)
    chiavi = keys(w)
    assert len(chiavi) > 1
    version = w.tt.version
    approve(admin, chiavi, version)
    w.tt.refresh_from_db()
    assert w.tt.version == version + 1
    assert ConflictDecision.objects.count() == len(chiavi)
    assert not visible_conflicts(w.tt)


def test_approve_an_empty_list_changes_nothing(w, admin):
    with_double_booking(w, admin)
    before = state(w)
    r = admin.post(APPROVE, {"version": before[0]})
    assert r.status_code == 200
    assert state(w) == before
    assert approve(admin, [], before[0]).status_code == 200
    assert state(w) == before


def test_approve_an_unknown_key_stores_nothing(w, admin):
    with_double_booking(w, admin)
    before = state(w)
    r = approve(admin, ["docente_double_booking|1|2|LUNEDI|9", ""], before[0])
    assert r.status_code == 200
    assert state(w) == before


def test_an_unknown_key_does_not_hide_a_later_conflict(w, admin):
    ghosts = [
        f"docente_double_booking|{klass.pk}|{w.rossi.pk}|LUNEDI|2"
        for klass in (w.a, w.b)
    ]
    approve(admin, ghosts, 1)  # nothing is reported yet: nothing is stored
    assert not ConflictDecision.objects.exists()
    conflict = with_double_booking(w, admin)
    assert conflict["chiave"] in ghosts
    assert [c["chiave"] for c in visible_conflicts(w.tt)].count(conflict["chiave"]) == 1


def test_a_duplicate_key_is_stored_once(w, admin):
    conflict = with_double_booking(w, admin)
    key = conflict["chiave"]
    approve(admin, [key, key], w.tt.version)
    assert ConflictDecision.objects.filter(chiave=key).count() == 1
    w.tt.refresh_from_db()
    version = w.tt.version
    r = approve(admin, [key], version)  # again, in a later request
    assert r.status_code == 200
    assert ConflictDecision.objects.filter(chiave=key).count() == 1
    w.tt.refresh_from_db()
    assert w.tt.version == version  # nothing new: nothing written


def test_approve_does_not_turn_a_libera_hour_into_an_approval(w, admin):
    key = free_key(w.a.pk, "LUNEDI", 2)
    ConflictDecision.objects.create(timetable=w.tt, chiave=key, action="LIBERA")
    w.tt.conflicts = [{"kind": "classe_unfilled", "chiave": key, "description": "x"}]
    w.tt.save()
    approve(admin, [key], 1)
    assert ConflictDecision.objects.get().action == "LIBERA"


def test_approved_conflicts_stay_hidden_in_the_grid_poll(w, admin):
    conflict = with_double_booking(w, admin)
    approve(admin, [conflict["chiave"]], w.tt.version)
    w.tt.refresh_from_db()
    text = html(admin.get(f"{BASE}griglia/"))
    assert conflict["description"] not in text
    page = html(admin.get(f"{PAGE}?settimana={MON:%Y-%m-%d}"))
    assert conflict["description"] not in page


def test_the_conflict_list_offers_approve_to_admin_only(w, admin, seg):
    with_double_booking(w, admin)
    url = f"{PAGE}?settimana={MON:%Y-%m-%d}"
    assert "Approva" in html(admin.get(url))
    assert "Approva" not in html(seg.get(url))


# ---- the key decides what stays hidden --------------------------------------


def move(client, w, slot, hour):
    w.tt.refresh_from_db()
    return client.post(
        MOVE,
        {
            "slot_id": slot.pk,
            "giorno": "LUNEDI",
            "ora_inizio": hour,
            "version": w.tt.version,
        },
    )


def test_a_conflict_that_changes_key_after_a_move_is_reported_again(w, admin):
    conflict = with_double_booking(w, admin)  # Rossi in 1A and 1B at 10
    approve(admin, [conflict["chiave"]], w.tt.version)
    mine = Slot.objects.get(school_class=w.a, day="LUNEDI", start_hour=10)
    theirs = Slot.objects.get(school_class=w.b, day="LUNEDI", start_hour=10)
    assert move(admin, w, mine, 11).status_code == 200  # the clash is gone
    assert conflict["chiave"] not in keys(w)
    r = move(admin, w, theirs, 11)  # ...and is back, one hour later
    w.tt.refresh_from_db()
    again = [
        c
        for c in w.tt.conflicts
        if c["kind"] == "docente_double_booking" and c["docente_id"] == w.rossi.pk
    ]
    assert [c["ore_slot"] for c in again] == [3]
    assert again[0]["chiave"] != conflict["chiave"]
    text = html(r)
    assert again[0]["description"] in text
    assert marked(text, "LUNEDI", 11)


def test_the_same_conflict_coming_back_with_its_key_stays_hidden(w, admin):
    conflict = with_double_booking(w, admin)
    approve(admin, [conflict["chiave"]], w.tt.version)
    mine = Slot.objects.get(school_class=w.a, day="LUNEDI", start_hour=10)
    move(admin, w, mine, 11)
    r = move(admin, w, mine, 10)  # the identical clash again: same key
    assert conflict["chiave"] in keys(w)
    assert conflict["description"] not in html(r)
    assert not marked(html(r), "LUNEDI", 10)


def test_a_conflict_with_an_unchanged_key_stays_hidden_after_a_move(w, admin):
    # Move 1A's Monday 9 lesson to 11: holes at slots 1 and 2 are reported.
    slot = w.slots["LUNEDI", "a_ita"]
    r = admin.post(
        MOVE,
        {"slot_id": slot.pk, "giorno": "LUNEDI", "ora_inizio": 11, "version": 1},
    )
    assert r.status_code == 200
    w.tt.refresh_from_db()
    holes = [
        c["chiave"]
        for c in w.tt.conflicts
        if c["kind"] == "classe_unfilled"
        and c["classe_id"] == w.a.pk
        and c["giorno"] == "LUNEDI"
    ]
    assert len(holes) == 2
    approve(admin, holes, 2)
    # An unrelated move on Tuesday leaves those two keys unchanged.
    other = w.slots["MARTEDI", "a_ita"]
    r = admin.post(
        MOVE,
        {"slot_id": other.pk, "giorno": "MARTEDI", "ora_inizio": 11, "version": 3},
    )
    assert r.status_code == 200
    assert set(holes) <= set(keys(w))  # still computed...
    text = html(r)
    assert not marked(text, "LUNEDI", 9) and not marked(text, "LUNEDI", 10)
    assert marked(text, "MARTEDI", 9)  # ...while the new Tuesday holes show


# ---- reject --------------------------------------------------------------------


def test_reject_removes_the_lesson_and_leaves_the_hour_free(w, admin):
    slot = w.slots["LUNEDI", "a_ita"]
    r = reject(admin, w, hour=9)
    assert r.status_code == 200
    assert not Slot.objects.filter(pk=slot.pk).exists()
    assert not Slot.objects.filter(
        school_class=w.a, day="LUNEDI", start_hour=9
    ).exists()
    text = html(r)
    assert f'data-slot-id="{slot.pk}"' not in text
    assert 'class="tt-free" data-day="LUNEDI" data-hour="9"' in text


def test_reject_records_a_libera_decision_and_bumps_once(w, admin):
    reject(admin, w, hour=9)
    w.tt.refresh_from_db()
    assert w.tt.version == 2
    decision = ConflictDecision.objects.get()
    assert decision.action == DecisionAction.LIBERA
    assert decision.chiave == free_key(w.a.pk, "LUNEDI", 1)


def test_the_hour_left_free_is_not_reported_as_a_hole(w, admin):
    r = reject(admin, w, hour=9)
    w.tt.refresh_from_db()
    # the evaluation does see the hole...
    assert free_key(w.a.pk, "LUNEDI", 1) in keys(w)
    # ...but the decision keeps it out of the list and the highlight
    text = html(r)
    assert not marked(text, "LUNEDI", 9)
    assert all(
        "Ora non coperta" not in t or "alle 09:00" not in t for t in listed(text)
    )
    assert free_key(w.a.pk, "LUNEDI", 1) not in [
        c["chiave"] for c in visible_conflicts(w.tt)
    ]


def test_other_holes_of_the_class_stay_reported(w, admin):
    r = reject(admin, w, hour=9)
    text = html(r)
    # 1A's Monday now has one lesson in a 4-hour day: 10, 11 and 12 are holes too
    # only if they are reported by the solver; the rejected cell must be the only
    # one hidden.
    w.tt.refresh_from_db()
    visible = visible_conflicts(w.tt)
    hidden = [c for c in w.tt.conflicts if c not in visible]
    assert [c["chiave"] for c in hidden] == [free_key(w.a.pk, "LUNEDI", 1)]
    assert f"Conflitti ({len(visible)})" in text


def test_rejecting_an_empty_cell_is_refused_with_a_message(w, admin):
    before = state(w)
    r = reject(admin, w, hour=12)
    assert r.status_code == 409
    assert "già libera" in html(r)
    assert 'id="timetable-body"' in html(r)
    assert state(w) == before


def test_rejecting_a_stage_cell_is_refused(w, admin):
    from school.models import CalendarEntry

    CalendarEntry.objects.create(
        school=w.school, date=MON + datetime.timedelta(days=1), stage_class=w.a
    )
    before = state(w)
    assert reject(admin, w, day="MARTEDI", hour=12).status_code == 409
    assert state(w) == before


def test_rejecting_a_paired_lesson_removes_it_from_both_classes(w, admin):
    ClassPairing.objects.create(
        school=w.school, class_a=w.a, class_b=w.b, subject=w.mat, teacher=w.rossi
    )
    Slot.objects.filter(teacher=w.rossi).delete()
    w.slot(w.a, w.rossi, w.mat, "LUNEDI", 8, paired=True, paired_class=w.b)
    w.slot(w.b, w.rossi, w.mat, "LUNEDI", 8, paired=True, paired_class=w.a)
    r = reject(admin, w, hour=8)
    assert r.status_code == 200
    assert not Slot.objects.filter(teacher=w.rossi, day="LUNEDI").exists()
    w.tt.refresh_from_db()
    assert w.tt.version == 2
    assert sorted(ConflictDecision.objects.values_list("chiave", "action")) == sorted(
        [
            (free_key(w.a.pk, "LUNEDI", 0), "LIBERA"),
            (free_key(w.b.pk, "LUNEDI", 0), "LIBERA"),
        ]
    )


def test_rejecting_from_the_partner_class_side_does_the_same(w, admin):
    ClassPairing.objects.create(
        school=w.school, class_a=w.a, class_b=w.b, subject=w.mat, teacher=w.rossi
    )
    Slot.objects.filter(teacher=w.rossi).delete()
    w.slot(w.a, w.rossi, w.mat, "LUNEDI", 8, paired=True, paired_class=w.b)
    w.slot(w.b, w.rossi, w.mat, "LUNEDI", 8, paired=True, paired_class=w.a)
    reject(admin, w, klass=w.b, hour=8)
    assert not Slot.objects.filter(teacher=w.rossi, day="LUNEDI").exists()


def test_rejecting_a_conflicting_lesson_clears_its_conflict(w, admin):
    conflict = with_double_booking(w, admin)
    r = reject(admin, w, hour=10, version=w.tt.version)
    assert r.status_code == 200
    assert conflict["chiave"] not in keys(w)
    assert conflict["description"] not in html(r)


def test_the_last_lesson_of_the_week_can_be_rejected(w, admin):
    Slot.objects.exclude(pk=w.slots["LUNEDI", "a_ita"].pk).delete()
    r = reject(admin, w, hour=9)
    assert r.status_code == 200
    assert not Slot.objects.exists()
    w.tt.refresh_from_db()
    assert w.tt.version == 2


def test_rejecting_twice_keeps_one_decision(w, admin):
    reject(admin, w, hour=9)
    w.slot(w.a, w.bianchi, w.ita, "LUNEDI", 9)  # a lesson appears again
    w.tt.refresh_from_db()
    reject(admin, w, hour=9, version=w.tt.version)
    assert ConflictDecision.objects.count() == 1


def test_a_lesson_marked_in_conflict_offers_reject_to_admin_only(w, admin, seg):
    with_double_booking(w, admin)
    url = f"{PAGE}?settimana={MON:%Y-%m-%d}"
    assert "Rifiuta" in html(admin.get(url))
    assert "Rifiuta" not in html(seg.get(url))


REJECT_BAD = {
    "hour_7": dict(hour=7),
    "hour_14": dict(hour=14),
    "hour_text": dict(hour="x"),
    "hour_missing": dict(hour=None),
    "unknown_day": dict(day="SABATO"),
    "unknown_class": dict(classe="99999"),
    "text_class": dict(classe="abc"),
    "missing_version": dict(version=None),
}


@pytest.mark.parametrize("name", list(REJECT_BAD))
def test_reject_bad_input_is_a_400_and_changes_nothing(w, admin, name):
    before = state(w)
    r = reject(admin, w, **REJECT_BAD[name])
    assert r.status_code == 400
    assert state(w) == before


def test_approve_bad_version_is_a_400(w, admin):
    with_double_booking(w, admin)
    before = state(w)
    assert approve(admin, keys(w), None).status_code == 400
    assert approve(admin, keys(w), "x").status_code == 400
    assert state(w) == before


def test_a_failed_evaluation_rolls_a_rejection_back(w, admin):
    before = state(w)
    with mock.patch(
        "scheduling.moves.solve_exclusive",
        return_value=SolveResult(SolveStatus.INFEASIBLE),
    ):
        r = reject(admin, w, hour=9)
    assert r.status_code == 409
    assert state(w) == before


# ---- state, role, sync (approve and reject) ------------------------------------


@pytest.fixture(params=["approve", "reject"])
def call(request, w):
    def run(client, version=1):
        if request.param == "approve":
            return approve(client, ["x"], version)
        return reject(client, w, hour=9, version=version)

    return run


def test_a_stale_version_is_a_409_with_the_current_grid(w, admin, call):
    before = state(w)
    r = call(admin, 0)
    assert r.status_code == 409
    assert "modificato nel frattempo" in html(r)
    assert 'id="timetable-body"' in html(r)
    assert state(w) == before


def test_an_approved_week_is_refused(w, admin, call):
    w.tt.state = TimetableState.APPROVATO
    w.tt.save()
    before = state(w)
    r = call(admin)
    assert r.status_code == 409
    assert "approvata" in html(r)
    assert state(w) == before


def test_segreteria_is_forbidden(w, seg, call):
    before = state(w)
    assert call(seg).status_code == 403
    assert state(w) == before


def test_anonymous_is_sent_to_login(w, call):
    from django.test import Client

    assert call(Client()).status_code in (302, 403)
    assert not ConflictDecision.objects.exists()


def test_a_week_without_a_timetable_is_404(w, admin, call):
    w.tt.delete()
    assert call(admin).status_code == 404


def test_get_is_not_allowed(w, admin):
    assert admin.get(APPROVE).status_code == 405
    assert admin.get(REJECT).status_code == 405


# ---- regeneration --------------------------------------------------------------


def test_regenerating_deletes_the_decisions_and_reports_the_conflict_again(w, admin):
    key = "docente_double_booking|1|2|LUNEDI|2"
    item = {
        "kind": "docente_double_booking",
        "chiave": key,
        "description": "Il docente è in due classi",
        "classe_id": w.a.pk,
        "docente_id": w.rossi.pk,
        "giorno": "LUNEDI",
        "ore_slot": 2,
    }
    w.tt.conflicts = [item]
    w.tt.save()
    approve(admin, [key], 1)
    assert ConflictDecision.objects.count() == 1
    assert visible_conflicts(w.tt) == []
    result = SolveResult(SolveStatus.OPTIMAL, conflict_items=[dict(item)])
    job = SolveJob.objects.create(school=w.school, week_start=MON)
    with mock.patch.object(tasks, "solve", return_value=result):
        tasks.run_job(job.pk)
    job.refresh_from_db()
    assert job.status == JobStatus.DONE
    assert not ConflictDecision.objects.exists()
    w.tt.refresh_from_db()
    assert [c["chiave"] for c in visible_conflicts(w.tt)] == [key]
    assert item["description"] in html(admin.get(f"{BASE}griglia/"))


def test_decisions_of_another_week_are_untouched_by_regeneration(w, admin):
    from scheduling.models import Timetable

    other = Timetable.objects.create(
        school=w.school, week_start=MON + datetime.timedelta(days=7)
    )
    ConflictDecision.objects.create(timetable=other, chiave="k")
    job = SolveJob.objects.create(school=w.school, week_start=MON)
    with mock.patch.object(
        tasks, "solve", return_value=SolveResult(SolveStatus.OPTIMAL)
    ):
        tasks.run_job(job.pk)
    assert ConflictDecision.objects.filter(timetable=other).count() == 1


# ---- the model ---------------------------------------------------------------


def test_a_decision_is_unique_per_timetable_and_key(w):
    from django.db import IntegrityError, transaction

    ConflictDecision.objects.create(timetable=w.tt, chiave="k")
    with pytest.raises(IntegrityError), transaction.atomic():
        ConflictDecision.objects.create(timetable=w.tt, chiave="k", action="LIBERA")


def test_the_action_is_limited_to_the_spec_values(w):
    from django.db import IntegrityError, transaction

    assert ConflictDecision.objects.create(timetable=w.tt, chiave="a").action == (
        "APPROVATO"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        ConflictDecision.objects.create(timetable=w.tt, chiave="b", action="APPROVE")


def test_a_key_can_be_300_characters(w):
    ConflictDecision.objects.create(timetable=w.tt, chiave="k" * 300)

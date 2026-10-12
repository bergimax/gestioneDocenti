"""Weight proposals (#35): evidence, bounds, apply, reset, history, access."""

import datetime
from unittest import mock

import pytest
from django.test import Client

from accounts.models import User
from scheduling import alternatives, proposals, tasks
from scheduling.models import AlternativePick, SoftWeightAdjustment, SolveJob
from scheduling.proposals import (
    ADJUSTABLE_KINDS,
    apply_proposals,
    compute_proposals,
    proposed_weight,
    reset_weights,
)
from scheduling.solver import (
    CLASSE_LATE_START,
    CLASSE_UNFILLED,
    CONTRACTOR_GAP,
    DEFAULT_SOFT_WEIGHTS,
    MAX_SOFT_WEIGHT,
    ORE_TARGET_DEVIATION,
    TEORIA_CONSECUTIVE,
)
from school.models import School

D = datetime.date
S1, S2, S3, S4 = (
    CLASSE_LATE_START,
    TEORIA_CONSECUTIVE,
    ORE_TARGET_DEVIATION,
    CONTRACTOR_GAP,
)


@pytest.fixture
def school(db):
    return School.objects.create(
        name="S",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )


@pytest.fixture
def admin(db):
    c = Client()
    c.user = User.objects.create_user("a@example.com", "pw-long-enough", "ADMIN")
    c.force_login(c.user)
    return c


@pytest.fixture
def seg(db):
    c = Client()
    c.force_login(
        User.objects.create_user("s@example.com", "pw-long-enough", "SEGRETERIA")
    )
    return c


_week = [0]


def pick(school, chosen, *rejected):
    """A stored pick; each argument is a {kind: count} violation dict."""
    _week[0] += 1
    job = SolveJob.objects.create(
        school=school, week_start=D(2026, 10, 12) + datetime.timedelta(weeks=_week[0])
    )
    return AlternativePick.objects.create(
        job=job,
        school=school,
        week_start=job.week_start,
        chosen={"label": "A", "violations_by_kind": chosen},
        rejected=[{"label": "B", "violations_by_kind": r} for r in rejected],
    )


def by_kind(school):
    return {p.kind: p for p in compute_proposals(school)}


def test_no_picks_no_proposals(school):
    assert compute_proposals(school) == []


def test_a_single_supporting_pick_is_not_enough(school):
    pick(school, {S1: 0}, {S1: 3})
    assert compute_proposals(school) == []


def test_two_supporting_picks_propose_one_kind(school):
    pick(school, {S1: 0}, {S1: 3})
    pick(school, {S1: 1, S2: 5}, {S1: 2, S2: 5}, {S1: 4})
    found = compute_proposals(school)
    assert [p.kind for p in found] == [S1]
    assert found[0].old_weight == 500
    assert found[0].new_weight == 600  # +10% per pick, two picks = +20%
    assert len(found[0].supporting) == 2
    assert found[0].contradicting == []


def test_equal_support_and_contradiction_proposes_nothing(school):
    for _ in range(2):
        pick(school, {S1: 0}, {S1: 3})
        pick(school, {S1: 3}, {S1: 0})
    assert compute_proposals(school) == []


def test_missing_kind_counts_as_zero_violations(school):
    pick(school, {}, {S2: 1})
    pick(school, {}, {S2: 2})
    assert [p.kind for p in compute_proposals(school)] == [S2]


def test_other_kinds_are_ignored(school):
    pick(school, {CLASSE_UNFILLED: 0}, {CLASSE_UNFILLED: 3})
    pick(school, {CLASSE_UNFILLED: 0}, {CLASSE_UNFILLED: 3})
    assert compute_proposals(school) == []


def test_step_is_capped_at_fifty_percent():
    assert proposed_weight(500, 5) == 750
    assert proposed_weight(500, 50) == 750
    assert proposed_weight(50, 2) == 60


def test_step_is_at_least_one_and_never_above_the_ceiling():
    assert proposed_weight(3, 2) == 4
    assert proposed_weight(900, 3) == MAX_SOFT_WEIGHT
    for active in range(1, MAX_SOFT_WEIGHT):
        for n in (2, 5, 20):
            new = proposed_weight(active, n)
            assert isinstance(new, int) and active < new <= MAX_SOFT_WEIGHT


def test_a_kind_at_the_ceiling_has_no_proposal(school):
    SoftWeightAdjustment.objects.create(
        school=school, kind=S1, old_weight=500, new_weight=MAX_SOFT_WEIGHT
    )
    for _ in range(3):
        pick(school, {S1: 0}, {S1: 3})
    assert compute_proposals(school) == []


def test_a_proposal_above_800_is_clamped(school):
    SoftWeightAdjustment.objects.create(
        school=school, kind=S4, old_weight=750, new_weight=900
    )
    for _ in range(3):
        pick(school, {S4: 0}, {S4: 3})
    assert by_kind(school)[S4].new_weight == MAX_SOFT_WEIGHT


def test_only_adjustable_kinds_exist():
    assert set(ADJUSTABLE_KINDS) == set(DEFAULT_SOFT_WEIGHTS)
    with pytest.raises(ValueError):
        proposals.check_kinds(["classe_unfilled"])


def test_apply_writes_one_row_with_evidence(school, admin):
    pick(school, {S1: 0}, {S1: 3})
    pick(school, {S1: 0}, {S1: 3})
    rows = apply_proposals(school, admin.user)
    assert len(rows) == 1
    row = rows[0]
    assert (row.kind, row.old_weight, row.new_weight) == (S1, 500, 600)
    assert row.user == admin.user
    assert row.evidence["supporting"] == 2
    assert proposals.active_weights(school)[S1] == 600
    assert proposals.active_weights(school)[S2] == DEFAULT_SOFT_WEIGHTS[S2]


def test_applied_picks_are_not_counted_twice(school, admin):
    pick(school, {S1: 0}, {S1: 3})
    pick(school, {S1: 0}, {S1: 3})
    apply_proposals(school, admin.user)
    assert compute_proposals(school) == []
    assert apply_proposals(school, admin.user) == []  # the second admin
    assert SoftWeightAdjustment.objects.count() == 1


def test_a_pick_after_the_adjustment_counts_again(school, admin):
    pick(school, {S1: 0}, {S1: 3})
    pick(school, {S1: 0}, {S1: 3})
    apply_proposals(school, admin.user)
    pick(school, {S1: 0}, {S1: 3})
    assert compute_proposals(school) == []  # one is not enough
    pick(school, {S1: 0}, {S1: 3})
    assert by_kind(school)[S1].old_weight == 600


def test_apply_some_applies_only_those(school, admin):
    for _ in range(2):
        pick(school, {S1: 0, S2: 0}, {S1: 3, S2: 3})
    rows = apply_proposals(school, admin.user, [S2])
    assert [r.kind for r in rows] == [S2]
    assert [p.kind for p in compute_proposals(school)] == [S1]


def test_reset_appends_the_default_and_forgets_older_picks(school, admin):
    for _ in range(2):
        pick(school, {S1: 0}, {S1: 3})
    apply_proposals(school, admin.user)
    rows, skipped = reset_weights(school, admin.user, [S1])
    assert (rows[0].old_weight, rows[0].new_weight) == (600, 500)
    assert skipped == []
    assert proposals.active_weights(school)[S1] == 500
    assert SoftWeightAdjustment.objects.count() == 2
    assert compute_proposals(school) == []


def test_reset_of_a_default_kind_writes_nothing(school, admin):
    rows, skipped = reset_weights(school, admin.user, [S1, S2])
    assert rows == [] and skipped == [S1, S2]
    assert SoftWeightAdjustment.objects.count() == 0


def test_rows_cannot_be_updated_or_deleted(school):
    row = SoftWeightAdjustment.objects.create(
        school=school, kind=S1, old_weight=500, new_weight=600
    )
    row.new_weight = 1
    with pytest.raises(NotImplementedError):
        row.save()
    with pytest.raises(NotImplementedError):
        row.delete()
    with pytest.raises(NotImplementedError):
        SoftWeightAdjustment.objects.update(new_weight=1)
    with pytest.raises(NotImplementedError):
        SoftWeightAdjustment.objects.all().delete()


def test_active_soft_weights_reads_the_adjustments(school):
    assert alternatives.active_soft_weights(school) == DEFAULT_SOFT_WEIGHTS
    SoftWeightAdjustment.objects.create(
        school=school, kind=S3, old_weight=75, new_weight=90
    )
    SoftWeightAdjustment.objects.create(
        school=school, kind=S3, old_weight=90, new_weight=99
    )
    assert alternatives.active_soft_weights(school)[S3] == 99


def test_generation_uses_the_active_weights(school, admin):
    job = SolveJob.objects.create(school=school, week_start=D(2026, 10, 12))
    seen = []

    def spy(ctx, pinned=None, weights=None):
        seen.append(weights)
        raise RuntimeError("stop")

    with (
        mock.patch.object(tasks, "build_week_context"),
        mock.patch.object(tasks.dataclasses, "replace"),
        mock.patch.object(tasks, "solve_exclusive", spy),
    ):
        tasks.run_job(job.pk)
        assert seen[-1] == DEFAULT_SOFT_WEIGHTS
        for _ in range(2):
            pick(school, {S1: 0}, {S1: 3})
        apply_proposals(school, admin.user)
        job2 = SolveJob.objects.create(school=school, week_start=D(2026, 10, 19))
        tasks.run_job(job2.pk)
    assert seen[-1][S1] == 600


# ---- screen -----------------------------------------------------------------

URL = "/pesi/"


def test_empty_screen_shows_defaults(admin, school):
    body = admin.get(URL).content.decode()
    assert "Nessuna proposta" in body
    for kind, value in DEFAULT_SOFT_WEIGHTS.items():
        assert proposals.KIND_NAMES[kind] in body
        assert f"<td>{value}</td>" in body


def test_screen_lists_proposal_and_history(admin, school):
    for _ in range(2):
        pick(school, {S1: 0}, {S1: 3})
    body = admin.get(URL).content.decode()
    assert "<td>600</td>" in body and "Applica tutte" in body
    admin.post(URL + "applica/", {"all": "1"})
    body = admin.get(URL).content.decode()
    assert "Nessuna proposta" in body
    assert "a@example.com" in body and "Storico" in body
    assert "Nessuna modifica ai pesi" not in body


def test_apply_ignores_posted_values(admin, school):
    for _ in range(2):
        pick(school, {S1: 0}, {S1: 3})
    admin.post(URL + "applica/", {"kind": S1, "new_weight": "999", S1: "999"})
    assert SoftWeightAdjustment.objects.get().new_weight == 600


def test_apply_rejects_fixed_kinds(admin, school):
    r = admin.post(URL + "applica/", {"kind": "classe_unfilled"})
    assert r.status_code == 400
    r = admin.post(URL + "ripristina/", {"kind": "RELAX_WEIGHT"})
    assert r.status_code == 400
    assert SoftWeightAdjustment.objects.count() == 0


def test_second_apply_reports_already_applied(admin, school):
    for _ in range(2):
        pick(school, {S1: 0}, {S1: 3})
    admin.post(URL + "applica/", {"kind": S1})
    r = admin.post(URL + "applica/", {"kind": S1}, follow=True)
    assert "già applicata" in r.content.decode()
    assert SoftWeightAdjustment.objects.count() == 1


def test_reset_screen_flow(admin, school):
    r = admin.post(URL + "ripristina/", {"kind": S1}, follow=True)
    assert "Già al valore predefinito" in r.content.decode()
    SoftWeightAdjustment.objects.create(
        school=school, kind=S1, old_weight=500, new_weight=600
    )
    admin.post(URL + "ripristina/", {"all": "1"})
    assert proposals.active_weights(school)[S1] == 500
    assert SoftWeightAdjustment.objects.count() == 2


def test_access_rules(seg, school):
    anon = Client()
    for method in ("get", "post"):
        for path in (URL, URL + "applica/", URL + "ripristina/"):
            assert getattr(seg, method)(path).status_code == 403
            r = getattr(anon, method)(path)
            assert r.status_code == 302 and "/login" in r["Location"]
    assert "/pesi/" not in seg.get("/").content.decode()


def test_admin_has_a_menu_entry(admin, school):
    assert 'href="/pesi/"' in admin.get("/").content.decode()

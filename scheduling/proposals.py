"""Weight proposals from pairwise picks (architecture 8, specs 11).

A proposal only ever raises the weight of an adjustable kind; nothing changes
until an ADMIN applies it. Every apply or reset appends a `SoftWeightAdjustment`
row and the newest row of a kind is its active weight, else the default.
Proposals are always computed from the stored picks, never from posted values.
"""

import math
from dataclasses import dataclass, field

from django.db import transaction

from school.models import School

from . import audit
from .models import AlternativePick, SoftWeightAdjustment
from .solver import (
    CLASSE_LATE_START,
    CONTRACTOR_GAP,
    DEFAULT_SOFT_WEIGHTS,
    MAX_SOFT_WEIGHT,
    ORE_TARGET_DEVIATION,
    SINGLE_CLASSE_DAY,
    TEORIA_CONSECUTIVE,
)

ADJUSTABLE_KINDS = (
    CLASSE_LATE_START,
    TEORIA_CONSECUTIVE,
    ORE_TARGET_DEVIATION,
    CONTRACTOR_GAP,
    SINGLE_CLASSE_DAY,
)
MIN_EVIDENCE = 2
STEP_PER_PICK = 0.10
MAX_STEP = 0.50

KIND_NAMES = {
    CLASSE_LATE_START: "Inizio non alle 8 (S1)",
    TEORIA_CONSECUTIVE: "Teoria consecutiva (S2)",
    ORE_TARGET_DEVIATION: "Scarto dalle ore target (S3)",
    CONTRACTOR_GAP: "Ora buca del contrattista (S4)",
    SINGLE_CLASSE_DAY: "Giornata con una sola classe (S5)",
}


@dataclass
class Proposal:
    kind: str
    old_weight: int
    new_weight: int
    supporting: list = field(default_factory=list)  # week_start dates
    contradicting: list = field(default_factory=list)
    supporting_ids: list = field(default_factory=list)
    contradicting_ids: list = field(default_factory=list)

    @property
    def name(self):
        return KIND_NAMES[self.kind]

    def evidence(self):
        return {
            "supporting": len(self.supporting),
            "contradicting": len(self.contradicting),
            "supporting_picks": self.supporting_ids,
            "contradicting_picks": self.contradicting_ids,
            "weeks": sorted({d.isoformat() for d in self.supporting}),
        }


def _newest(school, kind):
    return SoftWeightAdjustment.objects.filter(school=school, kind=kind).first()


def active_weights(school):
    """Defaults overlaid with the newest adjustment of each kind."""
    weights = dict(DEFAULT_SOFT_WEIGHTS)
    seen = set()
    for row in SoftWeightAdjustment.objects.filter(school=school):  # newest first
        if row.kind in weights and row.kind not in seen:
            weights[row.kind] = row.new_weight
            seen.add(row.kind)
    return weights


def proposed_weight(active, supporting):
    """+10% per supporting pick, at most +50%, at least +1, at most the cap."""
    step = math.floor(active * STEP_PER_PICK * supporting + 0.5)
    step = min(step, math.floor(active * MAX_STEP))
    return min(active + max(1, step), MAX_SOFT_WEIGHT)


def _count(violations, kind):
    return (violations or {}).get(kind, 0)


def _judge(pick, kind):
    """+1 when the pick supports a rise of `kind`, -1 when it contradicts."""
    chosen = _count(pick.chosen.get("violations_by_kind"), kind)
    rejected = [_count(r.get("violations_by_kind"), kind) for r in pick.rejected]
    if not rejected:
        return 0
    if chosen < max(rejected):
        return 1
    if chosen > max(rejected):
        return -1
    return 0


def compute_proposals(school):
    """The proposals of the school, in the order of `ADJUSTABLE_KINDS`."""
    active = active_weights(school)
    picks = list(AlternativePick.objects.filter(school=school).order_by("created_at"))
    proposals = []
    for kind in ADJUSTABLE_KINDS:
        if active[kind] >= MAX_SOFT_WEIGHT:
            continue
        newest = _newest(school, kind)
        proposal = Proposal(kind, active[kind], active[kind])
        for pick in picks:
            if newest is not None and pick.created_at <= newest.created_at:
                continue
            verdict = _judge(pick, kind)
            if verdict > 0:
                proposal.supporting.append(pick.week_start)
                proposal.supporting_ids.append(pick.pk)
            elif verdict < 0:
                proposal.contradicting.append(pick.week_start)
                proposal.contradicting_ids.append(pick.pk)
        n = len(proposal.supporting)
        if n < MIN_EVIDENCE or n <= len(proposal.contradicting):
            continue
        proposal.new_weight = proposed_weight(active[kind], n)
        if proposal.new_weight > proposal.old_weight:
            proposals.append(proposal)
    return proposals


def check_kinds(kinds):
    """Raise ValueError when a requested kind is not adjustable."""
    bad = [k for k in kinds if k not in ADJUSTABLE_KINDS]
    if bad:
        raise ValueError(f"kind not adjustable: {', '.join(bad)}")


def _lock(school):
    School.objects.select_for_update().get(pk=school.pk)


def _audit(action, user, rows):
    audit.record_audit(
        action,
        user,
        None,
        {"kinds": [r.kind for r in rows], "adjustments": [r.pk for r in rows]},
    )


def apply_proposals(school, user, kinds=None):
    """Apply the current proposals of `kinds` (all when None); return the rows.

    Recomputed on the server inside a transaction that locks the school, so a
    second concurrent apply of the same proposal finds nothing left to apply.
    """
    if kinds is not None:
        check_kinds(kinds)
    with transaction.atomic():
        _lock(school)
        rows = []
        for p in compute_proposals(school):
            if kinds is not None and p.kind not in kinds:
                continue
            rows.append(
                SoftWeightAdjustment.objects.create(
                    school=school,
                    kind=p.kind,
                    old_weight=p.old_weight,
                    new_weight=p.new_weight,
                    evidence=p.evidence(),
                    user=user,
                )
            )
        if rows:
            _audit(audit.SOFT_WEIGHTS_APPLIED, user, rows)
        return rows


def reset_weights(school, user, kinds=None):
    """Append rows restoring the default of `kinds` (all when None).

    Kinds already at their default write nothing; returns (rows, skipped kinds).
    """
    if kinds is not None:
        check_kinds(kinds)
    with transaction.atomic():
        _lock(school)
        active = active_weights(school)
        rows, skipped = [], []
        for kind in ADJUSTABLE_KINDS:
            if kinds is not None and kind not in kinds:
                continue
            if active[kind] == DEFAULT_SOFT_WEIGHTS[kind]:
                skipped.append(kind)
                continue
            rows.append(
                SoftWeightAdjustment.objects.create(
                    school=school,
                    kind=kind,
                    old_weight=active[kind],
                    new_weight=DEFAULT_SOFT_WEIGHTS[kind],
                    evidence={"reset": True},
                    user=user,
                )
            )
        if rows:
            _audit(audit.SOFT_WEIGHTS_RESET, user, rows)
        return rows, skipped


def history(school):
    return SoftWeightAdjustment.objects.filter(school=school).select_related("user")

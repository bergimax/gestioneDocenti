"""Alternative solutions by weight variation (architecture 7.5): pure functions.

Only the five adjustable soft weights are ever varied, so the hard
constraints, the quasi-hard relax weight and the fixed penalties are the same
in every alternative. The variations are deterministic: the same base weights
always give the same list.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from .context import MAX_SOFT_WEIGHT, ScheduleContext, SolveResult, SolveStatus
from .model import solve
from .weights import resolve_weights

MAX_ALTERNATIVES = 3
LOWER_FACTOR = 0.5
RAISE_FACTOR = 1.5
MIN_WEIGHT = 1  # a varied weight never drops below 1 (0 would switch it off)

# A solve needs this long to be worth starting (seconds).
MIN_SOLVE_SECONDS = 0.5
# Share of an alternative's time slice a solve may use. When the rigid model
# is infeasible the best-effort model runs after it, and the unsat core
# extraction in between, so the three phases together stay within the slice.
SOLVE_SHARE = 0.4
EXPLAIN_SHARE = 0.2

OK_STATUSES = (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)


@dataclass(frozen=True)
class Variation:
    """One alternative's weights; `changed` is None for the unvaried base."""

    index: int
    weights: dict[str, float]
    changed: str | None = None
    factor: float | None = None
    old: float | None = None
    new: float | None = None


@dataclass(frozen=True)
class Alternative:
    variation: Variation
    result: SolveResult
    violations_by_kind: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class AlternativesOutcome:
    alternatives: list[Alternative]  # distinct lesson sets, in variation order
    first_status: SolveStatus | None  # status of the base solve (None: not run)
    collapsed: int = 0  # alternatives dropped as duplicates of an earlier one


def varied(weight: float, factor: float) -> int:
    """`weight * factor` as an integer clamped to [1, MAX_SOFT_WEIGHT]."""
    return max(MIN_WEIGHT, min(MAX_SOFT_WEIGHT, round(weight * factor)))


def weight_variations(base_weights: dict | None) -> list[Variation]:
    """The base weights plus up to two single-weight variations.

    1. the base weights, unchanged;
    2. the heaviest active weight multiplied by 0.5;
    3. the second heaviest active weight multiplied by 1.5.

    Ties are broken by the order of DEFAULT_SOFT_WEIGHTS. A weight of 0 is off
    and is never varied (it would come back as 1). A variation that leaves the
    weights unchanged (a clamp, a rounding) is dropped.
    """
    base = resolve_weights(base_weights)
    out = [Variation(0, dict(base))]
    keys = list(base)
    active = sorted((k for k in keys if base[k] > 0), key=lambda k: -base[k])
    for key, factor in zip(active, (LOWER_FACTOR, RAISE_FACTOR), strict=False):
        new = varied(base[key], factor)
        if new == base[key]:
            continue
        out.append(
            Variation(
                len(out),
                {**base, key: new},
                changed=key,
                factor=factor,
                old=base[key],
                new=new,
            )
        )
    return out[:MAX_ALTERNATIVES]


def lesson_set(result: SolveResult) -> frozenset:
    return frozenset((le.assignment_id, le.day, le.hour) for le in result.lessons)


def violation_counts(result: SolveResult) -> dict[str, int]:
    """Violated penalty terms per constraint kind."""
    return dict(Counter(v.kind for v in result.violations))


def generate_alternatives(
    ctx: ScheduleContext,
    base_weights: dict | None,
    total_seconds: float,
    clock: Callable[[], float],
) -> AlternativesOutcome:
    """Solve the variations of `base_weights` within `total_seconds` in all.

    The time left is shared among the variations still to run, so a quick
    solve leaves more to the next ones; a variation that cannot get at least
    MIN_SOLVE_SECONDS is not started. The base solve comes first: when it has
    no solution the weights cannot help (hard constraints are the same), and
    the outcome carries its status and no alternatives. Variations whose
    lessons equal an earlier one's are collapsed.
    """
    variations = weight_variations(base_weights)
    started = clock()
    alternatives: list[Alternative] = []
    seen: set[frozenset] = set()
    first_status = None
    collapsed = 0
    for i, variation in enumerate(variations):
        remaining = total_seconds - (clock() - started)
        if remaining < MIN_SOLVE_SECONDS:
            break
        share = remaining / (len(variations) - i)
        # Only the first solve may explain an infeasible week (the others
        # would repeat the same explanation).
        explain = ctx.explain_budget_seconds if i == 0 else 0.0
        run_ctx = replace(
            ctx,
            timeout_seconds=share * SOLVE_SHARE,
            explain_budget_seconds=min(explain, share * EXPLAIN_SHARE),
        )
        result = solve(run_ctx, weights=variation.weights)
        if i == 0:
            first_status = result.status
            if result.status not in OK_STATUSES:
                break
        if result.status not in OK_STATUSES:
            continue
        key = lesson_set(result)
        if key in seen:
            collapsed += 1
            continue
        seen.add(key)
        alternatives.append(Alternative(variation, result, violation_counts(result)))
    return AlternativesOutcome(alternatives, first_status, collapsed)

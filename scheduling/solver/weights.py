"""Weights, contractor scarcity and quality score: pure functions (specs 9.4-9.6)."""

from __future__ import annotations

import math
from collections.abc import Mapping

from .context import (
    CONTRATTO,
    DEFAULT_SOFT_WEIGHTS,
    MAX_SOFT_WEIGHT,
    OVERRIDE_AVAILABILITY,
    QUALITY_A_ABOVE,
    QUALITY_B_ABOVE,
    ScheduleContext,
)


def resolve_weights(weights: Mapping | None) -> dict[str, float]:
    """Merge a per-run weight map over the defaults, rejecting bad input."""
    if weights is None:
        weights = {}
    elif not isinstance(weights, Mapping):
        raise ValueError(
            f"soft weights must be a mapping, got {type(weights).__name__}"
        )
    resolved = dict(DEFAULT_SOFT_WEIGHTS)
    for key, value in weights.items():
        if key not in DEFAULT_SOFT_WEIGHTS:
            raise ValueError(
                f"unknown soft weight {key!r}; expected one of "
                f"{sorted(DEFAULT_SOFT_WEIGHTS)}"
            )
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            raise ValueError(f"soft weight {key!r} must be a number, got {value!r}")
        if value < 0 or value > MAX_SOFT_WEIGHT:
            raise ValueError(
                f"soft weight {key!r} must be between 0 and {MAX_SOFT_WEIGHT}, "
                f"got {value!r}"
            )
        resolved[key] = value
    return resolved


def scarcity_multiplier(ctx: ScheduleContext, teacher_id) -> float:
    """m = 1.5 - available_hours / 30 for contractors, 1.0 for everyone else."""
    if ctx.teacher_types.get(teacher_id) != CONTRATTO:
        return 1.0
    if any(
        d.action_type == OVERRIDE_AVAILABILITY
        and (d.teacher_id is None or d.teacher_id == teacher_id)
        for d in ctx.derogations
    ):
        return 1.0
    if ctx.availability is None or teacher_id not in ctx.availability:
        return 1.5  # no grid recorded
    return 1.5 - len(ctx.availability[teacher_id]) / 30


def scaled_weight(base: float, ctx: ScheduleContext, teacher_id) -> int:
    """Final weight max(1, round(base * m)) for a scarcity-sensitive penalty."""
    return max(1, round(base * scarcity_multiplier(ctx, teacher_id)))


def quality(solved: bool, n_penalties: int, n_violated: int) -> tuple[float, str]:
    """Score and level (specs 9.6). No penalty term at all counts as 100 / A."""
    if not solved:
        return 0.0, "C"
    if n_penalties == 0:
        return 100.0, "A"
    score = (n_penalties - n_violated) * 100 / n_penalties
    if score > QUALITY_A_ABOVE:
        return score, "A"
    if score > QUALITY_B_ABOVE:
        return score, "B"
    return score, "C"

"""Unsat cores of an infeasible week (architecture 7.3).

The diagnostic model (``model.build_probe``) enforces every hard constraint that
can make a week infeasible under an assumption literal. This module asks the
solver for a sufficient set of failing assumptions, shrinks it to a minimal one
and repeats on what is left, so that independent causes come out as separate
cores. ``translate.py`` turns the cores into Italian sentences.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from .context import ScheduleContext
from .translate import translate


def _used(solver) -> float:
    """Seconds one solve took; the budget is spent in solver time."""
    return solver.wall_time


@dataclass(frozen=True)
class Explanation:
    messages: list[str] = field(default_factory=list)
    suggested_deroghe: list[dict] = field(default_factory=list)
    # True when the core extraction ran out of time (or found nothing).
    unavailable: bool = False


class _OutOfTime(Exception):
    pass


class _Prober:
    def __init__(self, model, lits, budget):
        self.model = model
        self.lits = lits
        self.remaining = budget
        self.by_index = {lit.index: key for key, lit in lits.items()}

    def core(self, keys):
        """Minimal-ish failing subset of ``keys``, or None if they are feasible."""
        if self.remaining <= 0:
            raise _OutOfTime
        self.model.clear_assumptions()
        self.model.add_assumptions([self.lits[k] for k in keys])
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.remaining
        solver.parameters.num_workers = 1
        solver.parameters.random_seed = 0
        status = solver.solve(self.model)
        self.remaining -= _used(solver)
        if status == cp_model.INFEASIBLE:
            return {
                self.by_index[i]
                for i in solver.sufficient_assumptions_for_infeasibility()
            }
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None
        raise _OutOfTime  # UNKNOWN: the solver stopped on the time limit

    def minimal(self, core):
        """Deletion-based shrink: drop each literal the failure survives without."""
        core = set(core)
        for key in sorted(core, key=repr):
            if key not in core:
                continue
            smaller = self.core(core - {key})
            if smaller is not None:
                core = smaller
        return core


def explain_infeasibility(ctx: ScheduleContext) -> Explanation:
    """Why the rigid model of ``ctx`` has no solution, within the time budget."""
    from .model import build_probe  # model imports this module

    try:
        built = build_probe(ctx)
        if built is None:
            return Explanation(unavailable=True)
        prober = _Prober(*built, ctx.explain_budget_seconds)
        active = set(prober.lits)
        cores = []
        while True:
            failing = prober.core(active)
            if failing is None:
                break
            minimal = prober.minimal(failing)
            cores.append(minimal)
            active -= minimal
    except _OutOfTime:
        return Explanation(unavailable=True)
    if not cores:
        return Explanation(unavailable=True)
    messages, deroghe = translate(ctx, cores)
    return Explanation(messages, deroghe)

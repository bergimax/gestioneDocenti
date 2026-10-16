"""Solver benchmark: a deterministic school-sized week, solved and measured.

No Django and no database: the context is built directly and only
``scheduling.solver`` is imported. The week is feasible by construction: a
timetable is planted first (a Latin-square rotation of six subjects over the
hours), and the assignments, residuals and availability grids are derived from
it, so the solver always has at least one complete solution to find.
"""

from __future__ import annotations

import random
import resource
import sys
import time

from scheduling.solver import (
    ASSUNTO,
    CONTRATTO,
    DAYS,
    HOURS,
    Assignment,
    Pairing,
    ScheduleContext,
    SolveStatus,
    solve,
)

DEFAULT_CLASSES = 40
DEFAULT_SEED = 0
DEFAULT_TIMEOUT_SECONDS = 60.0  # the production SOLVER_TIMEOUT_SECONDS default
SUBJECTS = 6  # subjects per class (the hours rotate over them)
LESSONS_PER_SUBJECT = DAYS * HOURS // SUBJECTS  # 10 a week, 2 a day
CHAIN = 10  # class indexes sharing one teacher per subject (mod CHAIN)
CONTRACT_SHARE = 3  # one teacher in three is CONTRATTO
PAIRINGS = 5
STAGE_CLASSES = 1
EXTRA_AVAILABILITY = (2, 6)  # free cells a contractor offers beyond the planted ones
GROUP_SIZE = 5  # classes per group (same year)


def _shift(class_index: int) -> int:
    # Classes of one chain (same index mod CHAIN) get different shifts, so the
    # teacher they share is never in two places at once.
    return (class_index + class_index // CHAIN) % SUBJECTS


def generate_context(
    n_classes: int = DEFAULT_CLASSES,
    seed: int = DEFAULT_SEED,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ScheduleContext:
    """A feasible, deterministic week of ``n_classes`` classes (default 40)."""
    if n_classes < 1:
        raise ValueError("n_classes must be at least 1")
    rng = random.Random(seed)
    classes = list(range(1, n_classes + 1))

    def teacher_of(index: int, subject: int) -> int:
        return 1 + subject * CHAIN + index % CHAIN

    # Pairings: classes with the same shift in one chain block (index,
    # index + SUBJECTS) follow the same lessons, so one teacher can teach both
    # at once (H3).
    candidates = [
        i
        for i in range(n_classes)
        if i + SUBJECTS < n_classes and i % CHAIN + SUBJECTS < CHAIN
    ]
    rng.shuffle(candidates)
    paired = {}  # second class index -> (first class index, subject)
    for first in candidates:
        if len(paired) == PAIRINGS:
            break
        second = first + SUBJECTS
        taken = set(paired) | {f for f, _ in paired.values()}
        if first in taken or second in taken:
            continue
        paired[second] = (first, rng.randrange(SUBJECTS))

    teacher_ids = sorted(
        {
            teacher_of(i, s)
            for i in range(n_classes)
            for s in range(SUBJECTS)
            if not (i in paired and paired[i][1] == s)
        }
        | {teacher_of(first, s) for first, s in paired.values()}
    )
    contractors = set(rng.sample(teacher_ids, len(teacher_ids) // CONTRACT_SHARE))
    teacher_types = {t: CONTRATTO if t in contractors else ASSUNTO for t in teacher_ids}

    assignments = []
    pairings = []
    planted_cells = {t: set() for t in teacher_ids}
    for index, class_id in enumerate(classes):
        for subject in range(SUBJECTS):
            if index in paired and paired[index][1] == subject:
                first, _ = paired[index]
                teacher = teacher_of(first, subject)
                pairings.append(Pairing(classes[first], class_id, subject, teacher))
            else:
                teacher = teacher_of(index, subject)
            residual = LESSONS_PER_SUBJECT + rng.randrange(3)
            assignments.append(
                Assignment(
                    len(assignments) + 1,
                    teacher,
                    class_id,
                    subject,
                    residual,
                )
            )
            for d in range(DAYS):
                for h in range(HOURS):
                    if (h + d + _shift(index)) % SUBJECTS == subject:
                        planted_cells[teacher].add((d, h))

    availability = {}
    for t in sorted(contractors):
        cells = set(planted_cells[t])
        free = [(d, h) for d in range(DAYS) for h in range(HOURS)]
        free = [c for c in free if c not in cells]
        cells.update(rng.sample(free, min(len(free), rng.randint(*EXTRA_AVAILABILITY))))
        availability[t] = frozenset(cells)

    paired_ids = set(paired) | {f for f, _ in paired.values()}
    stage_pool = [c for i, c in enumerate(classes) if i not in paired_ids]
    stage_days = {
        c: frozenset({rng.randrange(DAYS)})
        for c in rng.sample(stage_pool, min(STAGE_CLASSES, len(stage_pool)))
    }

    return ScheduleContext(
        assignments=assignments,
        teacher_types=teacher_types,
        class_groups={c: (c - 1) // GROUP_SIZE + 1 for c in classes},
        stage_days=stage_days,
        pairings=pairings,
        availability=availability,
        targets={a.id: LESSONS_PER_SUBJECT for a in assignments},
        timeout_seconds=timeout_seconds,
    )


def _peak_memory_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is in KiB on Linux and in bytes on macOS.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def run_benchmark(
    n_classes: int = DEFAULT_CLASSES,
    seed: int = DEFAULT_SEED,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Generate the week, solve it as in production and return the numbers."""
    ctx = generate_context(n_classes, seed, timeout_seconds)
    started = time.perf_counter()
    result = solve(ctx)
    elapsed = time.perf_counter() - started
    return {
        "classes": len({a.class_id for a in ctx.assignments}),
        "teachers": len({a.teacher_id for a in ctx.assignments}),
        "assignments": len(ctx.assignments),
        "pairings": len(ctx.pairings),
        "solve_seconds": round(elapsed, 3),
        "status": str(result.status),
        "quality_score": result.quality_score,
        "quality_level": result.quality_level,
        "violated_penalties": len(result.violations),
        "peak_memory_mb": round(_peak_memory_mb(), 1),
    }


def is_success(report: dict) -> bool:
    return report["status"] in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)

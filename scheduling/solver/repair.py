"""Repair solving (architecture 7.4): the perturbation and the diff, Django-free.

A perturbation changes the context of one solve (a teacher absent, a slot
removed, a class closed). The solver then minimises the lessons that differ
from the current timetable (``ScheduleContext.current_lessons``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .context import Id, Lesson, ScheduleContext
from .context import capacity as _capacity

TEACHER_ABSENT = "teacher_absent"
SLOT_REMOVED = "slot_removed"
CLASS_CLOSED = "class_closed"
KINDS = (TEACHER_ABSENT, SLOT_REMOVED, CLASS_CLOSED)


@dataclass(frozen=True)
class Perturbation:
    """What changed. ``days`` are day indexes 0-4; ``hour`` is an hour index 0-5."""

    kind: str
    teacher_id: Id | None = None
    class_id: Id | None = None
    days: frozenset[int] = field(default_factory=frozenset)
    hour: int | None = None


@dataclass(frozen=True)
class Change:
    """One lesson of the diff: ``old`` and/or ``new`` is a (day, hour) cell.

    Both set: the lesson moved. Only ``old``: removed. Only ``new``: added.
    """

    assignment_id: Id
    old: tuple[int, int] | None
    new: tuple[int, int] | None


def apply_perturbation(ctx: ScheduleContext, pert: Perturbation) -> ScheduleContext:
    """The context of the repair: ``ctx`` with the perturbation switched on."""
    if pert.kind == TEACHER_ABSENT:
        excluded = dict(ctx.excluded_days)
        excluded[pert.teacher_id] = frozenset(
            excluded.get(pert.teacher_id, frozenset()) | pert.days
        )
        return replace(ctx, excluded_days=excluded)
    capacity = {k: dict(v) for k, v in ctx.class_capacity.items()}
    if pert.kind == CLASS_CLOSED:
        for day in pert.days:
            capacity.setdefault(pert.class_id, {})[day] = 0
        return replace(ctx, class_capacity=capacity)
    if pert.kind == SLOT_REMOVED:
        blocked = dict(ctx.blocked_cells)
        cells = set(blocked.get(pert.class_id, ()))
        for day in pert.days:
            cells.add((day, pert.hour))
            capacity.setdefault(pert.class_id, {})[day] = max(
                0, _capacity(ctx, pert.class_id, day) - 1
            )
        blocked[pert.class_id] = frozenset(cells)
        return replace(ctx, class_capacity=capacity, blocked_cells=blocked)
    raise ValueError(f"unknown perturbation kind: {pert.kind!r}")


def affected_lessons(
    ctx: ScheduleContext, pert: Perturbation, current: list[Lesson]
) -> list[Lesson]:
    """The current lessons the perturbation takes away (empty: nothing to repair)."""
    by_id = {a.id: a for a in ctx.assignments}
    hit = []
    for le in current:
        a = by_id.get(le.assignment_id)
        if a is None or le.day not in pert.days:
            continue
        if pert.kind == TEACHER_ABSENT:
            ok = a.teacher_id == pert.teacher_id
        elif pert.kind == CLASS_CLOSED:
            ok = a.class_id == pert.class_id
        else:
            ok = a.class_id == pert.class_id and le.hour == pert.hour
        if ok:
            hit.append(le)
    return hit


def _key(cell):
    return (cell[1], cell[2], str(cell[0]))


def diff_lessons(old: list[Lesson], new: list[Lesson]) -> list[Change]:
    """Moved, removed and added lessons, per assignment, in a stable order.

    A lesson that left a cell and appeared in another one of the same
    assignment is one ``moved`` change; a removed cell prefers an added cell of
    the same day.
    """
    old_cells = {(le.assignment_id, le.day, le.hour) for le in old}
    new_cells = {(le.assignment_id, le.day, le.hour) for le in new}
    removed: dict = {}
    added: dict = {}
    for aid, d, h in sorted(old_cells - new_cells, key=_key):
        removed.setdefault(aid, []).append((d, h))
    for aid, d, h in sorted(new_cells - old_cells, key=_key):
        added.setdefault(aid, []).append((d, h))
    changes = []
    for aid in sorted(set(removed) | set(added), key=str):
        gone, came = list(removed.get(aid, [])), list(added.get(aid, []))
        for cell in list(gone):
            if not came:
                break
            target = next((c for c in came if c[0] == cell[0]), came[0])
            gone.remove(cell)
            came.remove(target)
            changes.append(Change(aid, cell, target))
        changes += [Change(aid, cell, None) for cell in gone]
        changes += [Change(aid, None, cell) for cell in came]
    changes.sort(key=lambda c: (c.old or c.new, str(c.assignment_id)))
    return changes

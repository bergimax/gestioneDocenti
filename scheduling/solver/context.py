"""Plain dataclasses for the solver input and output (specs section 9.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

DAYS = 5  # Monday..Friday, day index 0-4
HOURS = 6  # hour index 0..5 = 08:00..13:00
FIRST_HOUR = 8  # clock hour of hour index 0

ASSUNTO = "ASSUNTO"
CONTRATTO = "CONTRATTO"

Id = int | str


class SolveStatus(StrEnum):
    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Assignment:
    """A teacher teaching a subject to a class, with the hours still to deliver."""

    id: Id
    teacher_id: Id
    class_id: Id
    subject_id: Id
    residual_hours: int
    subject_type: str = ""


@dataclass(frozen=True)
class Pairing:
    """One lesson given by one teacher to two classes at once (H3)."""

    class_a: Id
    class_b: Id
    subject_id: Id
    teacher_id: Id


@dataclass(frozen=True)
class Lesson:
    assignment_id: Id
    day: int  # 0-4
    hour: int  # 0-5 (0 = 08:00)


@dataclass(frozen=True)
class ScheduleContext:
    """Everything the solver needs; it reads nothing else.

    Conventions:
    - ``class_capacity[class_id][day]``: lessons that day (0-6, clamped to 6);
      a missing entry means 6.
    - ``class_min_start[class_id]``: first clock hour (8-13) with a lesson;
      missing means 8.
    - ``stage_days[class_id]``: days in stage, forced to capacity 0.
    - ``teacher_types``: a teacher missing from the map is treated as ASSUNTO
      (the stricter H6 rule).
    - ``excluded_days[teacher_id]`` (H7) and ``weekly_caps[teacher_id]`` (H8)
      are run-only inputs.
    """

    assignments: list[Assignment] = field(default_factory=list)
    teacher_types: dict[Id, str] = field(default_factory=dict)
    class_groups: dict[Id, Id] = field(default_factory=dict)
    class_capacity: dict[Id, dict[int, int]] = field(default_factory=dict)
    class_min_start: dict[Id, int] = field(default_factory=dict)
    stage_days: dict[Id, frozenset[int]] = field(default_factory=dict)
    pairings: list[Pairing] = field(default_factory=list)
    excluded_days: dict[Id, frozenset[int]] = field(default_factory=dict)
    weekly_caps: dict[Id, int] = field(default_factory=dict)
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class SolveResult:
    status: SolveStatus
    lessons: list[Lesson] = field(default_factory=list)

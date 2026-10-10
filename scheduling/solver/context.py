"""Plain dataclasses for the solver input and output (specs section 9.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

DAYS = 5  # Monday..Friday, day index 0-4
HOURS = 6  # hour index 0..5 = 08:00..13:00
FIRST_HOUR = 8  # clock hour of hour index 0

ASSUNTO = "ASSUNTO"
CONTRATTO = "CONTRATTO"
PRATICA = "PRATICA"

RELAX_WEIGHT = 1000  # penalty of one quasi-hard violation (specs 9.4)
MAX_HOURS_PER_CLASSE_DAY = 4  # H11

OVERRIDE_AVAILABILITY = "override_availability"  # H4
AUTHORIZE_FRIDAY_LATE_START = "authorize_friday_late_start"  # H9
AUTHORIZE_SHORT_PRATICA_BLOCK = "authorize_short_pratica_block"  # H10
AUTHORIZE_SINGLE_CLASSE_DAY = "authorize_single_classe_day"  # H11

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
class Derogation:
    """Switches a quasi-hard constraint off inside its scope.

    An omitted scope field (``None``) means "all".
    """

    action_type: str
    class_id: Id | None = None
    teacher_id: Id | None = None
    day: int | None = None


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
    - ``availability[teacher_id]``: the (day, hour) cells where the teacher is
      available (H4). Only CONTRATTO teachers are constrained by it. ``None``
      (the default) means no availability data was supplied and H4 is not
      modelled (keeps #22 contexts unchanged); once a dict is given, a
      contractor missing from it has no grid and is unavailable all week.
    - ``availability_relaxed``: H4 becomes a 1000-weight override instead of
      rigid (used by manual edits).
    - ``derogations``: see ``Derogation``.
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
    availability: dict[Id, frozenset[tuple[int, int]]] | None = None
    availability_relaxed: bool = False
    derogations: list[Derogation] = field(default_factory=list)
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class Conflict:
    """A quasi-hard violation (H4, H9, H10, H11) the solver was forced into."""

    kind: str
    class_id: Id
    teacher_id: Id | None
    day: int
    hour: int | None
    description: str
    action_type: str  # the derogation that would authorise it
    penalty: int = RELAX_WEIGHT


@dataclass(frozen=True)
class SolveResult:
    status: SolveStatus
    lessons: list[Lesson] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    # Sum of the override variables times their weight, read from the solution.
    override_penalty: int = 0

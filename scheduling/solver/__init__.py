"""Framework-free timetable solver: standard library and OR-Tools only."""

from .context import (
    ASSUNTO,
    CONTRATTO,
    DAYS,
    HOURS,
    Assignment,
    Lesson,
    Pairing,
    ScheduleContext,
    SolveResult,
    SolveStatus,
)
from .model import solve

__all__ = [
    "ASSUNTO",
    "CONTRATTO",
    "DAYS",
    "HOURS",
    "Assignment",
    "Lesson",
    "Pairing",
    "ScheduleContext",
    "SolveResult",
    "SolveStatus",
    "solve",
]

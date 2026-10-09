"""Weekly availability grid logic (specs section 6).

Pure: no Django, no database. Grids are plain dicts (`giorni_fasce`), weeks
are `datetime.date` Mondays. Nothing here raises on a malformed grid read
from storage: `grid_errors` and `resolve_grid` report problems as data.
"""

import copy
import datetime
import re
from dataclasses import dataclass, field

DAYS = ("lunedi", "martedi", "mercoledi", "giovedi", "venerdi")
SLOT_KEYS = frozenset({"ora_inizio", "ora_fine", "disponibile"})
_TIME = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")

STORED = "STORED"
INHERITED = "INHERITED"
DEFAULT = "DEFAULT"


def default_grid():
    """A fresh all-available grid: six one-hour slots, 08:00-14:00."""
    return {
        day: [
            {
                "ora_inizio": f"{h:02d}:00",
                "ora_fine": f"{h + 1:02d}:00",
                "disponibile": True,
            }
            for h in range(8, 14)
        ]
        for day in DAYS
    }


def _is_time(value):
    return isinstance(value, str) and _TIME.fullmatch(value) is not None


def grid_errors(grid):
    """List of problems with `grid`; empty when valid. Never raises."""
    if not isinstance(grid, dict):
        return ["La griglia deve essere un oggetto con i cinque giorni."]
    errors = []
    for key in grid:
        if key not in DAYS:
            errors.append(f"Giorno sconosciuto: {key!r}.")
    for day in DAYS:
        if day not in grid:
            errors.append(f"Giorno mancante: {day}.")
            continue
        slots = grid[day]
        if not isinstance(slots, list) or not slots:
            errors.append(f"{day}: serve una lista non vuota di fasce.")
            continue
        for i, slot in enumerate(slots):
            where = f"{day}[{i}]"
            if not isinstance(slot, dict):
                errors.append(f"{where}: la fascia deve essere un oggetto.")
                continue
            for k in slot:
                if k not in SLOT_KEYS:
                    errors.append(f"{where}: chiave sconosciuta {k!r}.")
            for k in sorted(SLOT_KEYS):
                if k not in slot:
                    errors.append(f"{where}: manca {k}.")
            for k in ("ora_inizio", "ora_fine"):
                if k in slot and not _is_time(slot[k]):
                    errors.append(f"{where}: {k} deve essere HH:MM.")
            if "disponibile" in slot and type(slot["disponibile"]) is not bool:
                errors.append(f"{where}: disponibile deve essere booleano.")
    return errors


def validate_grid(grid):
    """Raise ValueError naming the problems if `grid` is not valid."""
    errors = grid_errors(grid)
    if errors:
        raise ValueError(" ".join(errors))


def check_monday(week_start):
    """Raise ValueError unless `week_start` is a date falling on a Monday."""
    if not isinstance(week_start, datetime.date) or week_start.weekday() != 0:
        raise ValueError(f"week_start deve essere un lunedi: {week_start!r}.")


@dataclass
class Resolution:
    grid: object
    source: str
    from_week: datetime.date | None = None
    # Non-empty only when the chosen stored grid is malformed.
    errors: list = field(default_factory=list)


def resolve_grid(week_start, stored, first_time):
    """Pick the grid for `week_start` (specs section 6, "Lettura").

    `stored` maps Monday -> grid for one teacher. Order: the record of the
    week; first-year teacher -> default; latest strictly earlier record;
    default. The returned grid is always a copy.
    """
    check_monday(week_start)
    if week_start in stored:
        grid = stored[week_start]
        return Resolution(copy.deepcopy(grid), STORED, None, grid_errors(grid))
    if not first_time:
        earlier = [w for w in stored if w < week_start]
        if earlier:
            best = max(earlier)
            grid = stored[best]
            return Resolution(copy.deepcopy(grid), INHERITED, best, grid_errors(grid))
    return Resolution(default_grid(), DEFAULT)


def completion_percentage(total, filled):
    """filled / total * 100 rounded to one decimal; 0 when total is 0."""
    if total <= 0:
        return 0
    return round(filled / total * 100, 1)


def list_weeks(year_start, year_end):
    """Every Monday from the first on/after year_start up to year_end."""
    first = year_start + datetime.timedelta(days=(-year_start.weekday()) % 7)
    weeks = []
    start = first
    while start <= year_end:
        weeks.append(
            {
                "start": start,
                "end": start + datetime.timedelta(days=4),
                "week_num": len(weeks) + 1,
            }
        )
        start += datetime.timedelta(days=7)
    return weeks

"""Library-independent layout of the exported timetable (specs §12).

A plain data structure shared by the PDF renderer and, later, the Excel
export. It never imports a rendering library.
"""

import datetime
import re
from dataclasses import dataclass

from scheduling.board import HOURS, build_board

TITLE_BG = "#95b3d7"
HEADER_BG = "#c00000"
EMPTY_BG = "#d9d9d9"
STAGE_BG = "#ffff00"
WHITE = "#ffffff"
DAY_COLORS = ("#e46c0a", "#0070c0", "#e46c0a", "#0070c0", "#e46c0a")
STAGE_TEXT = "STAGE"

MONTHS = (
    "Gennaio",
    "Febbraio",
    "Marzo",
    "Aprile",
    "Maggio",
    "Giugno",
    "Luglio",
    "Agosto",
    "Settembre",
    "Ottobre",
    "Novembre",
    "Dicembre",
)
COURSES = ("OP. INFORM.", "ELETTRICISTI", "ESTETISTE", "PAN. E PAST", "I.T.C.")
YEARS = {"I": 1, "II": 2, "III": 3, "IV": 4}

# (course, year) -> background of the lesson cells; anything else is white.
LESSON_COLORS = {
    ("OP. INFORM.", 1): "#ddd9c4",
    ("OP. INFORM.", 2): "#f2dcdb",
    ("OP. INFORM.", 3): "#92d050",
    ("ELETTRICISTI", 1): "#ddd9c4",
    ("ELETTRICISTI", 2): "#f2dcdb",
    ("ELETTRICISTI", 3): "#ff99cc",
    ("ESTETISTE", 1): "#c5d9f1",
    ("ESTETISTE", 2): "#fde9d9",
    ("ESTETISTE", 3): "#ff99cc",
    ("PAN. E PAST", 1): "#c5d9f1",
    ("PAN. E PAST", 2): "#fde9d9",
    ("PAN. E PAST", 3): "#92d050",
    ("I.T.C.", 1): "#ffff99",
}

_NAME = re.compile(r"^\s*(IV|III|II|I)\s+(.+?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ClassColumn:
    name: str
    course: str | None
    year: int | None
    background: str  # colour of this class's lesson cells


@dataclass(frozen=True)
class LayoutCell:
    kind: str  # "lesson", "stage" or "empty"
    text: str
    background: str
    bold: bool


@dataclass(frozen=True)
class HourRow:
    label: str  # "8-9"
    cells: tuple  # one LayoutCell per class, in the order of Layout.classes


@dataclass(frozen=True)
class DayBlock:
    label: str  # lowercase day name
    background: str
    rows: tuple


@dataclass(frozen=True)
class Layout:
    title_lines: tuple
    header: tuple  # DATA, Orario, class names
    classes: tuple
    days: tuple


def parse_class_name(name):
    """(course, year) from "<roman year> <COURSE>", else (None, None)."""
    match = _NAME.match(name or "")
    if match:
        course = " ".join(match.group(2).upper().split())
        if course in COURSES:
            return course, YEARS[match.group(1).upper()]
    return None, None


def class_sort_key(name):
    course, year = parse_class_name(name)
    if course is None:
        return (1, 0, 0, name.casefold())
    return (0, COURSES.index(course), year, name.casefold())


def lesson_background(course, year):
    return LESSON_COLORS.get((course, year), WHITE)


def week_title(week_start):
    """ "Orario scolastico dal 5 al 9 Ottobre" (Monday to Friday)."""
    end = week_start + datetime.timedelta(days=4)
    if end.month == week_start.month:
        return (
            f"Orario scolastico dal {week_start.day} al {end.day} "
            f"{MONTHS[end.month - 1]}"
        )
    return (
        f"Orario scolastico dal {week_start.day} {MONTHS[week_start.month - 1]} "
        f"al {end.day} {MONTHS[end.month - 1]}"
    )


def build_layout(school, week_start, timetable):
    """The layout of `timetable`, built on scheduling.board.build_board()."""
    days = build_board(school, week_start, timetable)
    rows0 = days[0]["rows"] if days else []
    ordered = sorted(
        range(len(rows0)), key=lambda i: class_sort_key(rows0[i]["school_class"].name)
    )
    columns = []
    for i in ordered:
        name = rows0[i]["school_class"].name
        course, year = parse_class_name(name)
        columns.append(ClassColumn(name, course, year, lesson_background(course, year)))

    blocks = []
    for index, day in enumerate(days):
        by_hour = []
        for h in range(len(HOURS)):
            cells = []
            for i, column in zip(ordered, columns, strict=True):
                cell = day["rows"][i]["cells"][h]
                if cell["kind"] == "lesson":
                    cells.append(
                        LayoutCell("lesson", cell["teacher"], column.background, True)
                    )
                elif cell["kind"] == "stage":
                    cells.append(LayoutCell("stage", STAGE_TEXT, STAGE_BG, True))
                else:
                    cells.append(LayoutCell("empty", "", EMPTY_BG, False))
            by_hour.append(cells)
        last = max(
            (
                h
                for h, cells in enumerate(by_hour)
                if any(c.kind == "lesson" for c in cells)
            ),
            default=0,  # no lesson at all: keep one row so the day shows
        )
        rows = tuple(
            HourRow(f"{HOURS[h]}-{HOURS[h] + 1}", tuple(by_hour[h]))
            for h in range(last + 1)
        )
        blocks.append(DayBlock(day["label"].lower(), DAY_COLORS[index], rows))

    return Layout(
        title_lines=(school.name, week_title(week_start)),
        header=("DATA", "Orario", *(c.name for c in columns)),
        classes=tuple(columns),
        days=tuple(blocks),
    )


__all__ = ["Layout", "build_layout"]

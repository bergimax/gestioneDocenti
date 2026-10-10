"""Read model of the timetable page: classes by hour for each weekday."""

import datetime

from school.models import CalendarEntry, Class, SubjectKind

from .context import _is_stage
from .models import Day, Slot
from .solver.context import CLASSE_UNFILLED
from .solver.model import NO_COMPLETE_MESSAGE

FIRST_HOUR = 8
HOURS = list(range(FIRST_HOUR, FIRST_HOUR + 6))
DAY_LABELS = {
    Day.LUNEDI: "Lunedì",
    Day.MARTEDI: "Martedì",
    Day.MERCOLEDI: "Mercoledì",
    Day.GIOVEDI: "Giovedì",
    Day.VENERDI: "Venerdì",
}


def surname(teacher):
    """First word of "COGNOME NOME"."""
    parts = teacher.name.split()
    return parts[0] if parts else ""


# Quasi-hard conflicts tied to a day, not to an hour: H4 (teacher unavailable)
# and H11 (too many hours with one class) mark the teacher's lessons with that
# class on that day, H10 (broken practical block) the practical lessons of the
# class on that day.
TEACHER_DAY_KINDS = {"H4", "H11"}
PRATICA_DAY_KIND = "H10"


def conflict_keys(conflicts):
    """Sets of (class_id, day, hour) and (teacher_id, day, hour) in conflict.

    Conflicts tied to one slot (`giorno` and `ore_slot`) mark cells; the
    day-level ones of TEACHER_DAY_KINDS fill `by_day` with (class_id,
    teacher_id, day) and H10 fills `pratica_days` with (class_id, day).
    The others appear in the list alone.
    """
    by_class, by_teacher, by_day, pratica_days = set(), set(), set(), set()
    for c in conflicts or []:
        if not isinstance(c, dict):
            continue
        day, slot = c.get("giorno"), c.get("ore_slot")
        if day is not None and slot is None and c.get("classe_id") is not None:
            if c.get("kind") in TEACHER_DAY_KINDS and c.get("docente_id") is not None:
                by_day.add((c["classe_id"], c["docente_id"], day))
            elif c.get("kind") == PRATICA_DAY_KIND:
                pratica_days.add((c["classe_id"], day))
        if day is None or not isinstance(slot, int):
            continue
        hour = FIRST_HOUR + slot
        if c.get("classe_id") is not None:
            by_class.add((c["classe_id"], day, hour))
        if c.get("docente_id") is not None:
            by_teacher.add((c["docente_id"], day, hour))
    return by_class, by_teacher, by_day, pratica_days


def conflict_descriptions(conflicts):
    return [
        c["description"]
        for c in conflicts or []
        if isinstance(c, dict) and c.get("description")
    ]


def is_best_effort(conflicts):
    return any(
        isinstance(c, dict) and c.get("kind") == CLASSE_UNFILLED
        for c in conflicts or []
    )


def build_board(school, week_start, timetable):
    """Days -> rows (one per class) -> six cells, for `timetable`."""
    classes = list(Class.objects.filter(school=school).order_by("name", "pk"))
    dates = [week_start + datetime.timedelta(days=i) for i in range(5)]
    entries = list(CalendarEntry.objects.filter(school=school, date__in=dates))
    by_class, by_teacher, by_day, pratica_days = conflict_keys(timetable.conflicts)

    lessons = {}  # (class_id, day, hour) -> cell
    slots = list(
        Slot.objects.filter(timetable=timetable).select_related("teacher", "subject")
    )

    def lesson_cell(slot, class_id):
        key = (class_id, slot.day, slot.start_hour)
        return {
            "kind": "lesson",
            "slot_id": slot.pk,
            "subject": slot.subject.name,
            "teacher": surname(slot.teacher),
            "paired": slot.paired,
            "conflict": key in by_class
            or (slot.teacher_id, slot.day, slot.start_hour) in by_teacher
            or (class_id, slot.teacher_id, slot.day) in by_day
            or (
                slot.subject.kind == SubjectKind.PRATICA
                and (class_id, slot.day) in pratica_days
            ),
        }

    for slot in slots:
        lessons[slot.school_class_id, slot.day, slot.start_hour] = lesson_cell(
            slot, slot.school_class_id
        )
    for slot in slots:  # a paired lesson also shows in the partner class
        if slot.paired_class_id:
            lessons.setdefault(
                (slot.paired_class_id, slot.day, slot.start_hour),
                lesson_cell(slot, slot.paired_class_id),
            )

    days = []
    for index, day in enumerate(Day.values):
        rows = []
        for school_class in classes:
            stage = _is_stage(entries, dates[index], school_class)
            cells = []
            for hour in HOURS:
                cell = lessons.get((school_class.pk, day, hour))
                if cell is None:
                    cell = {"kind": "stage" if stage else "empty"}
                    cell["conflict"] = (school_class.pk, day, hour) in by_class
                cell["hour"] = hour
                cell["day"] = day
                cells.append(cell)
            rows.append({"school_class": school_class, "cells": cells})
        days.append(
            {"key": day, "label": DAY_LABELS[day], "date": dates[index], "rows": rows}
        )
    return days


__all__ = ["HOURS", "NO_COMPLETE_MESSAGE", "build_board", "is_best_effort"]

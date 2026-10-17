"""Read model of the timetable page: classes by hour for each weekday."""

import datetime
import re

from school.models import CalendarEntry, Class, SubjectKind, Teacher

from .context import _is_stage
from .derogations import QUICK_ACTIONS, label_of
from .models import ConflictDecision, Day, JobKind, JobStatus, Slot, SolveJob
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


def decided_keys(timetable):
    return set(
        ConflictDecision.objects.filter(timetable=timetable).values_list(
            "chiave", flat=True
        )
    )


def visible_conflicts(timetable):
    """The stored conflicts without the approved ones and the LIBERA holes.

    The stored list and the score are left alone: only the reporting changes.
    """
    conflicts = timetable.conflicts or []
    if not conflicts:
        return []
    decided = decided_keys(timetable)
    return [
        c for c in conflicts if not (isinstance(c, dict) and c.get("chiave") in decided)
    ]


def conflict_descriptions(conflicts):
    return [
        c["description"]
        for c in conflicts or []
        if isinstance(c, dict) and c.get("description")
    ]


def _quick(conflict):
    """The suggested quick action of a conflict, or None.

    assign_teacher and manual_edit are suggestions too, but not quick actions.
    """
    suggested = conflict.get("suggested_action") or {}
    if suggested.get("action_type") not in QUICK_ACTIONS:
        return None
    return {
        "action_type": suggested["action_type"],
        "label": suggested.get("label") or label_of(suggested["action_type"]),
        "classe": conflict.get("classe_id"),
        "docente": conflict.get("docente_id"),
        "giorno": conflict.get("giorno"),
    }


def _named_text(text, names):
    """Replace "classe N", "docente N", "contrattista N" with the stored name.

    `names` maps word -> (id, name). Only the item's own whole-number id
    matches (id 3 never touches 31 or 3.5).
    """

    def swap(match):
        found = names.get(match.group(1))
        if found and match.group(2) == str(found[0]):
            return f"{match.group(1)} {found[1]}"
        return match.group(0)

    return _ID_PATTERN.sub(swap, text)


_ID_PATTERN = re.compile(r"\b(classe|docente|contrattista) (\d+)(?!\d|\.\d)")


def conflict_rows(conflicts, school=None):
    """Text, chiave and suggested quick action of each reported conflict.

    The text shows class and teacher names of `school` instead of the ids the
    solver wrote; the stored conflicts are never changed. Unknown ids leave the
    text as stored.
    """
    items = [c for c in conflicts or [] if isinstance(c, dict) and c.get("description")]
    class_ids = {c["classe_id"] for c in items if isinstance(c.get("classe_id"), int)}
    teacher_ids = {
        c["docente_id"] for c in items if isinstance(c.get("docente_id"), int)
    }
    class_names, teacher_names = {}, {}
    if school is not None:
        if class_ids:
            class_names = dict(
                Class.objects.filter(school=school, pk__in=class_ids).values_list(
                    "pk", "name"
                )
            )
        if teacher_ids:
            teacher_names = dict(
                Teacher.objects.filter(school=school, pk__in=teacher_ids).values_list(
                    "pk", "name"
                )
            )
    rows = []
    for c in items:
        names = {}
        if c.get("classe_id") in class_names:
            names["classe"] = (c["classe_id"], class_names[c["classe_id"]])
        if c.get("docente_id") in teacher_names:
            found = (c["docente_id"], teacher_names[c["docente_id"]])
            names["docente"] = names["contrattista"] = found
        rows.append(
            {
                "text": _named_text(c["description"], names),
                "chiave": c.get("chiave", ""),
                "quick": _quick(c),
            }
        )
    return rows


def explanations(timetable):
    """Why the week has no complete timetable, from the job that wrote it.

    Read from the stored job result: the solver is never called while rendering.
    """
    job = (
        SolveJob.objects.filter(
            school_id=timetable.school_id,
            week_start=timetable.week_start,
            kind=JobKind.GENERATE,
            status=JobStatus.DONE,
        )
        .order_by("-pk")
        .first()
    )
    if job is None or job.result.get("timetable_id") != timetable.pk:
        return []
    return [m for m in job.result.get("explanations") or [] if isinstance(m, str)]


def is_best_effort(conflicts):
    return any(
        isinstance(c, dict) and c.get("kind") == CLASSE_UNFILLED
        for c in conflicts or []
    )


def build_board(school, week_start, timetable, slots=None, conflicts=None):
    """Days -> rows (one per class) -> six cells, for `timetable`.

    `slots` and `conflicts` show a week that is not stored (an alternative
    before it is picked): unsaved Slots with `teacher` and `subject` set, and
    the solver's conflict items; `timetable` is then not read.
    """
    classes = list(Class.objects.filter(school=school).order_by("name", "pk"))
    dates = [week_start + datetime.timedelta(days=i) for i in range(5)]
    entries = list(CalendarEntry.objects.filter(school=school, date__in=dates))
    by_class, by_teacher, by_day, pratica_days = conflict_keys(
        visible_conflicts(timetable) if conflicts is None else conflicts
    )

    lessons = {}  # (class_id, day, hour) -> cell
    if slots is None:
        slots = list(
            Slot.objects.filter(timetable=timetable).select_related(
                "teacher", "subject"
            )
        )

    def lesson_cell(slot, class_id):
        key = (class_id, slot.day, slot.start_hour)
        return {
            "kind": "lesson",
            "slot_id": slot.pk,
            "subject": slot.subject.name,
            "teacher": surname(slot.teacher),
            "teacher_id": slot.teacher_id,
            "subject_kind": slot.subject.kind,
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


__all__ = [
    "HOURS",
    "NO_COMPLETE_MESSAGE",
    "build_board",
    "explanations",
    "is_best_effort",
]

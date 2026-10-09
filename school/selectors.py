import calendar as pycal
import datetime

from django.db.models import Count, Q
from django.db.models.functions import Lower
from django.utils import timezone

from . import calendar as day_rules
from .assignment_hours import delivered_hours_map
from .models import (
    Assignment,
    CalendarEntry,
    Class,
    ClassPairing,
    Group,
    School,
    Subject,
    Teacher,
)

# (related name, singular, plural) of every relation that blocks a delete.
TEACHER_REFERENCES = (
    ("assignments", "assegnazione", "assegnazioni"),
    ("class_pairings", "accoppiamento", "accoppiamenti"),
    ("availabilities", "disponibilità", "disponibilità"),
    ("slots", "slot dell'orario", "slot dell'orario"),
)


def current_school():
    """The school the application works on (single-school in practice)."""
    return School.objects.order_by("pk").first()


def teachers_of(school):
    """Teachers of `school` with their assignment count, in one query."""
    return (
        Teacher.objects.filter(school=school)
        .annotate(assignment_count=Count("assignments"))
        .order_by(Lower("name"), "pk")
    )


def teacher_reference_summary(teacher):
    """Italian description of the records that block deleting `teacher`.

    Empty string when nothing references it.
    """
    parts = []
    for related, singular, plural in TEACHER_REFERENCES:
        count = getattr(teacher, related).count()
        if count:
            parts.append(f"{count} {singular if count == 1 else plural}")
    return ", ".join(parts)


def _plural(count, singular, plural):
    return f"{count} {singular if count == 1 else plural}"


def classes_of(school):
    """Classes of `school` with their assignment count, in one query."""
    return (
        Class.objects.filter(school=school)
        .annotate(assignment_count=Count("assignments", distinct=True))
        .order_by(Lower("name"), "pk")
    )


def subjects_of(school):
    """Subjects of `school` with their assignment count, in one query."""
    return (
        Subject.objects.filter(school=school)
        .annotate(assignment_count=Count("assignments", distinct=True))
        .order_by(Lower("name"), "pk")
    )


def _summary(parts):
    return ", ".join(_plural(count, *words) for count, words in parts if count)


def class_reference_summary(school_class):
    """Italian description of the records that block deleting `school_class`."""
    c = school_class
    return _summary(
        [
            (c.assignments.count(), ("assegnazione", "assegnazioni")),
            (
                c.pairings_as_a.count() + c.pairings_as_b.count(),
                ("accoppiamento", "accoppiamenti"),
            ),
            (
                c.calendar_stage_entries.count(),
                ("giorno di stage nel calendario", "giorni di stage nel calendario"),
            ),
            (
                c.slots.count() + c.paired_slots.count(),
                ("slot dell'orario", "slot dell'orario"),
            ),
        ]
    )


def subject_reference_summary(subject):
    """Italian description of the records that block deleting `subject`."""
    s = subject
    return _summary(
        [
            (s.assignments.count(), ("assegnazione", "assegnazioni")),
            (s.class_pairings.count(), ("accoppiamento", "accoppiamenti")),
            (s.slots.count(), ("slot dell'orario", "slot dell'orario")),
        ]
    )


def assignments_of(school):
    """Assignments of `school` with teacher, class and subject, in one query."""
    return (
        Assignment.objects.filter(school=school)
        .select_related("teacher", "school_class", "subject")
        .order_by(
            Lower("teacher__name"),
            Lower("school_class__name"),
            Lower("subject__name"),
            "pk",
        )
    )


def assignment_rows(school):
    """The assignments list with delivered and remaining hours (may be < 0)."""
    delivered = delivered_hours_map(school)
    rows = []
    for assignment in assignments_of(school):
        assignment.delivered = delivered.get(assignment.pk, 0)
        assignment.remaining = assignment.ore_totali - assignment.delivered
        rows.append(assignment)
    return rows


def pairings_of(school):
    """Pairings of `school` with classes, subject and teacher, in one query."""
    return (
        ClassPairing.objects.filter(school=school)
        .select_related("class_a", "class_b", "subject", "teacher")
        .order_by(
            Lower("class_a__name"),
            Lower("class_b__name"),
            Lower("subject__name"),
            "pk",
        )
    )


def pairings_relying_on(assignment):
    """Pairings that need `assignment` to stay valid (specs 5.4).

    A pairing of the same subject and class relies on it when it names this
    teacher, or names no teacher and this is the last assignment of the
    class and subject.
    """
    others = (
        Assignment.objects.filter(
            school=assignment.school_id,
            school_class=assignment.school_class_id,
            subject=assignment.subject_id,
        )
        .exclude(pk=assignment.pk)
        .exists()
    )
    reliant = Q(teacher=assignment.teacher_id)
    if not others:
        reliant |= Q(teacher__isnull=True)
    return pairings_of(assignment.school_id).filter(
        Q(class_a=assignment.school_class_id) | Q(class_b=assignment.school_class_id),
        reliant,
        subject=assignment.subject_id,
    )


# --- annual calendar -------------------------------------------------------

MONTH_NAMES = (
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
WEEKDAY_NAMES = (
    "Lunedì",
    "Martedì",
    "Mercoledì",
    "Giovedì",
    "Venerdì",
    "Sabato",
    "Domenica",
)
WEEKDAY_ABBR = ("Lun", "Mar", "Mer", "Gio", "Ven")
GROUP_ORDER = (
    Group.PRIME,
    Group.SECONDE,
    Group.TERZE,
    Group.QUARTE,
    Group.PRIMA4_2,
)


def today():
    return timezone.localdate()


def day_label(day):
    """Italian "8 ottobre" for a date."""
    return f"{day.day} {MONTH_NAMES[day.month - 1].lower()}"


def month_title(first):
    return f"{MONTH_NAMES[first.month - 1]} {first.year}"


def _first_of(day):
    return day.replace(day=1)


def _shift(first, delta):
    index = first.year * 12 + first.month - 1 + delta
    return datetime.date(index // 12, index % 12 + 1, 1)


def resolve_month(school, raw):
    """First day of the month to show: `raw` ("YYYY-MM") clamped to the year."""
    low, high = _first_of(school.year_start), _first_of(school.year_end)
    try:
        year, month = raw.split("-")
        if len(year) != 4 or len(month) != 2:
            raise ValueError
        wanted = datetime.date(int(year), int(month), 1)
    except (AttributeError, ValueError):
        now = _first_of(today())
        return now if low <= now <= high else low
    return min(max(wanted, low), high)


def entries_of_month(school, first):
    """Every calendar row of the month, in ONE query (stage class included)."""
    last = datetime.date(
        first.year, first.month, pycal.monthrange(first.year, first.month)[1]
    )
    return list(
        CalendarEntry.objects.filter(school=school, date__range=(first, last))
        .select_related("stage_class")
        .order_by("date", "group", "pk")
    )


def groups_with_classes(school):
    """The set of groups that have at least one class."""
    return set(
        Class.objects.filter(school=school)
        .exclude(group="")
        .values_list("group", flat=True)
        .distinct()
    )


def entry_summary(entry):
    """Short Italian description of one calendar row."""
    parts = []
    if entry.stage_class_id:
        parts.append(f"stage della classe {entry.stage_class.name}")
    if entry.is_group_stage:
        parts.append("stage di gruppo")
    if entry.is_closure:
        parts.append("chiusura")
    if not entry.stage_class_id and not entry.is_group_stage and not entry.is_closure:
        parts.append(f"max {entry.max_hours} ore")
    if entry.start_hour_min is not None:
        parts.append(f"dalle {entry.start_hour_min}")
    return ", ".join(parts)


def entry_scope(entry):
    if entry.stage_class_id:
        return "Singola classe"
    return entry.get_group_display() if entry.group else "Tutta la scuola"


def _decorate(entry):
    entry.scope = entry_scope(entry)
    entry.day_text = day_label(entry.date)
    entry.summary = entry_summary(entry)
    entry.action_label = f"riga del {day_label(entry.date)} - {entry.scope}"
    return entry


def _cell(day, rows, groups):
    """One weekday cell; every hour comes from the pure resolvers."""
    lines = []
    for group in GROUP_ORDER:
        if group.value not in groups:
            lines.append({"label": group.label, "hours": None, "start": None})
            continue
        lines.append(
            {
                "label": group.label,
                "hours": day_rules.resolve_hours(rows, day, None, group.value),
                "start": day_rules.resolve_start_hour(rows, day, group.value),
            }
        )
    shown = [line["hours"] for line in lines if line["hours"] is not None]
    closure = any(r.is_closure for r in rows) and 0 in shown
    stage_classes = sorted(r.stage_class.name for r in rows if r.stage_class_id)
    stage = bool(stage_classes) or any(r.is_group_stage for r in rows)
    short = any(1 <= hours <= 5 for hours in shown)
    badges = []
    if closure:
        badges.append("Chiusura")
    if stage:
        badges.append("Stage")
    if short:
        badges.append("Giornata corta")
    kind = "closure" if closure else "stage" if stage else "short" if short else ""
    weekday = WEEKDAY_NAMES[day.weekday()]
    aria = f"{weekday} {day_label(day)}"
    if badges:
        aria += ": " + ", ".join(badges)
    return {
        "day": day,
        "lines": lines,
        "stage_classes": stage_classes,
        "badges": badges,
        "kind": kind,
        "aria_label": aria,
        "weekday": weekday,
        "entries": [_decorate(r) for r in rows],
    }


def month_view(school, first):
    """Everything the month screen shows: weeks of cells, weekend rows, nav."""
    entries = entries_of_month(school, first)
    groups = groups_with_classes(school)
    by_date = {}
    for entry in entries:
        by_date.setdefault(entry.date, []).append(entry)
    weeks = []
    for week in pycal.Calendar(firstweekday=0).monthdatescalendar(
        first.year, first.month
    ):
        cells = []
        for day in week[:5]:
            if day.month == first.month:
                cells.append(_cell(day, by_date.get(day, []), groups))
            else:
                cells.append(None)
        if any(cells):
            weeks.append(cells)
    weekend = [_decorate(e) for e in entries if e.date.weekday() >= 5]
    low, high = _first_of(school.year_start), _first_of(school.year_end)
    return {
        "month": first,
        "month_param": first.strftime("%Y-%m"),
        "title": month_title(first),
        "weeks": weeks,
        "weekdays": WEEKDAY_ABBR,
        "weekend_entries": weekend,
        "has_entries": bool(entries),
        "prev_month": _shift(first, -1).strftime("%Y-%m") if first > low else None,
        "next_month": _shift(first, 1).strftime("%Y-%m") if first < high else None,
    }

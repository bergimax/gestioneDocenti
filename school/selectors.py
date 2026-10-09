from django.db.models import Count, Q
from django.db.models.functions import Lower

from .assignment_hours import delivered_hours_map
from .models import Assignment, Class, ClassPairing, School, Subject, Teacher

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

from django.db.models import Count
from django.db.models.functions import Lower

from .models import School, Teacher

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

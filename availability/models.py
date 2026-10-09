from django.core.exceptions import ValidationError
from django.db import models

from school.models import School, Teacher

from . import grid as grid_logic


class Availability(models.Model):
    """A teacher's availability grid for one week (specs section 6).

    `slots` holds the `giorni_fasce` grid. `week_start` is the Monday.
    Same-school and grid checks are model-level: callers must call
    full_clean() (save_availability does).
    """

    school = models.ForeignKey(
        School, on_delete=models.PROTECT, related_name="availabilities"
    )
    teacher = models.ForeignKey(
        Teacher, on_delete=models.PROTECT, related_name="availabilities"
    )
    week_start = models.DateField("inizio settimana")
    slots = models.JSONField("giorni_fasce")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "availabilities"
        constraints = [
            models.UniqueConstraint(
                fields=["teacher", "week_start"],
                name="availability_unique_teacher_week",
            ),
        ]

    def __str__(self):
        return f"{self.teacher} - {self.week_start}"

    def clean(self):
        errors = {}
        if self.week_start is not None:
            try:
                grid_logic.check_monday(self.week_start)
            except ValueError:
                errors["week_start"] = "Deve essere un lunedi."
        problems = grid_logic.grid_errors(self.slots)
        if problems:
            errors["slots"] = problems
        if (
            self.teacher_id
            and self.school_id
            and self.teacher.school_id != self.school_id
        ):
            errors["teacher"] = "Deve appartenere alla stessa scuola."
        if errors:
            raise ValidationError(errors)

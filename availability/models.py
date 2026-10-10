from django.conf import settings
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


class AvailabilityLink(models.Model):
    """Magic link for one teacher and one week (architecture section 10).

    Only the SHA-256 hex digest of the token is stored; the token itself is
    shown once at creation and cannot be recomputed from any column.
    """

    teacher = models.ForeignKey(
        Teacher, on_delete=models.CASCADE, related_name="availability_links"
    )
    week_start = models.DateField("inizio settimana")
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField("scade il")
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["teacher", "week_start"],
                condition=models.Q(revoked_at__isnull=True),
                name="availabilitylink_one_active_per_teacher_week",
            ),
        ]

    def __str__(self):
        return f"{self.teacher} - {self.week_start}"


class LinkLookupFailure(models.Model):
    """One failed magic-link lookup, kept in Postgres for the rate limit."""

    client_address = models.CharField(max_length=64, db_index=True)
    attempted_at = models.DateTimeField(db_index=True)

    def __str__(self):
        return f"{self.client_address} {self.attempted_at}"

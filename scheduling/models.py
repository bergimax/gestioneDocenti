from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q

from availability.grid import check_monday
from school.models import Class, School, Subject, Teacher


class TimetableState(models.TextChoices):
    BOZZA = "BOZZA", "Bozza"
    APPROVATO = "APPROVATO", "Approvato"


class QualityLevel(models.TextChoices):
    A = "A", "A"
    B = "B", "B"
    C = "C", "C"


class Day(models.TextChoices):
    LUNEDI = "LUNEDI", "Lunedi"
    MARTEDI = "MARTEDI", "Martedi"
    MERCOLEDI = "MERCOLEDI", "Mercoledi"
    GIOVEDI = "GIOVEDI", "Giovedi"
    VENERDI = "VENERDI", "Venerdi"


class Timetable(models.Model):
    """Weekly timetable (specs section 3, `orario_settimanale`).

    `version` is the sync counter: it is bumped only by
    scheduling.services.apply_change. Model-level checks need full_clean().
    """

    school = models.ForeignKey(
        School, on_delete=models.PROTECT, related_name="timetables"
    )
    week_start = models.DateField("inizio settimana")
    state = models.CharField(
        max_length=10, choices=TimetableState.choices, default=TimetableState.BOZZA
    )
    quality_score = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    quality_level = models.CharField(  # noqa: DJ001 - issue: null until solved
        max_length=1, choices=QualityLevel.choices, null=True, blank=True
    )
    soft_conflicts = models.PositiveIntegerField(default=0)
    approved_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["school", "week_start"], name="timetable_unique_school_week"
            ),
            models.CheckConstraint(
                condition=Q(quality_score__isnull=True)
                | Q(quality_score__gte=0, quality_score__lte=100),
                name="timetable_quality_score_range",
            ),
        ]

    def __str__(self):
        return f"{self.school} - {self.week_start}"

    def clean(self):
        if self.week_start is not None:
            try:
                check_monday(self.week_start)
            except ValueError:
                raise ValidationError(
                    {"week_start": "Deve essere un lunedi."}
                ) from None


class Slot(models.Model):
    """One lesson of a timetable (specs section 3, `slot_lezione`).

    No uniqueness on class/teacher + day + hour: forced edits may double-book
    and are reported as conflicts.
    """

    timetable = models.ForeignKey(
        Timetable, on_delete=models.CASCADE, related_name="slots"
    )
    school_class = models.ForeignKey(
        Class, on_delete=models.PROTECT, related_name="slots"
    )
    teacher = models.ForeignKey(Teacher, on_delete=models.PROTECT, related_name="slots")
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="slots")
    day = models.CharField(max_length=10, choices=Day.choices)
    start_hour = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(8), MaxValueValidator(13)]
    )
    end_hour = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(9), MaxValueValidator(14)]
    )
    paired = models.BooleanField(default=False)
    paired_class = models.ForeignKey(
        Class,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="paired_slots",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(day__in=Day.values), name="slot_day_valid"
            ),
            models.CheckConstraint(
                condition=Q(start_hour__gte=8, start_hour__lte=13),
                name="slot_start_hour_range",
            ),
            models.CheckConstraint(
                condition=Q(end_hour__gte=9, end_hour__lte=14),
                name="slot_end_hour_range",
            ),
            models.CheckConstraint(
                condition=Q(end_hour=F("start_hour") + 1),
                name="slot_end_is_start_plus_one",
            ),
            models.CheckConstraint(
                condition=(Q(paired=True) & Q(paired_class__isnull=False))
                | (Q(paired=False) & Q(paired_class__isnull=True)),
                name="slot_paired_iff_paired_class",
            ),
            models.CheckConstraint(
                condition=~Q(paired_class=F("school_class")),
                name="slot_paired_class_differs",
            ),
        ]

    def __str__(self):
        return f"{self.school_class} {self.day} {self.start_hour}"

    def clean(self):
        errors = {}
        if (
            self.start_hour is not None
            and self.end_hour is not None
            and self.end_hour != self.start_hour + 1
        ):
            errors["end_hour"] = "Deve essere start_hour + 1."
        if self.paired and not self.paired_class_id:
            errors["paired_class"] = "Obbligatoria per una lezione accoppiata."
        elif not self.paired and self.paired_class_id:
            errors["paired_class"] = "Ammessa solo se la lezione e accoppiata."
        elif self.paired_class_id and self.paired_class_id == self.school_class_id:
            errors["paired_class"] = "Deve differire dalla classe."
        if self.timetable_id:
            school_id = self.timetable.school_id
            for name in ("school_class", "paired_class", "teacher", "subject"):
                obj = getattr(self, name) if getattr(self, f"{name}_id") else None
                if obj is not None and obj.school_id != school_id:
                    errors.setdefault(name, "Deve appartenere alla stessa scuola.")
        if errors:
            raise ValidationError(errors)

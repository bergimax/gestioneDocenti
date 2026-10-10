from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

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
    # The solver's conflict list (specs 9.8 dicts), replaced on every generation.
    conflicts = models.JSONField(default=list, blank=True)

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


class DecisionAction(models.TextChoices):
    APPROVATO = "APPROVATO", "Approvato"
    LIBERA = "LIBERA", "Ora lasciata libera"


class ConflictDecision(models.Model):
    """An admin decision on a conflict (specs 3, `conflitto_gestito`).

    APPROVATO: the conflict with this `chiave` is no longer reported.
    LIBERA: the lesson of a class/day/hour was rejected and the hour is left
    free on purpose; `chiave` is the one of the "hour not covered" conflict of
    that cell, so the empty hour is not reported either. All decisions of a
    timetable are deleted when the week is regenerated.
    """

    timetable = models.ForeignKey(
        Timetable, on_delete=models.CASCADE, related_name="conflict_decisions"
    )
    chiave = models.CharField(max_length=300)
    action = models.CharField(
        max_length=10, choices=DecisionAction.choices, default=DecisionAction.APPROVATO
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["timetable", "chiave"], name="conflictdecision_unique_key"
            ),
            models.CheckConstraint(
                condition=Q(action__in=DecisionAction.values),
                name="conflictdecision_action_valid",
            ),
        ]

    def __str__(self):
        return f"{self.chiave} ({self.action})"


class JobKind(models.TextChoices):
    GENERATE = "GENERATE", "Generazione"


class JobStatus(models.TextChoices):
    PENDING = "PENDING", "In attesa"
    RUNNING = "RUNNING", "In corso"
    DONE = "DONE", "Completato"
    FAILED = "FAILED", "Fallito"


ACTIVE_JOB_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING)
_ALLOWED_TRANSITIONS = {
    JobStatus.PENDING: {JobStatus.RUNNING},
    JobStatus.RUNNING: {JobStatus.DONE, JobStatus.FAILED},
}


class InvalidJobTransition(Exception):
    """A SolveJob status change other than PENDING -> RUNNING -> DONE/FAILED."""


class SolveJob(models.Model):
    """A background solve for one week (architecture section 4).

    Status moves only forward through `transition_to`: PENDING -> RUNNING ->
    DONE or FAILED. `result` is for the UI (message, score, counts); `error` is
    a human-readable Italian message, never a traceback.
    """

    school = models.ForeignKey(School, on_delete=models.PROTECT, related_name="jobs")
    week_start = models.DateField("inizio settimana")
    kind = models.CharField(
        max_length=10, choices=JobKind.choices, default=JobKind.GENERATE
    )
    status = models.CharField(
        max_length=10, choices=JobStatus.choices, default=JobStatus.PENDING
    )
    params = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    requested_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=JobStatus.values), name="solvejob_status_valid"
            ),
            models.CheckConstraint(
                condition=Q(kind__in=JobKind.values), name="solvejob_kind_valid"
            ),
            # One active job per week and kind, also under concurrent requests.
            models.UniqueConstraint(
                fields=["school", "week_start", "kind"],
                condition=Q(status__in=["PENDING", "RUNNING"]),
                name="solvejob_one_active_per_week",
            ),
        ]

    def __str__(self):
        return f"{self.kind} {self.week_start} {self.status}"

    def transition_to(self, status, *, error="", result=None):
        """Move to `status` atomically, setting started_at / finished_at.

        Raises InvalidJobTransition unless the stored status allows it.
        """
        if status not in _ALLOWED_TRANSITIONS.get(self.status, ()):
            raise InvalidJobTransition(f"{self.status} -> {status}")
        now = timezone.now()
        fields = {"status": status}
        if status == JobStatus.RUNNING:
            fields["started_at"] = now
        else:
            fields["finished_at"] = now
            fields["error"] = error
            if result is not None:
                fields["result"] = result
        # Conditional on the stored status: a concurrent change loses.
        changed = SolveJob.objects.filter(pk=self.pk, status=self.status).update(
            **fields
        )
        if not changed:
            raise InvalidJobTransition(f"{self.status} -> {status}")
        for name, value in fields.items():
            setattr(self, name, value)

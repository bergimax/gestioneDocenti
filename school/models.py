from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower


class Group(models.TextChoices):
    PRIME = "PRIME", "Prime"
    SECONDE = "SECONDE", "Seconde"
    TERZE = "TERZE", "Terze"
    QUARTE = "QUARTE", "Quarte"
    PRIMA4_2 = "PRIMA4+2", "Prima 4+2"


class TeacherType(models.TextChoices):
    ASSUNTO = "ASSUNTO", "Assunto"
    CONTRATTO = "CONTRATTO", "Contratto"


class SubjectKind(models.TextChoices):
    TEORIA = "TEORIA", "Teoria"
    PRATICA = "PRATICA", "Pratica"


class CognitiveWeight(models.TextChoices):
    ALTO = "ALTO", "Alto"
    MEDIO = "MEDIO", "Medio"
    BASSO = "BASSO", "Basso"


class School(models.Model):
    name = models.CharField("nome", max_length=255)
    formative_year = models.CharField("anno formativo", max_length=20)
    year_start = models.DateField("inizio anno")
    year_end = models.DateField("fine anno")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(year_end__gt=models.F("year_start")),
                name="school_school_year_order",
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        if self.year_start and self.year_end and self.year_end <= self.year_start:
            raise ValidationError(
                {"year_end": "La fine dell'anno deve essere dopo l'inizio."}
            )


class Class(models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="classes")
    name = models.CharField("nome", max_length=50)
    student_count = models.PositiveIntegerField("studenti", default=0)
    group = models.CharField(
        "gruppo", max_length=10, choices=Group.choices, blank=True, default=""
    )

    class Meta:
        verbose_name_plural = "classes"
        constraints = [
            models.UniqueConstraint(
                fields=["school", "name"], name="school_class_name_per_school"
            ),
        ]

    def __str__(self):
        return self.name


class Teacher(models.Model):
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="teachers"
    )
    name = models.CharField("cognome nome", max_length=255)
    email = models.EmailField("email", blank=True)
    type = models.CharField("tipo", max_length=10, choices=TeacherType.choices)
    active = models.BooleanField("attivo", default=True)
    first_time = models.BooleanField("primo anno", default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                "school", Lower("name"), name="school_teacher_name_ci_per_school"
            ),
        ]

    def __str__(self):
        return self.name


class Subject(models.Model):
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="subjects"
    )
    name = models.CharField("nome", max_length=255)
    kind = models.CharField("tipo", max_length=10, choices=SubjectKind.choices)
    cognitive_weight = models.CharField(
        "peso cognitivo", max_length=10, choices=CognitiveWeight.choices
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["school", "name"], name="school_subject_name_per_school"
            ),
        ]

    def __str__(self):
        return self.name


def _check_same_school(instance, **related):
    """Collect a field error for each related object from another school.

    Skips objects that are not set yet, so `full_clean()` still reports the
    required-field errors itself.
    """
    errors = {}
    for field, obj in related.items():
        if obj is not None and obj.school_id != instance.school_id:
            errors[field] = "Deve appartenere alla stessa scuola."
    return errors


class Assignment(models.Model):
    school = models.ForeignKey(
        School, on_delete=models.PROTECT, related_name="assignments"
    )
    teacher = models.ForeignKey(
        Teacher, on_delete=models.PROTECT, related_name="assignments"
    )
    school_class = models.ForeignKey(
        Class, on_delete=models.PROTECT, related_name="assignments"
    )
    subject = models.ForeignKey(
        Subject, on_delete=models.PROTECT, related_name="assignments"
    )
    ore_totali = models.PositiveIntegerField("ore totali")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["school", "teacher", "school_class", "subject"],
                name="school_assignment_unique_per_school",
            ),
        ]

    def __str__(self):
        return f"{self.teacher} - {self.school_class} - {self.subject}"

    def clean(self):
        """Teacher, class and subject must belong to `school`.

        Model-level only: PostgreSQL cannot compare columns across tables with
        plain FKs, so save()/objects.create()/bulk_create() without
        full_clean() can store a mismatched row. Callers must call full_clean().
        """
        errors = _check_same_school(
            self,
            teacher=self.teacher if self.teacher_id else None,
            school_class=self.school_class if self.school_class_id else None,
            subject=self.subject if self.subject_id else None,
        )
        if errors:
            raise ValidationError(errors)


class ClassPairing(models.Model):
    school = models.ForeignKey(
        School, on_delete=models.PROTECT, related_name="class_pairings"
    )
    class_a = models.ForeignKey(
        Class, on_delete=models.PROTECT, related_name="pairings_as_a"
    )
    class_b = models.ForeignKey(
        Class, on_delete=models.PROTECT, related_name="pairings_as_b"
    )
    subject = models.ForeignKey(
        Subject, on_delete=models.PROTECT, related_name="class_pairings"
    )
    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.PROTECT,
        related_name="class_pairings",
        null=True,
        blank=True,
    )
    note = models.TextField("note", blank=True)

    class Meta:
        constraints = [
            # Canonical order: also rejects a class paired with itself.
            models.CheckConstraint(
                condition=models.Q(class_a_id__lt=models.F("class_b_id")),
                name="school_classpairing_canonical_order",
            ),
            models.UniqueConstraint(
                fields=["school", "class_a", "class_b", "subject", "teacher"],
                name="school_classpairing_unique_per_school",
                nulls_distinct=False,
            ),
        ]

    def __str__(self):
        return f"{self.class_a} + {self.class_b} - {self.subject}"

    def clean(self):
        """Classes, subject and teacher (if set) must belong to `school`.

        Model-level only: PostgreSQL cannot compare columns across tables with
        plain FKs, so save()/objects.create()/bulk_create() without
        full_clean() can store a mismatched row. Callers must call full_clean().
        """
        errors = _check_same_school(
            self,
            class_a=self.class_a if self.class_a_id else None,
            class_b=self.class_b if self.class_b_id else None,
            subject=self.subject if self.subject_id else None,
            teacher=self.teacher if self.teacher_id else None,
        )
        if errors:
            raise ValidationError(errors)


class CalendarEntry(models.Model):
    school = models.ForeignKey(
        School, on_delete=models.PROTECT, related_name="calendar_entries"
    )
    date = models.DateField("data")
    # NULL (not "") means "whole school", enforced by a check constraint.
    group = models.CharField(  # noqa: DJ001
        "gruppo",
        max_length=10,
        choices=Group.choices,
        null=True,
        blank=True,
        default=None,
    )
    max_hours = models.IntegerField(
        "ore massime",
        default=6,
        validators=[MinValueValidator(0), MaxValueValidator(8)],
    )
    is_closure = models.BooleanField("chiusura", default=False)
    start_hour_min = models.IntegerField(
        "ora inizio minima",
        null=True,
        blank=True,
        validators=[MinValueValidator(8), MaxValueValidator(13)],
    )
    stage_class = models.ForeignKey(
        Class,
        on_delete=models.PROTECT,
        related_name="calendar_stage_entries",
        null=True,
        blank=True,
    )
    is_group_stage = models.BooleanField("stage di gruppo", default=False)

    class Meta:
        verbose_name_plural = "calendar entries"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(max_hours__gte=0, max_hours__lte=8),
                name="school_calendarentry_max_hours_range",
            ),
            models.CheckConstraint(
                condition=models.Q(start_hour_min__isnull=True)
                | models.Q(start_hour_min__gte=8, start_hour_min__lte=13),
                name="school_calendarentry_start_hour_range",
            ),
            # Only NULL means "whole school": the empty string is not allowed.
            models.CheckConstraint(
                condition=models.Q(group__isnull=True)
                | models.Q(group__in=Group.values),
                name="school_calendarentry_group_valid",
            ),
            models.CheckConstraint(
                condition=~models.Q(is_group_stage=True, group__isnull=True),
                name="school_calendarentry_group_stage_needs_group",
            ),
            models.CheckConstraint(
                condition=~models.Q(is_group_stage=True, stage_class__isnull=False),
                name="school_calendarentry_stage_class_xor_group_stage",
            ),
            # Class-stage rows are excluded: several classes can be on stage
            # on the same date, next to a whole-school row.
            models.UniqueConstraint(
                fields=["school", "date", "group"],
                condition=models.Q(stage_class__isnull=True, group__isnull=False),
                name="school_calendarentry_unique_group_per_date",
            ),
            models.UniqueConstraint(
                fields=["school", "date"],
                condition=models.Q(stage_class__isnull=True, group__isnull=True),
                name="school_calendarentry_unique_whole_school_per_date",
            ),
        ]

    def __str__(self):
        return f"{self.date} {self.group or 'tutta la scuola'}"

    def clean(self):
        """Row shape and same-school checks.

        The stage class must belong to `school`; model-level only, as for
        Assignment: callers must call full_clean().
        """
        errors = {}
        if self.group == "":
            errors["group"] = "Usare NULL per tutta la scuola, non una stringa vuota."
        if self.is_group_stage and self.group is None:
            errors["is_group_stage"] = "Lo stage di gruppo richiede un gruppo."
        if self.is_group_stage and self.stage_class_id:
            errors["stage_class"] = (
                "Stage di classe e stage di gruppo non possono coesistere."
            )
        errors.update(
            _check_same_school(
                self,
                stage_class=self.stage_class if self.stage_class_id else None,
            )
        )
        if errors:
            raise ValidationError(errors)


class ParkedUpload(models.Model):
    """A parsed setup file waiting in the database for the setup to be saved.

    One row per file type: a new upload of the same type replaces the old one.
    `rows` and `messages` are JSON (dates as ISO strings); only
    `school.setup_service` reads and writes them.
    """

    file_type = models.CharField(max_length=20, unique=True)
    source = models.CharField(max_length=10)
    filename = models.CharField(max_length=255)
    rows = models.JSONField(default=list)
    messages = models.JSONField(default=list)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.file_type}: {self.filename}"


class Draft(models.Model):
    """An AI-produced proposal, never authoritative until a person confirms it.

    `payload` is JSON (dates as ISO strings). The status moves DRAFT to
    CONFIRMED when the proposal is applied, or DISCARDED when it is replaced.
    """

    class Kind(models.TextChoices):
        CALENDAR_ROWS = "calendar_rows", "Righe di calendario"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Bozza"
        CONFIRMED = "CONFIRMED", "Confermata"
        DISCARDED = "DISCARDED", "Scartata"

    kind = models.CharField(max_length=30, choices=Kind.choices)
    payload = models.JSONField(default=list)
    source = models.CharField(max_length=255)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.kind} {self.status}: {self.source}"

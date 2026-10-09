from django.core.exceptions import ValidationError
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

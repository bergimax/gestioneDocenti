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

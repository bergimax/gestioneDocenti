from django import forms
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.models.functions import Lower

from .models import (
    Assignment,
    Class,
    ClassPairing,
    CognitiveWeight,
    Group,
    Subject,
    SubjectKind,
    Teacher,
    TeacherType,
)

NAME_ERRORS = {
    "required": "Inserisci il cognome e il nome.",
    "max_length": "Il nome può avere al massimo 255 caratteri.",
}


class TeacherForm(forms.ModelForm):
    """Teacher fields; the school is set by the server, never posted."""

    type = forms.ChoiceField(
        label="Tipo",
        choices=[("", "Scegli il tipo")] + list(TeacherType.choices),
        error_messages={
            "required": "Scegli il tipo di docente.",
            "invalid_choice": "Scegli un tipo valido.",
        },
    )

    class Meta:
        model = Teacher
        fields = ["name", "email", "type", "active", "first_time"]
        labels = {
            "name": "Cognome e nome",
            "email": "Email (facoltativa)",
            "active": "Attivo",
            "first_time": "Primo anno",
        }
        help_texts = {"name": "Prima il cognome, poi il nome. Esempio: ROSSI MARIO."}
        error_messages = {
            "name": NAME_ERRORS,
            "email": {"invalid": "Inserisci un indirizzo email valido."},
        }

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        self.instance.school = school
        for name, field in self.fields.items():
            if field.help_text:
                field.widget.attrs["aria-describedby"] = (
                    f"{self[name].id_for_label}-hint"
                )

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split()).upper()
        if len(name.split()) < 2:
            raise forms.ValidationError(
                "Inserisci cognome e nome, ad esempio ROSSI MARIO."
            )
        if (
            Teacher.objects.filter(school=self.school, name__iexact=name)
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError("Esiste già un docente con questo nome.")
        return name

    def full_clean(self):
        super().full_clean()
        self._link_errors()

    def _link_errors(self):
        """Link each invalid field to its error (and hint) for screen readers."""
        for name in self.errors:
            if name not in self.fields:
                continue
            widget = self.fields[name].widget
            field_id = self[name].id_for_label
            ids = [f"{field_id}-error"]
            if self.fields[name].help_text:
                ids.insert(0, f"{field_id}-hint")
            widget.attrs["aria-describedby"] = " ".join(ids)
            widget.attrs["aria-invalid"] = "true"

    def save_or_report(self):
        """Save; a lost uniqueness race becomes the same field error."""
        try:
            with transaction.atomic():
                return self.save()
        except IntegrityError:
            self.add_error("name", "Esiste già un docente con questo nome.")
            self._link_errors()
            return None


class _NamedSchoolForm(forms.ModelForm):
    """Shared behaviour of the class and subject forms.

    The school is set by the server, never posted. Names are trimmed and
    collapsed but keep their case; uniqueness ignores case within the school.
    """

    duplicate_message = ""

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        self.instance.school = school
        for name, field in self.fields.items():
            if field.help_text:
                field.widget.attrs["aria-describedby"] = (
                    f"{self[name].id_for_label}-hint"
                )

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if (
            self._meta.model.objects.filter(school=self.school, name__iexact=name)
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError(self.duplicate_message)
        return name

    def full_clean(self):
        super().full_clean()
        self._link_errors()

    def _link_errors(self):
        """Link each invalid field to its error (and hint) for screen readers."""
        for name in self.errors:
            if name not in self.fields:
                continue
            widget = self.fields[name].widget
            field_id = self[name].id_for_label
            ids = [f"{field_id}-error"]
            if self.fields[name].help_text:
                ids.insert(0, f"{field_id}-hint")
            widget.attrs["aria-describedby"] = " ".join(ids)
            widget.attrs["aria-invalid"] = "true"

    def save_or_report(self):
        """Save; a lost uniqueness race becomes the same field error."""
        try:
            with transaction.atomic():
                return self.save()
        except IntegrityError:
            self.add_error("name", self.duplicate_message)
            self._link_errors()
            return None


class ClassForm(_NamedSchoolForm):
    duplicate_message = "Esiste già una classe con questo nome."

    student_count = forms.IntegerField(
        label="Numero di studenti",
        required=False,
        min_value=0,
        initial=0,
        error_messages={
            "invalid": "Inserisci un numero intero.",
            "min_value": "Il numero di studenti non può essere negativo.",
        },
    )
    group = forms.ChoiceField(
        label="Gruppo (facoltativo)",
        required=False,
        choices=[("", "Nessun gruppo")] + list(Group.choices),
        error_messages={"invalid_choice": "Scegli un gruppo valido."},
    )

    class Meta:
        model = Class
        fields = ["name", "student_count", "group"]
        labels = {"name": "Nome della classe"}
        help_texts = {"name": "Esempio: II ELETTRICISTI."}
        error_messages = {
            "name": {
                "required": "Inserisci il nome della classe.",
                "max_length": "Il nome può avere al massimo 50 caratteri.",
            }
        }

    def clean_student_count(self):
        value = self.cleaned_data["student_count"]
        return 0 if value is None else value


class SubjectForm(_NamedSchoolForm):
    duplicate_message = "Esiste già una materia con questo nome."

    kind = forms.ChoiceField(
        label="Tipo",
        choices=[("", "Scegli il tipo")] + list(SubjectKind.choices),
        error_messages={
            "required": "Scegli il tipo di materia.",
            "invalid_choice": "Scegli un tipo valido.",
        },
    )
    cognitive_weight = forms.ChoiceField(
        label="Peso cognitivo",
        choices=[("", "Scegli il peso")] + list(CognitiveWeight.choices),
        error_messages={
            "required": "Scegli il peso cognitivo.",
            "invalid_choice": "Scegli un peso valido.",
        },
    )

    class Meta:
        model = Subject
        fields = ["name", "kind", "cognitive_weight"]
        labels = {"name": "Nome della materia"}
        error_messages = {
            "name": {
                "required": "Inserisci il nome della materia.",
                "max_length": "Il nome può avere al massimo 255 caratteri.",
            }
        }


MAX_HOURS = 9999


class _AriaForm:
    """Mixin: hints and errors are linked to their field for screen readers."""

    def _init_hints(self):
        for name, field in self.fields.items():
            if field.help_text:
                field.widget.attrs["aria-describedby"] = (
                    f"{self[name].id_for_label}-hint"
                )

    def full_clean(self):
        super().full_clean()
        self._link_errors()

    def _link_errors(self):
        for name in self.errors:
            if name not in self.fields:
                continue
            widget = self.fields[name].widget
            field_id = self[name].id_for_label
            ids = [f"{field_id}-error"]
            if self.fields[name].help_text:
                ids.insert(0, f"{field_id}-hint")
            widget.attrs["aria-describedby"] = " ".join(ids)
            widget.attrs["aria-invalid"] = "true"


def _hours_field():
    return forms.IntegerField(
        label="Ore totali",
        min_value=0,
        max_value=MAX_HOURS,
        help_text=f"Numero intero da 0 a {MAX_HOURS}.",
        error_messages={
            "required": "Inserisci le ore totali.",
            "invalid": "Inserisci un numero intero.",
            "min_value": "Le ore totali non possono essere negative.",
            "max_value": f"Le ore totali non possono superare {MAX_HOURS}.",
        },
    )


def _choice_errors(what):
    return {
        "required": f"Scegli {what}.",
        "invalid_choice": f"Scegli {what} dell'elenco: scelta non valida.",
    }


def _by_name(model, school):
    return model.objects.filter(school=school).order_by(Lower("name"), "pk")


class AssignmentForm(_AriaForm, forms.ModelForm):
    """Create an assignment; the school is set by the server, never posted."""

    duplicate_template = (
        "Esiste già un'assegnazione di {teacher} per {klass}, {subject}."
    )

    ore_totali = _hours_field()

    class Meta:
        model = Assignment
        fields = ["teacher", "school_class", "subject", "ore_totali"]
        labels = {"teacher": "Docente", "school_class": "Classe", "subject": "Materia"}
        error_messages = {
            "teacher": _choice_errors("un docente"),
            "school_class": _choice_errors("una classe"),
            "subject": _choice_errors("una materia"),
        }

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        self.instance.school = school
        self.fields["teacher"].queryset = _by_name(Teacher, school).filter(active=True)
        self.fields["teacher"].empty_label = "Scegli il docente"
        self.fields["school_class"].queryset = _by_name(Class, school)
        self.fields["school_class"].empty_label = "Scegli la classe"
        self.fields["subject"].queryset = _by_name(Subject, school)
        self.fields["subject"].empty_label = "Scegli la materia"
        self._init_hints()

    def _duplicate_message(self, teacher, klass, subject):
        return self.duplicate_template.format(
            teacher=teacher, klass=klass, subject=subject
        )

    def clean(self):
        data = super().clean()
        teacher, klass, subject = (
            data.get("teacher"),
            data.get("school_class"),
            data.get("subject"),
        )
        if (
            teacher
            and klass
            and subject
            and Assignment.objects.filter(
                school=self.school,
                teacher=teacher,
                school_class=klass,
                subject=subject,
            ).exists()
        ):
            self.add_error("teacher", self._duplicate_message(teacher, klass, subject))
        return data

    def save_or_report(self):
        """Save; a lost uniqueness race becomes the same field error."""
        try:
            with transaction.atomic():
                return self.save()
        except IntegrityError:
            data = self.cleaned_data
            self.add_error(
                "teacher",
                self._duplicate_message(
                    data["teacher"], data["school_class"], data["subject"]
                ),
            )
            self._link_errors()
            return None


class AssignmentHoursForm(_AriaForm, forms.Form):
    """Edit an assignment: the total hours only."""

    ore_totali = _hours_field()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_hints()


class PairingForm(_AriaForm, forms.ModelForm):
    """Create or edit a pairing; the two classes may be picked in any order."""

    class Meta:
        model = ClassPairing
        fields = ["class_a", "class_b", "subject", "teacher", "note"]
        labels = {
            "class_a": "Classe",
            "class_b": "Seconda classe",
            "subject": "Materia",
            "teacher": "Docente (facoltativo)",
            "note": "Note (facoltative)",
        }
        error_messages = {
            "class_a": _choice_errors("una classe"),
            "class_b": _choice_errors("la seconda classe"),
            "subject": _choice_errors("una materia"),
            "teacher": {
                "invalid_choice": "Scegli un docente dell'elenco: scelta non valida."
            },
        }

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        self.instance.school = school
        classes = _by_name(Class, school)
        self.fields["class_a"].queryset = classes
        self.fields["class_a"].empty_label = "Scegli la classe"
        self.fields["class_b"].queryset = classes
        self.fields["class_b"].empty_label = "Scegli la seconda classe"
        self.fields["subject"].queryset = _by_name(Subject, school)
        self.fields["subject"].empty_label = "Scegli la materia"
        teachers = Q(active=True)
        if self.instance.teacher_id:
            teachers |= Q(pk=self.instance.teacher_id)
        self.fields["teacher"].queryset = _by_name(Teacher, school).filter(teachers)
        self.fields["teacher"].empty_label = "Nessun docente"
        self._init_hints()

    def clean(self):
        data = super().clean()
        a, b = data.get("class_a"), data.get("class_b")
        subject, teacher = data.get("subject"), data.get("teacher")
        if a and b and a.pk == b.pk:
            self.add_error("class_b", "Scegli due classi diverse.")
            return data
        if subject:
            for field, klass in (("class_a", a), ("class_b", b)):
                if klass is None:
                    continue
                if not self._has_assignment(subject, klass):
                    self.add_error(
                        field,
                        f"La classe {klass} non ha un'assegnazione per {subject}.",
                    )
                elif teacher and not self._has_assignment(subject, klass, teacher):
                    self.add_error(
                        "teacher",
                        f"{teacher} non ha un'assegnazione per {subject} in {klass}.",
                    )
        if a and b and a.pk > b.pk:
            # Canonical order: the lower id is class_a.
            data["class_a"], data["class_b"] = b, a
            a, b = b, a
        if a and b and subject and not self.errors:
            if self._duplicates(a, b, subject, teacher).exists():
                self.add_error("subject", self.duplicate_message)
        return data

    duplicate_message = (
        "Esiste già un accoppiamento per queste classi, materia e docente."
    )

    def _has_assignment(self, subject, klass, teacher=None):
        qs = Assignment.objects.filter(
            school=self.school, subject=subject, school_class=klass
        )
        if teacher is not None:
            qs = qs.filter(teacher=teacher)
        return qs.exists()

    def _duplicates(self, a, b, subject, teacher):
        return ClassPairing.objects.filter(
            school=self.school,
            class_a=a,
            class_b=b,
            subject=subject,
            teacher=teacher,
        ).exclude(pk=self.instance.pk)

    def save_or_report(self):
        """Save; a lost uniqueness race becomes the same field error."""
        try:
            with transaction.atomic():
                return self.save()
        except IntegrityError:
            self.add_error("subject", self.duplicate_message)
            self._link_errors()
            return None

from django import forms
from django.db import IntegrityError, transaction

from .models import Teacher, TeacherType

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

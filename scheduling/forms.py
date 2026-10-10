import re

from django import forms

from school.models import Class, Teacher, TeacherType

from .derogations import (
    CLASSE,
    DOCENTE,
    GIORNO,
    MAX_HOURS_RANGE,
    MAX_ORE,
    QUICK_ACTIONS,
    REDUCE_CONTRACT_HOURS,
    applies,
    label_of,
)
from .models import Day

MSG_MAX_HOURS = (
    f"Il massimo di ore deve essere un numero intero da {MAX_HOURS_RANGE[0]} "
    f"a {MAX_HOURS_RANGE[1]}."
)
MSG_REDUCE_ASSUNTO = "La riduzione delle ore si applica solo ai docenti a contratto."
_INTEGER = re.compile(r"[0-9]{1,6}")


class QuickActionForm(forms.Form):
    azione = forms.ChoiceField(
        label="Tipo di deroga",
        choices=[("", "Scegli una deroga")]
        + [(key, label_of(key)) for key in QUICK_ACTIONS],
        error_messages={
            "required": "Scegli il tipo di deroga.",
            "invalid_choice": "Tipo di deroga non valido.",
        },
    )
    classe = forms.ModelChoiceField(
        Class.objects.none(),
        required=False,
        label="Classe",
        empty_label="Tutte le classi",
        error_messages={"invalid_choice": "La classe indicata non esiste."},
    )
    docente = forms.ModelChoiceField(
        Teacher.objects.none(),
        required=False,
        label="Docente",
        empty_label="Tutti i docenti",
        error_messages={"invalid_choice": "Il docente indicato non esiste."},
    )
    giorno = forms.ChoiceField(
        required=False,
        label="Giorno",
        choices=[("", "Tutta la settimana")]
        + [(value, label) for value, label in Day.choices],
        error_messages={
            "invalid_choice": "Giorno non valido: sono ammessi solo i giorni feriali."
        },
    )
    max_ore = forms.CharField(required=False, label="Massimo di ore")

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        # Alpine disables the controls of the parameters that do not apply, so
        # they are not submitted (the server drops them anyway).
        for name in (CLASSE, DOCENTE, GIORNO, MAX_ORE):
            self.fields[name].widget.attrs["x-bind:disabled"] = f"!shows('{name}')"
        self.fields[MAX_ORE].widget.attrs["inputmode"] = "numeric"
        self.fields[MAX_ORE].widget.attrs["autocomplete"] = "off"
        self.fields[CLASSE].queryset = Class.objects.filter(school=school).order_by(
            "name", "pk"
        )
        self.fields[DOCENTE].queryset = Teacher.objects.filter(school=school).order_by(
            "name", "pk"
        )

    def clean_max_ore(self):
        raw = self.cleaned_data[MAX_ORE]
        if raw == "":
            return None
        value = int(raw) if _INTEGER.fullmatch(raw) else 0
        if not MAX_HOURS_RANGE[0] <= value <= MAX_HOURS_RANGE[1]:
            raise forms.ValidationError(MSG_MAX_HOURS)
        return value

    def clean(self):
        data = super().clean()
        action = data.get("azione")
        teacher = data.get(DOCENTE)
        if (
            action == REDUCE_CONTRACT_HOURS
            and teacher is not None
            and teacher.type != TeacherType.CONTRATTO
        ):
            self.add_error(DOCENTE, MSG_REDUCE_ASSUNTO)
        return data

    def params(self):
        """Params of the valid submission, without those the type ignores."""
        data = self.cleaned_data
        action = data["azione"]
        school_class, teacher = data.get(CLASSE), data.get(DOCENTE)
        return {
            "action_type": action,
            "class_id": school_class.pk
            if school_class and applies(action, CLASSE)
            else None,
            "teacher_id": teacher.pk if teacher and applies(action, DOCENTE) else None,
            "day": (data.get(GIORNO) or None) if applies(action, GIORNO) else None,
            "max_hours": data.get(MAX_ORE) if applies(action, MAX_ORE) else None,
        }

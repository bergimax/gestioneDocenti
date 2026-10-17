import re

from django import forms

from school.models import Class, Teacher, TeacherType

from . import repair
from .derogations import (
    CLASSE,
    DOCENTE,
    GIORNO,
    MAX_HOURS_RANGE,
    MAX_ORE,
    NEEDS_TEACHER_AND_DAY,
    QUICK_ACTIONS,
    REDUCE_CONTRACT_HOURS,
    applies,
    label_of,
)
from .models import Day, Slot

MSG_MAX_HOURS = (
    f"Il massimo di ore deve essere un numero intero da {MAX_HOURS_RANGE[0]} "
    f"a {MAX_HOURS_RANGE[1]}."
)
MSG_REDUCE_ASSUNTO = "La riduzione delle ore si applica solo ai docenti a contratto."
MSG_NEEDS_TEACHER = "Scegli il docente."
MSG_NEEDS_DAY = "Scegli il giorno."
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
        if action in NEEDS_TEACHER_AND_DAY:
            if teacher is None and not self.has_error(DOCENTE):
                self.add_error(DOCENTE, MSG_NEEDS_TEACHER)
            if not data.get(GIORNO) and not self.has_error(GIORNO):
                self.add_error(GIORNO, MSG_NEEDS_DAY)
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


MSG_NOTHING_TO_REPAIR = (
    "Niente da riparare: il docente non ha lezioni in questa settimana."
)
MSG_BAD_DAY = "Giorno non valido: sono ammessi solo i giorni feriali."
MSG_NO_SUCH_SLOT = "Non c'è nessuna lezione della classe in quel giorno e a quell'ora."
MSG_BAD_HOUR = "L'ora deve essere compresa tra 8 e 13."


class RepairForm(forms.Form):
    """The "Sostituzione" form: the kind of perturbation and its parameters."""

    tipo = forms.ChoiceField(
        label="Tipo di sostituzione",
        choices=[("", "Scegli il tipo")]
        + [(key, label) for key, label in repair.KIND_LABELS.items()],
        error_messages={
            "required": "Scegli il tipo di sostituzione.",
            "invalid_choice": "Tipo di sostituzione non valido.",
        },
    )
    docente = forms.ModelChoiceField(
        Teacher.objects.none(),
        required=False,
        label="Docente",
        empty_label="Scegli il docente",
        error_messages={"invalid_choice": "Il docente indicato non esiste."},
    )
    classe = forms.ModelChoiceField(
        Class.objects.none(),
        required=False,
        label="Classe",
        empty_label="Scegli la classe",
        error_messages={"invalid_choice": "La classe indicata non esiste."},
    )
    giorni = forms.MultipleChoiceField(
        required=False,
        label="Giorni di assenza",
        choices=Day.choices,
        widget=forms.CheckboxSelectMultiple,
        error_messages={"invalid_choice": MSG_BAD_DAY},
    )
    giorno = forms.ChoiceField(
        required=False,
        label="Giorno",
        choices=[("", "Scegli il giorno")] + list(Day.choices),
        error_messages={"invalid_choice": MSG_BAD_DAY},
    )
    ora = forms.CharField(required=False, label="Ora di inizio (8-13)")

    NEEDS = {
        repair.TEACHER_ABSENT: ("docente", "giorni"),
        repair.SLOT_REMOVED: ("classe", "giorno", "ora"),
        repair.CLASS_CLOSED: ("classe", "giorno"),
    }
    REQUIRED = {
        "docente": "Scegli il docente.",
        "classe": "Scegli la classe.",
        "giorni": "Scegli almeno un giorno.",
        "giorno": "Scegli il giorno.",
        "ora": "Indica l'ora di inizio.",
    }

    def __init__(self, *args, school, timetable=None, **kwargs):
        kwargs.setdefault("auto_id", "id_sost_%s")  # the Deroga form has its own ids
        super().__init__(*args, **kwargs)
        self.timetable = timetable
        for name in ("docente", "classe", "giorni", "giorno", "ora"):
            self.fields[name].widget.attrs["x-bind:disabled"] = f"!shows('{name}')"
        self.fields["ora"].widget.attrs["inputmode"] = "numeric"
        self.fields["ora"].widget.attrs["autocomplete"] = "off"
        self.fields["docente"].queryset = Teacher.objects.filter(
            school=school
        ).order_by("name", "pk")
        self.fields["classe"].queryset = Class.objects.filter(school=school).order_by(
            "name", "pk"
        )

    def clean(self):
        data = super().clean()
        kind = data.get("tipo")
        if kind not in self.NEEDS:
            return data
        if self.timetable is None:
            raise forms.ValidationError(repair.ERROR_NO_TIMETABLE)
        for name in self.NEEDS[kind]:
            if name not in self.errors and not data.get(name):
                self.add_error(name, self.REQUIRED[name])
        if self.errors:
            return data
        slots = Slot.objects.filter(timetable=self.timetable)
        if kind == repair.TEACHER_ABSENT:
            if not slots.filter(teacher=data["docente"]).exists():
                self.add_error("docente", MSG_NOTHING_TO_REPAIR)
        elif kind == repair.SLOT_REMOVED:
            hour = self._hour(data["ora"])
            if hour is None:
                self.add_error("ora", MSG_BAD_HOUR)
            elif not (
                slots.filter(
                    day=data["giorno"], start_hour=hour, school_class=data["classe"]
                ).exists()
                or slots.filter(
                    day=data["giorno"],
                    start_hour=hour,
                    paired_class=data["classe"],
                ).exists()
            ):
                self.add_error("ora", MSG_NO_SUCH_SLOT)
        return data

    @staticmethod
    def _hour(raw):
        if not re.fullmatch(r"[0-9]{1,2}", raw or ""):
            return None
        value = int(raw)
        return value if 8 <= value <= 13 else None

    def params(self):
        """The validated perturbation, as stored in the job's params."""
        data = self.cleaned_data
        kind = data["tipo"]
        teacher, school_class = data.get("docente"), data.get("classe")
        days = (
            sorted(data["giorni"], key=Day.values.index)
            if kind == repair.TEACHER_ABSENT
            else [data["giorno"]]
        )
        return {
            "kind": kind,
            "teacher_id": teacher.pk if kind == repair.TEACHER_ABSENT else None,
            "class_id": None if kind == repair.TEACHER_ABSENT else school_class.pk,
            "days": days,
            "hour": self._hour(data["ora"]) if kind == repair.SLOT_REMOVED else None,
        }

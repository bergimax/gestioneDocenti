"""The eight quick actions (specs 9.7): catalogue, scope wording, solver mapping.

A derogation lives in `SolveJob.params["quick_action"]` of the one regeneration
it was requested for and nowhere else.
"""

from .models import Day
from .solver.context import (
    AUTHORIZE_EARLY_EXIT,
    AUTHORIZE_FRIDAY_LATE_START,
    AUTHORIZE_SHORT_PRATICA_BLOCK,
    AUTHORIZE_SINGLE_CLASSE_DAY,
    EXCLUDE_DAY,
    FORCE_3_HOURS_THEORY,
    OVERRIDE_AVAILABILITY,
    Derogation,
)

REDUCE_CONTRACT_HOURS = "reduce_contract_hours"
PARAMS_KEY = "quick_action"
MAX_HOURS_RANGE = (1, 30)

CLASSE, DOCENTE, GIORNO, MAX_ORE = "classe", "docente", "giorno", "max_ore"

# action_type -> (label, the parameters that apply to it)
QUICK_ACTIONS = {
    FORCE_3_HOURS_THEORY: (
        "Consenti 3 ore di teoria consecutive",
        (CLASSE, DOCENTE, GIORNO, MAX_ORE),
    ),
    REDUCE_CONTRACT_HOURS: (
        "Riduci le ore del contrattista",
        (CLASSE, DOCENTE, MAX_ORE),
    ),
    AUTHORIZE_EARLY_EXIT: (
        "Autorizza l'uscita anticipata",
        (CLASSE, DOCENTE, GIORNO),
    ),
    OVERRIDE_AVAILABILITY: ("Ignora la disponibilità", (CLASSE, DOCENTE, GIORNO)),
    AUTHORIZE_SINGLE_CLASSE_DAY: (
        "Autorizza la giornata con una sola classe",
        (CLASSE, DOCENTE, GIORNO),
    ),
    AUTHORIZE_FRIDAY_LATE_START: (
        "Autorizza l'inizio tardivo del venerdì",
        (CLASSE, DOCENTE),
    ),
    AUTHORIZE_SHORT_PRATICA_BLOCK: (
        "Autorizza un blocco di pratica breve",
        (CLASSE, DOCENTE, GIORNO),
    ),
    EXCLUDE_DAY: ("Escludi il docente da un giorno", (DOCENTE, GIORNO)),
}

# Actions that need a teacher and a day (no "all teachers", no "whole week").
NEEDS_TEACHER_AND_DAY = frozenset({EXCLUDE_DAY})

DAY_WORDS = {
    "LUNEDI": "lunedì",
    "MARTEDI": "martedì",
    "MERCOLEDI": "mercoledì",
    "GIOVEDI": "giovedì",
    "VENERDI": "venerdì",
}
_DAYS = list(Day.values)


def label_of(action_type):
    return QUICK_ACTIONS[action_type][0]


def applies(action_type, field):
    return field in QUICK_ACTIONS[action_type][1]


def derogation_from_params(params):
    """The solver `Derogation` of a job's params, or None for a plain job."""
    data = (params or {}).get(PARAMS_KEY)
    if not data:
        return None
    day = data.get("day")
    return Derogation(
        action_type=data["action_type"],
        class_id=data.get("class_id"),
        teacher_id=data.get("teacher_id"),
        day=None if day is None else _DAYS.index(day),
        max_hours=data.get("max_hours"),
    )


def scope_words(params, class_name=None, teacher_name=None):
    """The scope of a quick action in words, e.g. "classe 1A, lunedì"."""
    parts = []
    if params.get("class_id") is not None:
        parts.append(f"classe {class_name}")
    if params.get("teacher_id") is not None:
        parts.append(f"docente {teacher_name}")
    if params.get("day") is not None:
        parts.append(DAY_WORDS[params["day"]])
    return ", ".join(parts) if parts else "tutta la settimana"

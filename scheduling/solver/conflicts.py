"""Conflict list: the violated penalties of a solution (specs 9.8)."""

from __future__ import annotations

import uuid

from .context import (
    AUTHORIZE_EARLY_EXIT,
    AUTHORIZE_FRIDAY_LATE_START,
    AUTHORIZE_SHORT_PRATICA_BLOCK,
    AUTHORIZE_SINGLE_CLASSE_DAY,
    CLASSE_DOUBLE_BOOKING,
    CLASSE_EARLY_START,
    CLASSE_LATE_START,
    CLASSE_OVER_CAPACITY,
    CLASSE_STAGE_DAY,
    CLASSE_UNFILLED,
    CONTRACTOR_GAP,
    CONTRATTO,
    DOCENTE_DOUBLE_BOOKING,
    FIRST_HOUR,
    FORCE_3_HOURS_THEORY,
    MONTE_ORE_EXCEEDED,
    ORE_TARGET_DEVIATION,
    OVERRIDE_AVAILABILITY,
    SINGLE_CLASSE_DAY,
    TEORIA_CONSECUTIVE,
    Conflict,
    Lesson,
    Penalty,
    ScheduleContext,
)

REDUCE_CONTRACT_HOURS = "reduce_contract_hours"
MANUAL_EDIT = "manual_edit"

_DAY_NAMES = ["LUNEDI", "MARTEDI", "MERCOLEDI", "GIOVEDI", "VENERDI"]
_DAY_LABELS = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì"]

# kind -> (action_type, label) of the suggested action.
_ACTIONS = {
    "H4": (OVERRIDE_AVAILABILITY, "Ignora la disponibilità"),
    "H9": (AUTHORIZE_FRIDAY_LATE_START, "Autorizza l'inizio tardivo del venerdì"),
    "H10": (AUTHORIZE_SHORT_PRATICA_BLOCK, "Autorizza un blocco di pratica breve"),
    "H11": (AUTHORIZE_SINGLE_CLASSE_DAY, "Autorizza la giornata con una sola classe"),
    TEORIA_CONSECUTIVE: (FORCE_3_HOURS_THEORY, "Consenti 3 ore di teoria consecutive"),
    CONTRACTOR_GAP: (AUTHORIZE_EARLY_EXIT, "Autorizza l'uscita anticipata"),
    SINGLE_CLASSE_DAY: (
        AUTHORIZE_SINGLE_CLASSE_DAY,
        "Autorizza la giornata con una sola classe",
    ),
    CLASSE_UNFILLED: ("assign_teacher", "Assegna un docente alla classe"),
}
_MANUAL = (MANUAL_EDIT, "Modifica l'orario a mano")


def day_key(day: int | None) -> str:
    return "SETTIMANA" if day is None else _DAY_NAMES[day]


def chiave(kind, class_id, teacher_id, day, hour) -> str:
    """Stable key ``<kind>|<classe_id>|<docente_id>|<GIORNO>[|<ora>]``."""
    parts = [
        kind,
        "" if class_id is None else str(class_id),
        "" if teacher_id is None else str(teacher_id),
        day_key(day),
    ]
    if hour is not None:
        parts.append(str(hour))
    return "|".join(parts)


def _clock(hour: int) -> str:
    return f"{FIRST_HOUR + hour:02d}:00"


def _describe(p: Penalty, quasi: dict, ctx, lessons, assigned) -> str:
    where = "" if p.day is None else f" {_DAY_LABELS[p.day]}"
    at = "" if p.hour is None else f" alle {_clock(p.hour)}"
    c, t = p.class_id, p.teacher_id
    q = quasi.get((p.kind, c, t, p.day, p.hour))
    if q:
        return "; ".join(dict.fromkeys(x.description for x in q))
    if p.kind == CLASSE_UNFILLED:
        if p.hour is None:
            return f"{p.value} ore non coperte per la classe {c}{where}"
        return f"Ora non coperta per la classe {c}{where}{at}: nessun docente"
    if p.kind == CLASSE_OVER_CAPACITY:
        return f"La classe {c}{where}{at} ha più lezioni della capienza del giorno"
    if p.kind == CLASSE_DOUBLE_BOOKING:
        return f"La classe {c}{where}{at} ha due lezioni nella stessa ora"
    if p.kind == DOCENTE_DOUBLE_BOOKING:
        return f"Il docente {t}{where}{at} è in due classi nella stessa ora"
    if p.kind == MONTE_ORE_EXCEEDED:
        return (
            f"Il docente {t} supera il monte ore con la classe {c}{where}{at} "
            f"(1 ora oltre il residuo)"
        )
    if p.kind == CLASSE_EARLY_START:
        return f"La classe {c}{where}{at} ha una lezione prima dell'ora di inizio"
    if p.kind == CLASSE_STAGE_DAY:
        return f"La classe {c}{where}{at} ha una lezione in un giorno di stage"
    if p.kind == ORE_TARGET_DEVIATION:
        got = assigned.get(p.assignment_id, 0)
        target = ctx.targets.get(p.assignment_id, got)
        source = ctx.target_sources.get(p.assignment_id, "target fornito dal contesto")
        return (
            f"Il docente {t} con la classe {c}: {got} ore assegnate, target "
            f"{target}, scarto {got - target:+d} (target derivato da: {source})"
        )
    if p.kind == CLASSE_LATE_START:
        return f"La classe {c}{where} non inizia alle 08:00"
    if p.kind == TEORIA_CONSECUTIVE:
        return (
            f"Troppe ore di teoria consecutive del docente {t} con la classe {c}{where}"
        )
    if p.kind == CONTRACTOR_GAP:
        return f"Il contrattista {t}{where} ha un'ora buca tra due lezioni"
    if p.kind == SINGLE_CLASSE_DAY:
        return f"Il docente {t} passa la giornata{where} con la sola classe {c}"
    return f"{p.kind}: classe {c}, docente {t}{where}{at}"


def _action(p: Penalty, ctx: ScheduleContext, assigned) -> tuple[str, str]:
    if p.kind == ORE_TARGET_DEVIATION:
        got = assigned.get(p.assignment_id, 0)
        above = got > ctx.targets.get(p.assignment_id, got)
        if above and ctx.teacher_types.get(p.teacher_id) == CONTRATTO:
            return (REDUCE_CONTRACT_HOURS, "Riduci ore")
        return _MANUAL
    return _ACTIONS.get(p.kind, _MANUAL)


def build_conflict_items(
    violations: list[Penalty],
    conflicts: list[Conflict],
    ctx: ScheduleContext,
    lessons: list[Lesson],
) -> list[dict]:
    """One dict per distinct (kind, class, teacher, day, hour), in a stable order.

    ``chiave`` depends only on that tuple, so it survives recomputation and the
    move of an unrelated lesson; ``conflict_id`` is fresh on every call.
    ``ore`` (hours involved) appears for penalties that count hours without a
    single slot; ``ore_slot`` (hour index 0-5) for those tied to one slot.
    """
    quasi: dict[tuple, list[Conflict]] = {}
    for c in conflicts:
        quasi.setdefault((c.kind, c.class_id, c.teacher_id, c.day, c.hour), []).append(
            c
        )
    assigned: dict = {}
    for le in lessons:
        assigned[le.assignment_id] = assigned.get(le.assignment_id, 0) + 1

    grouped: dict[tuple, dict] = {}
    for p in violations:
        key = (p.kind, p.class_id, p.teacher_id, p.day, p.hour)
        desc = _describe(p, quasi, ctx, lessons, assigned)
        item = grouped.get(key)
        if item is None:
            action_type, label = _action(p, ctx, assigned)
            item = {
                "kind": p.kind,
                "chiave": chiave(*key),
                "description": desc,
                "suggested_action": {"action_type": action_type, "label": label},
                "classe_id": p.class_id,
                "docente_id": p.teacher_id,
                "giorno": None if p.day is None else _DAY_NAMES[p.day],
                "weight": p.weight,
                "_value": 0,
            }
            if p.hour is not None:
                item["ore_slot"] = p.hour
            grouped[key] = item
        elif desc not in item["description"]:
            item["description"] += "; " + desc
        item["_value"] += p.value
    items = []
    for key in sorted(
        grouped,
        key=lambda k: (
            k[0],
            str(k[1]),
            str(k[2]),
            -1 if k[3] is None else k[3],
            -1 if k[4] is None else k[4],
        ),
    ):
        item = grouped[key]
        value = item.pop("_value")
        if item["kind"] in _HOURS_KINDS and "ore_slot" not in item:
            item["ore"] = value
        item["conflict_id"] = uuid.uuid4().hex
        items.append(item)
    return items


# Kinds whose amount is a number of hours rather than a flag.
_HOURS_KINDS = {CLASSE_UNFILLED, ORE_TARGET_DEVIATION, MONTE_ORE_EXCEEDED}

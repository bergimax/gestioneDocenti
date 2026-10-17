"""Natural-language instructions (#42): from a typed sentence to a derogation.

The sentence is interpreted by `ai.instruction.interpret_instruction`; an
understood one is stored as a `Draft` (kind INSTRUCTION) and nothing is applied
until the admin confirms. Confirming goes through `apply_quick_action`, which
enqueues the one regeneration and records the audit entry.
"""

import datetime

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction

from ai.instruction import interpret_instruction
from projects.permissions import can_move_lesson, can_use_ai_instructions
from school.models import Class, Draft, Teacher, TeacherType

from .derogations import (
    DAY_WORDS,
    QUICK_ACTIONS,
    REDUCE_CONTRACT_HOURS,
    scope_words,
)
from .jobs import GenerationRefused
from .models import Day, Slot, Timetable, TimetableState
from .moves import MSG_STALE, MoveRejected, move_lesson
from .quick_actions import ERROR_NO_TIMETABLE, apply_quick_action
from .tasks import ERROR_APPROVED

MSG_NOT_FOUND = "L'istruzione non esiste più: scrivila di nuovo."
MSG_NOT_PENDING = "L'istruzione è già stata confermata o scartata."
MSG_OTHER_WEEK = "L'istruzione riguarda un'altra settimana: scrivila di nuovo."
MSG_GONE = (
    "Il docente o la classe indicati non esistono più: scrivi di nuovo l'istruzione."
)
MSG_REDUCE_ASSUNTO = (
    "Questa deroga vale solo per i docenti a contratto: il docente indicato non lo è."
)

MSG_SAME_CELL = (
    "La lezione è già in quel giorno e a quell'ora: indica un'altra destinazione."
)
MSG_SLOT_GONE = "La lezione non esiste più nell'orario: scrivi di nuovo l'istruzione."
MSG_MOVED = "Lezione spostata: {scope}."
MOVE_LESSON = "move_lesson"
MOVE_LABEL = "Sposta una lezione"
MOVE_PARAMS = (
    "classe",
    "docente",
    "giorno",
    "ora",
    "giorno_destinazione",
    "ora_destinazione",
)

LABELS = {key: value[0] for key, value in QUICK_ACTIONS.items()}
PARAMS = {key: tuple(value[1]) for key, value in QUICK_ACTIONS.items()}


class InstructionRefused(Exception):
    """The request cannot proceed; `args[0]` is an Italian message."""


def feature_on():
    return bool(settings.ANTHROPIC_API_KEY)


def _timetable(school, week_start):
    return Timetable.objects.filter(school=school, week_start=week_start).first()


def check_draft_week(timetable):
    if timetable is None:
        raise InstructionRefused(ERROR_NO_TIMETABLE)
    if timetable.state == TimetableState.APPROVATO:
        raise InstructionRefused(ERROR_APPROVED)


def build_context(school, week_start):
    """What the model may see: names and ids, never anything else."""
    return {
        "week_start": week_start.isoformat(),
        "teachers": [
            {"id": t.pk, "name": t.name}
            for t in Teacher.objects.filter(school=school).order_by("name", "pk")
        ],
        "classes": [
            {"id": c.pk, "name": c.name}
            for c in Class.objects.filter(school=school).order_by("name", "pk")
        ],
        "actions": [
            {"type": key, "label": LABELS[key], "params": list(PARAMS[key])}
            for key in LABELS
        ]
        + [{"type": MOVE_LESSON, "label": MOVE_LABEL, "params": list(MOVE_PARAMS)}],
        "days": list(Day.values),
    }


def _params(payload):
    return {
        "action_type": payload["action_type"],
        "class_id": payload.get("class_id"),
        "teacher_id": payload.get("teacher_id"),
        "day": payload.get("day"),
        "max_hours": payload.get("max_hours"),
    }


def _check_teacher_kind(action, teacher):
    if (
        action == REDUCE_CONTRACT_HOURS
        and teacher is not None
        and teacher.type != TeacherType.CONTRATTO
    ):
        raise InstructionRefused(MSG_REDUCE_ASSUNTO)


def create_instruction(school, week_start, user, text):
    """Interpret `text`; return a stored Draft, or a clarification string.

    Raises InstructionRefused (week not a draft, rule broken) or AiError. A
    new understood sentence discards the user's earlier pending ones of the
    week. Nothing is applied.
    """
    check_draft_week(_timetable(school, week_start))
    result = interpret_instruction(text, build_context(school, week_start))
    if result["status"] == "clarify":
        return result["question"]
    if result["action_type"] == MOVE_LESSON:
        return _create_move(school, week_start, user, text, result)
    teacher = None
    if result["teacher_id"] is not None:
        teacher = Teacher.objects.filter(school=school, pk=result["teacher_id"]).first()
    _check_teacher_kind(result["action_type"], teacher)
    payload = {
        "action_type": result["action_type"],
        "class_id": result["class_id"],
        "teacher_id": result["teacher_id"],
        "day": result["day"],
        "max_hours": result["max_hours"],
        "week_start": week_start.isoformat(),
    }
    return _store(user, text, payload)


def _store(user, text, payload):
    with transaction.atomic():
        Draft.objects.filter(
            kind=Draft.Kind.INSTRUCTION,
            status=Draft.Status.DRAFT,
            created_by=user,
            payload__week_start=payload["week_start"],
        ).update(status=Draft.Status.DISCARDED)
        return Draft.objects.create(
            kind=Draft.Kind.INSTRUCTION,
            payload=payload,
            source=text.strip()[: Draft._meta.get_field("source").max_length],
            created_by=user,
        )


def _create_move(school, week_start, user, text, result):
    """Find the lesson the model described (by code); Draft or a question."""
    timetable = _timetable(school, week_start)
    if (result["day"], result["hour"]) == (result["to_day"], result["to_hour"]):
        raise InstructionRefused(MSG_SAME_CELL)
    slots = Slot.objects.filter(
        timetable=timetable,
        school_class_id=result["class_id"],
        day=result["day"],
        start_hour=result["hour"],
    )
    if result["teacher_id"] is not None:
        slots = slots.filter(teacher_id=result["teacher_id"])
    found = list(slots.select_related("school_class")[:2])
    klass = Class.objects.filter(school=school, pk=result["class_id"]).first()
    name = klass.name if klass else ""
    if not found:
        return (
            f"Non c'è nessuna lezione della {name} di {DAY_WORDS[result['day']]} "
            f"all'ora {result['hour']}: controlla giorno e ora."
        )
    if len(found) > 1:
        return (
            f"La {name} ha più lezioni di {DAY_WORDS[result['day']]} all'ora "
            f"{result['hour']}: di quale docente è la lezione da spostare?"
        )
    payload = {
        "action_type": MOVE_LESSON,
        "class_id": result["class_id"],
        "teacher_id": result["teacher_id"],
        "day": result["day"],
        "hour": result["hour"],
        "to_day": result["to_day"],
        "to_hour": result["to_hour"],
        "slot_id": found[0].pk,
        "week_start": week_start.isoformat(),
        "timetable_version": timetable.version,
    }
    return _store(user, text, payload)


def card(school, draft):
    """The confirmation card, built from the validated fields only."""
    payload = draft.payload
    action = payload["action_type"]
    if action == MOVE_LESSON:
        return _move_card(school, draft)
    teacher = Teacher.objects.filter(school=school, pk=payload.get("teacher_id"))
    klass = Class.objects.filter(school=school, pk=payload.get("class_id"))
    teacher, klass = teacher.first(), klass.first()
    day = payload.get("day")
    week = datetime.date.fromisoformat(payload["week_start"])
    return {
        "id": draft.pk,
        "label": LABELS[action],
        "teacher": teacher.name if teacher else None,
        "class": klass.name if klass else None,
        "day": DAY_WORDS[day] if day else None,
        "max_hours": payload.get("max_hours"),
        "week": week,
    }


def _move_card(school, draft):
    payload = draft.payload
    slot = (
        Slot.objects.filter(pk=payload["slot_id"], timetable__school=school)
        .select_related("school_class", "teacher", "subject")
        .first()
    )
    return {
        "id": draft.pk,
        "move": True,
        "label": MOVE_LABEL,
        "class": slot.school_class.name if slot else None,
        "teacher": slot.teacher.name if slot else None,
        "subject": slot.subject.name if slot else None,
        "from_day": DAY_WORDS[payload["day"]],
        "from_hour": payload["hour"],
        "to_day": DAY_WORDS[payload["to_day"]],
        "to_hour": payload["to_hour"],
        "week": datetime.date.fromisoformat(payload["week_start"]),
    }


def _pending_draft(week_start, user, draft_id):
    draft = (
        Draft.objects.select_for_update()
        .filter(pk=draft_id, kind=Draft.Kind.INSTRUCTION, created_by=user)
        .first()
    )
    if draft is None:
        raise InstructionRefused(MSG_NOT_FOUND)
    if draft.status != Draft.Status.DRAFT:
        raise InstructionRefused(MSG_NOT_PENDING)
    if draft.payload.get("week_start") != week_start.isoformat():
        raise InstructionRefused(MSG_OTHER_WEEK)
    return draft


def confirm_instruction(school, week_start, user, draft_id):
    """Apply the stored derogation; return (label, scope in words).

    Re-validates against current data. The Draft becomes CONFIRMED in the same
    transaction as the enqueue (and its audit entry).
    """
    with transaction.atomic():
        draft = _pending_draft(week_start, user, draft_id)
        timetable = _timetable(school, week_start)
        check_draft_week(timetable)
        if draft.payload.get("action_type") == MOVE_LESSON:
            return _confirm_move(school, user, draft, timetable)
        params = _params(draft.payload)
        if params["action_type"] not in LABELS:
            raise InstructionRefused(MSG_NOT_FOUND)
        teacher = klass = None
        if params["teacher_id"] is not None:
            teacher = Teacher.objects.filter(
                school=school, pk=params["teacher_id"]
            ).first()
        if params["class_id"] is not None:
            klass = Class.objects.filter(school=school, pk=params["class_id"]).first()
        if (params["teacher_id"] is not None and teacher is None) or (
            params["class_id"] is not None and klass is None
        ):
            raise InstructionRefused(MSG_GONE)
        _check_teacher_kind(params["action_type"], teacher)
        try:
            apply_quick_action(week_start, user, params)
        except GenerationRefused as exc:
            raise InstructionRefused(str(exc)) from None
        draft.status = Draft.Status.CONFIRMED
        draft.save(update_fields=["status"])
    scope = scope_words(
        params, klass.name if klass else None, teacher.name if teacher else None
    )
    return LABELS[params["action_type"]], scope


def _confirm_move(school, user, draft, timetable):
    """Re-validate the stored move and apply it; return (label, scope)."""
    if not can_move_lesson(user, timetable):
        raise PermissionDenied
    payload = draft.payload
    if timetable.version != payload["timetable_version"]:
        raise InstructionRefused(MSG_STALE)
    slot = (
        Slot.objects.filter(pk=payload["slot_id"], timetable=timetable)
        .select_related("school_class")
        .first()
    )
    if slot is None:
        raise InstructionRefused(MSG_SLOT_GONE)
    try:
        move_lesson(
            timetable.pk,
            slot.pk,
            payload["to_day"],
            payload["to_hour"],
            payload["timetable_version"],
            user,
        )
    except MoveRejected as exc:
        raise InstructionRefused(exc.message) from None
    draft.status = Draft.Status.CONFIRMED
    draft.save(update_fields=["status"])
    scope = (
        f"{slot.school_class.name}, da {DAY_WORDS[payload['day']]} ora "
        f"{payload['hour']} a {DAY_WORDS[payload['to_day']]} ora {payload['to_hour']}"
    )
    return MOVE_LABEL, scope


def discard_instruction(school, week_start, user, draft_id):
    with transaction.atomic():
        draft = _pending_draft(week_start, user, draft_id)
        check_draft_week(_timetable(school, week_start))
        draft.status = Draft.Status.DISCARDED
        draft.save(update_fields=["status"])


def panel_context(
    school,
    week,
    user,
    draft=None,
    question=None,
    message=None,
    error=None,
    text="",
    oob=False,
):
    """The instruction box: an ADMIN, a draft week and an API key."""
    timetable = _timetable(school, week["start"])
    visible = (
        feature_on()
        and timetable is not None
        and timetable.state == TimetableState.BOZZA
        and can_use_ai_instructions(user, timetable)
    )
    return {
        "week": week,
        "instr_visible": visible,
        "instr_oob": oob,
        "instr_card": card(school, draft) if draft is not None and visible else None,
        "instr_question": question,
        "instr_message": message,
        "instr_error": error,
        "instr_text": text,
    }

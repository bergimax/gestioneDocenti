"""Alternatives and pairwise choice (architecture 7.5).

A GENERATE job with the `alternatives` param solves up to three weight
variations (scheduling.solver.alternatives) and keeps them in its `result`;
the week's timetable and its version are not touched. `choose_alternative`
writes the picked one and stores the pairwise preference (`AlternativePick`),
in one transaction with the single version bump. No weight is ever changed here.
"""

import dataclasses
import logging
import time

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction

from projects.permissions import can_choose_alternative
from school.models import Subject, Teacher
from school.selectors import current_school

from . import audit
from .context import build_week_context
from .models import (
    AlternativePick,
    JobKind,
    JobStatus,
    SolveJob,
    Timetable,
    TimetableState,
)
from .proposals import active_weights
from .services import TimetableLocked, apply_change
from .solver import (
    CLASSE_DOUBLE_BOOKING,
    CLASSE_EARLY_START,
    CLASSE_LATE_START,
    CLASSE_OVER_CAPACITY,
    CLASSE_STAGE_DAY,
    CLASSE_UNFILLED,
    CONTRACTOR_GAP,
    DOCENTE_DOUBLE_BOOKING,
    MONTE_ORE_EXCEEDED,
    ORE_TARGET_DEVIATION,
    SINGLE_CLASSE_DAY,
    TEORIA_CONSECUTIVE,
    Lesson,
    SolveStatus,
    generate_alternatives,
)
from .tasks import (
    _SOLVE_LOCK,
    ALTERNATIVES_KEY,
    ERROR_APPROVED,
    ERROR_UNEXPECTED,
    _build_slots,
    finish_failed,
    infeasible_message,
    timeout_message,
    write_timetable,
)

logger = logging.getLogger(__name__)

MSG_NO_MEANINGFUL = "Nessuna alternativa significativa"
MSG_ALREADY_PICKED = "Scelta già effettuata"
MSG_STALE = (
    "L'orario è cambiato dopo il calcolo delle alternative: "
    "genera di nuovo le alternative."
)
MSG_NOT_FOUND = "Alternative non trovate."
MSG_NOT_READY = "Le alternative non sono ancora pronte."

LETTERS = "ABC"

# Constraint kind -> Italian label for the comparison.
KIND_LABELS = {
    CLASSE_LATE_START: "Inizio non alle 8 (S1)",
    TEORIA_CONSECUTIVE: "Teoria consecutiva (S2)",
    ORE_TARGET_DEVIATION: "Scarto dalle ore target (S3)",
    CONTRACTOR_GAP: "Ora buca del contrattista (S4)",
    SINGLE_CLASSE_DAY: "Giornata con una sola classe (S5)",
    CLASSE_UNFILLED: "Ore non coperte",
    CLASSE_OVER_CAPACITY: "Classe oltre la capienza",
    CLASSE_DOUBLE_BOOKING: "Classe con due lezioni nella stessa ora",
    DOCENTE_DOUBLE_BOOKING: "Docente in due classi nella stessa ora",
    MONTE_ORE_EXCEEDED: "Monte ore superato",
    CLASSE_EARLY_START: "Lezione prima dell'inizio",
    CLASSE_STAGE_DAY: "Lezione in giorno di stage",
    "H4": "Disponibilità ignorata (H4)",
    "H9": "Inizio tardivo del venerdì (H9)",
    "H10": "Blocco di pratica breve (H10)",
    "H11": "Giornata con una sola classe (H11)",
}


class AlternativeRefused(Exception):
    """The pick cannot be made; `status` is the HTTP status."""

    status = 409

    def __init__(self, message, status=409):
        super().__init__(message)
        self.message = message
        self.status = status


def active_soft_weights(school):
    """The weights generation and the alternatives start from: the newest
    adjustment of each kind (scheduling.proposals), else the default."""
    return active_weights(school)


def kind_label(kind):
    return KIND_LABELS.get(kind, kind)


def describe(variation):
    """The variation in words, from the stored `variation` dict."""
    if not variation or not variation.get("changed"):
        return "Pesi attuali"
    verb = "ridotto" if variation["factor"] < 1 else "aumentato"
    return (
        f"{kind_label(variation['changed'])}: peso {verb} "
        f"(×{variation['factor']:g}, da {variation['old']:g} a {variation['new']:g})"
    )


def _summary(alt):
    """The stored evidence of an alternative (no lessons)."""
    return {
        "index": alt["index"],
        "label": alt["label"],
        "variation": alt["variation"],
        "weights": alt["weights"],
        "score": alt["score"],
        "level": alt["level"],
        "n_soft_conflicts": alt["n_soft_conflicts"],
        "violations_by_kind": alt["violations_by_kind"],
    }


def _serialize(index, alt):
    v, result = alt.variation, alt.result
    return {
        "index": index,
        "label": f"Alternativa {LETTERS[index]}",
        "variation": {
            "changed": v.changed,
            "factor": v.factor,
            "old": v.old,
            "new": v.new,
        },
        "weights": v.weights,
        "score": result.quality_score,
        "level": result.quality_level,
        "n_soft_conflicts": result.n_soft_conflicts,
        "violations_by_kind": alt.violations_by_kind,
        "lessons": [[le.assignment_id, le.day, le.hour] for le in result.lessons],
        "conflict_items": result.conflict_items,
    }


def _base_version(job):
    timetable = Timetable.objects.filter(
        school_id=job.school_id, week_start=job.week_start
    ).first()
    if timetable is not None and timetable.state == TimetableState.APPROVATO:
        raise TimetableLocked(timetable.pk)
    return 0 if timetable is None else timetable.version


def run_alternatives(job):
    """Run one PENDING alternatives job to DONE or FAILED. Never raises."""
    job.transition_to(JobStatus.RUNNING)
    try:
        base_version = _base_version(job)
        ctx = build_week_context(job.week_start, job.school)
        total = settings.SOLVER_TIMEOUT_SECONDS
        ctx = dataclasses.replace(
            ctx,
            timeout_seconds=total,
            explain_budget_seconds=settings.SOLVER_EXPLAIN_BUDGET_SECONDS,
        )
        with _SOLVE_LOCK:
            outcome = generate_alternatives(
                ctx, active_soft_weights(job.school), total, time.monotonic
            )
        if not outcome.alternatives:
            if outcome.first_status == SolveStatus.INFEASIBLE:
                error = infeasible_message()
            else:
                error = timeout_message(total)
        else:
            meaningful = len(outcome.alternatives) > 1
            job.transition_to(
                JobStatus.DONE,
                result={
                    "base_version": base_version,
                    "meaningful": meaningful,
                    "message": "" if meaningful else MSG_NO_MEANINGFUL,
                    "alternatives": (
                        [_serialize(i, a) for i, a in enumerate(outcome.alternatives)]
                        if meaningful
                        else []
                    ),
                    "picked": False,
                },
            )
            return
    except TimetableLocked:
        error = ERROR_APPROVED
    except Exception as exc:
        logger.exception("Alternatives job %s failed", job.pk)
        error = f"{ERROR_UNEXPECTED} ({exc})"
    finish_failed(job, error)


def is_alternatives_job(job):
    return job is not None and bool(job.params.get(ALTERNATIVES_KEY))


def panel(job, timetable):
    """What the comparison shows for a DONE meaningful job, else None."""
    if not is_alternatives_job(job) or job.status != JobStatus.DONE:
        return None
    result = job.result
    if not result.get("meaningful") or result.get("picked"):
        return None
    alts = result["alternatives"]
    kinds = sorted({k for a in alts for k in a["violations_by_kind"]})
    rows = []
    for kind in kinds:
        values = [a["violations_by_kind"].get(kind, 0) for a in alts]
        rows.append(
            {
                "kind": kind,
                "label": kind_label(kind),
                "values": values,
                "differs": len(set(values)) > 1,
            }
        )
    current = 0 if timetable is None else timetable.version
    return {
        "job": job,
        "alternatives": [{**a, "description": describe(a["variation"])} for a in alts],
        "rows": rows,
        "stale": current != result["base_version"],
    }


def _alternatives_job(job_id):
    job = SolveJob.objects.filter(
        pk=job_id, school=current_school(), kind=JobKind.GENERATE
    ).first()
    if job is None or not is_alternatives_job(job):
        raise AlternativeRefused(MSG_NOT_FOUND, status=404)
    if job.status != JobStatus.DONE:
        raise AlternativeRefused(MSG_NOT_READY)
    return job


def get_alternative(job_id, index):
    """The stored alternative `index` of a DONE job (for the grid preview)."""
    job = _alternatives_job(job_id)
    alts = job.result.get("alternatives") or []
    if not 0 <= index < len(alts):
        raise AlternativeRefused(MSG_NOT_FOUND, status=404)
    return job, alts[index]


def lessons_of(alt):
    return [Lesson(a, d, h) for a, d, h in alt["lessons"]]


def preview_slots(job, alt):
    """Unsaved Slots of a stored alternative, for the read-only grid."""
    lessons = lessons_of(alt)
    slots = _build_slots(None, lessons, job.school_id)
    teachers = Teacher.objects.in_bulk({s.teacher_id for s in slots})
    subjects = Subject.objects.in_bulk({s.subject_id for s in slots})
    for slot in slots:
        slot.teacher = teachers[slot.teacher_id]
        slot.subject = subjects[slot.subject_id]
    return slots


def choose_alternative(job_id, index, user):
    """Write alternative `index` of the job as the week's timetable.

    One transaction: slots, score and conflicts, the stored pick and the one
    version bump. Returns the refreshed Timetable. Raises PermissionDenied or
    AlternativeRefused (unknown job or alternative, nothing meaningful, already
    picked, the week changed since, approved week); a refusal writes nothing.
    """
    if not can_choose_alternative(user):
        raise PermissionDenied
    job, chosen = get_alternative(job_id, index)
    if not job.result.get("meaningful"):
        raise AlternativeRefused(MSG_NO_MEANINGFUL)
    try:
        with transaction.atomic():
            timetable, _ = Timetable.objects.get_or_create(
                school_id=job.school_id, week_start=job.week_start
            )

            def change(locked):
                fresh = SolveJob.objects.select_for_update().get(pk=job_id)
                if AlternativePick.objects.filter(job=fresh).exists():
                    raise AlternativeRefused(MSG_ALREADY_PICKED)
                if locked.version != fresh.result["base_version"]:
                    raise AlternativeRefused(MSG_STALE)
                write_timetable(
                    locked,
                    job.school_id,
                    lessons_of(chosen),
                    chosen["score"],
                    chosen["level"],
                    chosen["n_soft_conflicts"],
                    chosen["conflict_items"],
                )
                alts = fresh.result["alternatives"]
                AlternativePick.objects.create(
                    job=fresh,
                    school_id=fresh.school_id,
                    week_start=fresh.week_start,
                    picked_by=user,
                    chosen=_summary(chosen),
                    rejected=[_summary(a) for a in alts if a["index"] != index],
                )
                fresh.result = {**fresh.result, "picked": True, "chosen": index}
                fresh.save(update_fields=["result"])
                audit.record_audit(
                    audit.ALTERNATIVE_CHOSEN,
                    user,
                    locked.week_start,
                    {
                        "job": job_id,
                        "indice": index,
                        "etichetta": chosen["label"],
                        "punteggio": chosen["score"],
                        "livello": chosen["level"],
                    },
                )

            return apply_change(timetable.pk, change)
    except TimetableLocked:
        raise AlternativeRefused(ERROR_APPROVED) from None

"""The audit log entry point: one append-only row per action on a week."""

from school.selectors import current_school

QUICK_ACTION = "QUICK_ACTION"
SCHEDULE_APPROVED = "SCHEDULE_APPROVED"
APPROVAL_UNDONE = "APPROVAL_UNDONE"
HOURS_ADJUSTED = "HOURS_ADJUSTED"
GENERATE = "GENERATE"
MODIFY_SLOT = "MODIFY_SLOT"
CONFLICT_DECISION = "CONFLICT_DECISION"
REPAIR_CONFIRMED = "REPAIR_CONFIRMED"
ALTERNATIVE_CHOSEN = "ALTERNATIVE_CHOSEN"
FEED_REGENERATED = "FEED_REGENERATED"
FEEDBACK_ADDED = "FEEDBACK_ADDED"
SOFT_WEIGHTS_APPLIED = "SOFT_WEIGHTS_APPLIED"
SOFT_WEIGHTS_RESET = "SOFT_WEIGHTS_RESET"
REPAIR_DISMISSED = "REPAIR_DISMISSED"
AVAILABILITY_LINK_CREATED = "AVAILABILITY_LINK_CREATED"
AVAILABILITY_LINK_REVOKED = "AVAILABILITY_LINK_REVOKED"
SETUP_SAVED = "SETUP_SAVED"

# action -> label shown on the audit screen
ACTION_LABELS = {
    GENERATE: "Generazione orario",
    MODIFY_SLOT: "Modifica lezione",
    CONFLICT_DECISION: "Decisione sui conflitti",
    QUICK_ACTION: "Azione rapida",
    REPAIR_CONFIRMED: "Sostituzione confermata",
    ALTERNATIVE_CHOSEN: "Alternativa scelta",
    SCHEDULE_APPROVED: "Approvazione settimana",
    APPROVAL_UNDONE: "Approvazione annullata",
    HOURS_ADJUSTED: "Correzione ore erogate",
    FEED_REGENERATED: "Rigenerazione feed",
    FEEDBACK_ADDED: "Nota sull'orario",
    SOFT_WEIGHTS_APPLIED: "Pesi applicati",
    SOFT_WEIGHTS_RESET: "Pesi ripristinati",
    REPAIR_DISMISSED: "Sostituzione annullata",
    AVAILABILITY_LINK_CREATED: "Link disponibilita creato",
    AVAILABILITY_LINK_REVOKED: "Link disponibilita revocato",
    SETUP_SAVED: "Setup salvato",
}
ACTIONS = frozenset(ACTION_LABELS)


def record_audit(action, user, week, params):
    """Write one audit row: `user` did `action` on the week starting `week`.

    `week` is the week-start date (None when the action is not tied to a
    week); `params` a JSON-able dict, never holding secrets. Call it inside the
    transaction of the action, so a rollback removes the entry. Raises
    ValueError for an unknown action.
    """
    from .models import AuditLog, Timetable

    if action not in ACTIONS:
        raise ValueError(f"unknown audit action: {action!r}")
    school = current_school()
    timetable = None
    if week is not None:
        timetable = Timetable.objects.filter(school=school, week_start=week).first()
    AuditLog.objects.create(
        school=school,
        timetable=timetable,
        week_start=week,
        action=action,
        user=user if getattr(user, "pk", None) else None,
        details=params or {},
    )

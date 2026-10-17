"""Free-text notes on a week's timetable (architecture section 8).

A note is append-only and never changes the timetable, its version, a weight
or a proposal; this module must stay independent of `proposals`.
"""

from django.db import transaction

from . import audit
from .models import Timetable, TimetableFeedback

MAX_NOTE_LENGTH = 2000
MSG_NOTE_REQUIRED = "Scrivi una nota."
MSG_NOTE_TOO_LONG = f"La nota può avere al massimo {MAX_NOTE_LENGTH} caratteri."


class InvalidNote(ValueError):
    """The note is empty or too long; `args[0]` is the message to show."""


def add_feedback(timetable_id, user, note):
    """Save `note` on the timetable with a snapshot of its quality.

    One transaction on the locked timetable row. Raises InvalidNote (nothing
    written) when the trimmed note is empty or over 2000 characters.
    """
    text = (note or "").strip()
    if not text:
        raise InvalidNote(MSG_NOTE_REQUIRED)
    if len(text) > MAX_NOTE_LENGTH:
        raise InvalidNote(MSG_NOTE_TOO_LONG)
    with transaction.atomic():
        locked = Timetable.objects.select_for_update().get(pk=timetable_id)
        feedback = TimetableFeedback.objects.create(
            school_id=locked.school_id,
            timetable=locked,
            week_start=locked.week_start,
            user=user if getattr(user, "pk", None) else None,
            note=text,
            quality_score=locked.quality_score,
            quality_level=locked.quality_level,
            soft_conflicts=locked.soft_conflicts,
            timetable_state=locked.state,
        )
        audit.record_audit(
            audit.FEEDBACK_ADDED,
            user,
            locked.week_start,
            {"feedback_id": feedback.pk, "lunghezza_nota": len(text)},
        )
    return feedback

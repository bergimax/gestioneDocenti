"""Writes to a timetable. All board mutations go through apply_change."""

from django.db import transaction
from django.db.models import F

from .models import Timetable, TimetableState


class TimetableLocked(Exception):
    """The timetable is APPROVATO: no changes allowed."""


class ForbiddenTimetableChange(Exception):
    """A change function touched state or approved_at."""


def apply_change(timetable_id, change_fn):
    """Run `change_fn(locked_timetable)` and bump the version exactly once.

    Everything happens in one transaction on a row locked with
    SELECT FOR UPDATE; any exception rolls back edits and bump together.
    Returns the refreshed Timetable. The bump also happens when the change
    edits nothing.
    """
    with transaction.atomic():
        locked = Timetable.objects.select_for_update().get(pk=timetable_id)
        if locked.state == TimetableState.APPROVATO:
            raise TimetableLocked(timetable_id)
        state, approved_at = locked.state, locked.approved_at
        change_fn(locked)
        current = Timetable.objects.values("state", "approved_at").get(pk=timetable_id)
        if current["state"] != state or current["approved_at"] != approved_at:
            raise ForbiddenTimetableChange(timetable_id)
        Timetable.objects.filter(pk=timetable_id).update(version=F("version") + 1)
        return Timetable.objects.get(pk=timetable_id)

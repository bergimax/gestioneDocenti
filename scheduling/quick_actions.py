"""Applying a quick action (derogation): one regeneration of a draft week."""

from django.core.exceptions import PermissionDenied
from django.db import transaction

from projects.permissions import can_apply_quick_action
from school.models import Class, Teacher
from school.selectors import current_school

from . import audit
from .derogations import PARAMS_KEY, scope_words
from .jobs import GenerationRefused, enqueue_job
from .models import Timetable, TimetableState
from .tasks import ERROR_APPROVED

ERROR_NO_TIMETABLE = "Non c'è ancora un orario da rigenerare: genera prima l'orario."


def apply_quick_action(week_start, user, params):
    """Enqueue the regeneration of the week with the derogation `params`.

    `params` is the validated dict of `QuickActionForm.params()`. Returns
    (job, scope in words). Raises PermissionDenied or GenerationRefused (no
    draft timetable, approved week, a job already active). The derogation
    lives only in the job's params; nothing else stores it. A refusal records
    nothing in the audit log.
    """
    school = current_school()
    timetable = Timetable.objects.filter(school=school, week_start=week_start).first()
    if not can_apply_quick_action(user, timetable):
        raise PermissionDenied
    if timetable is None:
        raise GenerationRefused(ERROR_NO_TIMETABLE)
    if timetable.state == TimetableState.APPROVATO:
        raise GenerationRefused(ERROR_APPROVED)
    with transaction.atomic():  # no enqueued job without its entry
        job = enqueue_job(week_start, user, {PARAMS_KEY: params}, refuse_active=True)
        audit.record_audit(audit.QUICK_ACTION, user, week_start, params)
    school_class = Class.objects.filter(pk=params["class_id"]).first()
    teacher = Teacher.objects.filter(pk=params["teacher_id"]).first()
    words = scope_words(
        params,
        school_class.name if school_class else None,
        teacher.name if teacher else None,
    )
    return job, words

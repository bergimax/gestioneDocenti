"""Every authorization rule of the application (specs §4).

Predicates take ``(user, obj=None)`` and return a bool. They never raise, deny
anonymous users, ``None``, inactive users and unknown or missing roles, and
this is the only non-model module that may look at ``user.role``.
"""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse
from django.template.loader import render_to_string

from accounts.models import Role

ADMIN_ONLY = frozenset({Role.ADMIN})
ALL_ROLES = frozenset({Role.ADMIN, Role.SEGRETERIA})


def _allowed(user, roles):
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if not getattr(user, "is_active", False):
        return False
    return getattr(user, "role", None) in roles


def can_view_timetable(user, obj=None):
    return _allowed(user, ALL_ROLES)


def can_view_school_data(user, obj=None):
    return _allowed(user, ALL_ROLES)


def can_edit_availability(user, obj=None):
    return _allowed(user, ALL_ROLES)


def can_manage_availability_links(user, teacher=None):
    return _allowed(user, ALL_ROLES)


def can_view_backup_status(user, obj=None):
    return _allowed(user, ADMIN_ONLY)


def can_generate(user, obj=None):
    return _allowed(user, ADMIN_ONLY)


def can_move_lesson(user, obj=None):
    return _allowed(user, ADMIN_ONLY)


def can_assign_slot(user, obj=None):
    return _allowed(user, ADMIN_ONLY)


def can_decide_conflicts(user, obj=None):
    return _allowed(user, ADMIN_ONLY)


def can_apply_quick_action(user, timetable=None):
    return _allowed(user, ADMIN_ONLY)


def can_use_ai_instructions(user, timetable=None):
    return _allowed(user, ADMIN_ONLY)


def can_repair(user, timetable=None):
    return _allowed(user, ADMIN_ONLY)


def can_choose_alternative(user, timetable=None):
    return _allowed(user, ADMIN_ONLY)


def can_export_timetable(user, timetable=None):
    return _allowed(user, ALL_ROLES)


def can_approve(user, timetable=None):
    return _allowed(user, ADMIN_ONLY)


def can_undo_approval(user, timetable=None):
    return _allowed(user, ADMIN_ONLY)


def can_adjust_hours(user, assignment=None):
    return _allowed(user, ADMIN_ONLY)


def can_view_audit_log(user, obj=None):
    return _allowed(user, ADMIN_ONLY)


def can_manage_feeds(user, obj=None):
    return _allowed(user, ADMIN_ONLY)


def can_run_setup(user, obj=None):
    return _allowed(user, ADMIN_ONLY)


def can_edit_school_data(user, obj=None):
    return _allowed(user, ADMIN_ONLY)


def can_edit_teachers(user, obj=None):
    return _allowed(user, ADMIN_ONLY)


def can_manage_soft_weights(user, obj=None):
    return _allowed(user, ADMIN_ONLY)


def can_leave_feedback(user, timetable=None):
    return _allowed(user, ADMIN_ONLY)


PREDICATES = (
    can_view_backup_status,
    can_view_timetable,
    can_view_school_data,
    can_edit_availability,
    can_manage_availability_links,
    can_generate,
    can_move_lesson,
    can_assign_slot,
    can_decide_conflicts,
    can_apply_quick_action,
    can_use_ai_instructions,
    can_repair,
    can_choose_alternative,
    can_export_timetable,
    can_approve,
    can_undo_approval,
    can_adjust_hours,
    can_view_audit_log,
    can_manage_feeds,
    can_run_setup,
    can_edit_school_data,
    can_edit_teachers,
    can_manage_soft_weights,
    can_leave_feedback,
)


def check_request(request, predicate, obj=None):
    """Return None when allowed, else the denial response.

    Anonymous users go to the login page, everyone else gets a 403 page that
    does not say which role would be needed.
    """
    user = getattr(request, "user", None)
    if predicate(user, obj):
        return None
    if user is None or not getattr(user, "is_authenticated", False):
        return redirect_to_login(request.get_full_path())
    body = render_to_string("projects/denied.html", request=request)
    return HttpResponse(body, status=403)


def permission_required(predicate, get_object=None):
    """Decorator for function views.

    ``get_object(request, *args, **kwargs)`` finds the object the predicate
    judges; it runs before the view body. Applies to every HTTP method.
    """

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            obj = get_object(request, *args, **kwargs) if get_object else None
            denied = check_request(request, predicate, obj)
            if denied is not None:
                return denied
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


class PredicatePermissionMixin:
    """Mixin for class-based views: set ``permission_predicate``.

    Override ``get_permission_object()`` to find the object to judge.
    """

    permission_predicate = None

    def get_permission_object(self):
        return None

    def dispatch(self, request, *args, **kwargs):
        self.request, self.args, self.kwargs = request, args, kwargs
        denied = check_request(
            request, self.permission_predicate, self.get_permission_object()
        )
        if denied is not None:
            return denied
        return super().dispatch(request, *args, **kwargs)

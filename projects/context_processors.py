"""Navigation menu and user label for the base layout.

Visibility comes only from the predicates of ``projects.permissions``.
"""

from projects.permissions import (
    can_edit_availability,
    can_edit_school_data,
    can_manage_feeds,
    can_manage_soft_weights,
    can_run_setup,
    can_view_audit_log,
    can_view_timetable,
)

MENU = (
    ("Panoramica", "home", can_view_timetable),
    ("Disponibilita", "availability", can_edit_availability),
    ("Orario", "timetable", can_view_timetable),
    ("Setup", "setup", can_run_setup),
    ("Dati scuola", "school_data", can_edit_school_data),
    ("Pesi", "soft_weights", can_manage_soft_weights),
    ("Feed", "feeds", can_manage_feeds),
    ("Registro", "audit_log", can_view_audit_log),
)


def navigation(request):
    user = getattr(request, "user", None)
    match = getattr(request, "resolver_match", None)
    current = match.url_name if match else None
    items = [
        {"label": label, "url_name": url_name, "active": url_name == current}
        for label, url_name, predicate in MENU
        if predicate(user)
    ]
    user_label = ""
    if items:
        user_label = user.get_role_display()
        if not can_edit_school_data(user):
            user_label += " / Sola lettura"
    return {"nav_items": items, "user_label": user_label}

"""The read-only audit log screen for the admin."""

import datetime
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.shortcuts import render
from django.views.decorators.http import require_GET

from projects.permissions import can_view_audit_log, permission_required

from .audit import ACTION_LABELS
from .models import AuditLog

PAGE_SIZE = 25


def _parse_week(raw):
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return None


@permission_required(can_view_audit_log)
@require_GET
def audit_page(request):
    """Entries newest first, 25 per page, filtered by week and by action."""
    raw_week = request.GET.get("settimana", "").strip()
    week = _parse_week(raw_week) if raw_week else None
    action = request.GET.get("azione", "")
    entries = AuditLog.objects.select_related("user")
    if week is not None:
        # any day of the week finds it: compare with its Monday
        entries = entries.filter(
            week_start=week - datetime.timedelta(days=week.weekday())
        )
    if action in ACTION_LABELS:
        entries = entries.filter(action=action)
    else:
        action = ""
    page = Paginator(entries, PAGE_SIZE).get_page(request.GET.get("page"))
    keep = urlencode(
        [(k, v) for k, v in (("settimana", raw_week), ("azione", action)) if v]
    )
    rows = [
        {
            "entry": e,
            "label": ACTION_LABELS.get(e.action, e.action),
            "details": [(k, _show(v)) for k, v in e.details.items()],
        }
        for e in page
    ]
    return render(
        request,
        "scheduling/audit.html",
        {
            "rows": rows,
            "page": page,
            "actions": ACTION_LABELS.items(),
            "week_value": raw_week,
            "action_value": action,
            "filtered": bool(week is not None or action),
            "keep": keep,
        },
    )


def _show(value):
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {_show(v)}" for k, v in value.items())
    return value

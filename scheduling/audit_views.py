"""The read-only audit log screen for the admin."""

import datetime
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.shortcuts import render
from django.views.decorators.http import require_GET

from ledger.models import LedgerEntry
from projects.permissions import can_view_audit_log, permission_required
from school.models import Class, Teacher

from .audit import ACTION_LABELS, FEED_REGENERATED, HOURS_ADJUSTED
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
    joined = _joined_values(page)
    rows = [
        {
            "entry": e,
            "label": ACTION_LABELS.get(e.action, e.action),
            "details": [(k, _show(v)) for k, v in e.details.items()]
            + joined.get(e.pk, []),
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


UNAVAILABLE = "(non disponibile)"


def _int_id(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _wanted(entry):
    """(display key, lookup kind, id) the entry needs joined, or None.

    Only new-shape entries qualify: the join key is present and the old
    free-text key is not, so an old entry renders exactly as stored.
    """
    d = entry.details
    if entry.action == HOURS_ADJUSTED and "voce" in d and "motivo" not in d:
        return "motivo", "voce", _int_id(d["voce"])
    if entry.action == FEED_REGENERATED and "id" in d and "nome" not in d:
        tipo = d.get("tipo")
        if tipo in ("teacher", "class"):
            return "nome", tipo, _int_id(d["id"])
        return "nome", None, None
    return None


def _joined_values(entries):
    """Extra `(key, value)` pair per entry pk, in at most 3 queries per school.

    The reason comes from the ledger entry, the name from the teacher or class,
    always scoped to the entry's school: another school's value is never shown.
    """
    wanted = {}
    for e in entries:
        need = _wanted(e)
        if need:
            wanted[e.pk] = (e.school_id, *need)
    lookups = {"voce": (LedgerEntry, "assignment__school", "reason")}
    lookups["teacher"] = (Teacher, "school", "name")
    lookups["class"] = (Class, "school", "name")
    result = {}
    for school_id in {w[0] for w in wanted.values()}:
        found = {}
        for kind, (model, school_field, column) in lookups.items():
            ids = {
                w[3]
                for w in wanted.values()
                if w[0] == school_id and w[2] == kind and w[3] is not None
            }
            if ids:
                found[kind] = dict(
                    model.objects.filter(
                        pk__in=ids, **{f"{school_field}_id": school_id}
                    ).values_list("pk", column)
                )
        for pk, (sid, key, kind, target) in wanted.items():
            if sid == school_id:
                value = found.get(kind, {}).get(target, UNAVAILABLE)
                result[pk] = [(key, value)]
    return result

import re

from django.contrib import messages
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from projects.permissions import can_manage_feeds, permission_required
from scheduling.models import Slot, TimetableState
from school.selectors import current_school

from . import services
from .ics import build_calendar
from .models import FeedToken

TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")  # token_urlsafe(32)
NOT_FOUND = "Feed non trovato."


def _not_found():
    # always the same answer: no hint of which part of the lookup failed
    return HttpResponse(NOT_FOUND, status=404, content_type="text/plain; charset=utf-8")


def _lookup(token):
    if not TOKEN_FORMAT.fullmatch(token):
        return None
    return (
        FeedToken.objects.select_related("teacher", "school_class")
        .filter(token=token)
        .exclude(teacher__active=False)
        .first()
    )


@require_GET
def feed_ics(request, token):
    """The public feed: the token in the URL is the credential."""
    feed = _lookup(token)
    if feed is None:
        return _not_found()
    slots = Slot.objects.filter(
        timetable__state=TimetableState.APPROVATO
    ).select_related("timetable", "subject", "teacher", "school_class", "paired_class")
    if feed.teacher_id:
        slots = slots.filter(teacher_id=feed.teacher_id)
        name = feed.teacher.name
    else:
        slots = slots.filter(
            Q(school_class_id=feed.school_class_id)
            | Q(paired_class_id=feed.school_class_id)
        )
        name = feed.school_class.name
    slots = slots.order_by("timetable__week_start", "pk")
    events = []
    for slot in slots:
        if feed.teacher_id:
            classes = [slot.school_class.name]
            if slot.paired_class_id:
                classes.append(slot.paired_class.name)
            other = " + ".join(classes)
        else:
            other = slot.teacher.name
        events.append((slot, f"{slot.subject.name} - {other}"))
    response = HttpResponse(
        build_calendar(name, events), content_type="text/calendar; charset=utf-8"
    )
    response["Cache-Control"] = "max-age=300"
    response["X-Robots-Tag"] = "noindex"
    return response


@permission_required(can_manage_feeds)
@require_GET
def feeds_page(request):
    school = current_school()
    rows = []
    if school is not None:
        services.ensure_tokens(school)
        base = f"{request.scheme}://{request.get_host()}"
        tokens = FeedToken.objects.filter(school=school).select_related(
            "teacher", "school_class"
        )
        for feed in tokens.order_by("pk"):
            if feed.teacher_id:
                if not feed.teacher.active:
                    continue
                row = (services.KIND_TEACHER, feed.teacher_id, feed.teacher.name)
            else:
                row = (
                    services.KIND_CLASS,
                    feed.school_class_id,
                    feed.school_class.name,
                )
            rows.append(
                {
                    "kind": row[0],
                    "pk": row[1],
                    "name": row[2],
                    "url": f"{base}/feeds/{feed.token}.ics",
                }
            )
        rows.sort(key=lambda r: r["name"].lower())
    return render(
        request,
        "feeds/feeds.html",
        {
            "school": school,
            "teachers": [r for r in rows if r["kind"] == services.KIND_TEACHER],
            "classes": [r for r in rows if r["kind"] == services.KIND_CLASS],
        },
    )


@permission_required(can_manage_feeds)
@require_POST
def feed_regenerate(request, kind, pk):
    school = current_school()
    if school is None or services.regenerate(school, kind, pk, request.user) is None:
        raise Http404
    messages.success(request, "Feed rigenerato: il vecchio indirizzo non funziona piu.")
    return redirect("feeds")

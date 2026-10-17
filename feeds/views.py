import datetime
import re

from django.conf import settings
from django.contrib import messages
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from export.layout import week_title
from projects.permissions import can_manage_feeds, permission_required
from scheduling.board import DAY_LABELS, HOURS
from scheduling.context import _is_stage
from scheduling.models import Day, Slot, Timetable, TimetableState
from school.models import CalendarEntry
from school.selectors import current_school

from . import services, throttle
from .ics import build_calendar
from .models import FeedToken

TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")  # token_urlsafe(32)
NOT_FOUND = "Feed non trovato."
LIMITED = "Troppi tentativi. Riprova piu tardi."


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
    address = request.META.get("REMOTE_ADDR", "")
    if throttle.is_limited(address):
        response = HttpResponse(
            LIMITED, status=429, content_type="text/plain; charset=utf-8"
        )
        response["Retry-After"] = str(settings.FEED_LOOKUP_WINDOW_SECONDS)
        return response
    feed = _lookup(token)
    if feed is None:
        throttle.record_failure(address)
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
            page_url = None
            if row[0] == services.KIND_CLASS and settings.PUBLIC_WEEK_PAGE_ENABLED:
                page_url = f"{base}/orario/{feed.token}/"
            rows.append(
                {
                    "kind": row[0],
                    "pk": row[1],
                    "name": row[2],
                    "url": f"{base}/feeds/{feed.token}.ics",
                    "page_url": page_url,
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


def _no_store(response):
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _public_not_found():
    return _no_store(_not_found())


@require_GET
def public_week(request, token):
    """Read-only page of one approved week of a class; the token is the credential.

    Off by default. Class tokens only, and nothing about teachers: the lessons
    are read as (day, hour, subject) in the queryset.
    """
    if not settings.PUBLIC_WEEK_PAGE_ENABLED:
        return _public_not_found()
    address = request.META.get("REMOTE_ADDR", "")
    if throttle.is_limited(address):
        response = HttpResponse(
            LIMITED, status=429, content_type="text/plain; charset=utf-8"
        )
        response["Retry-After"] = str(settings.FEED_LOOKUP_WINDOW_SECONDS)
        return _no_store(response)
    feed = None
    if TOKEN_FORMAT.fullmatch(token):
        feed = (
            FeedToken.objects.select_related("school", "school_class")
            .filter(token=token, school_class__isnull=False)
            .first()
        )
    if feed is None:
        throttle.record_failure(address)
        return _public_not_found()
    school, klass = feed.school, feed.school_class
    weeks = sorted(
        Timetable.objects.filter(school=school, state=TimetableState.APPROVATO)
        .order_by("week_start")
        .values_list("week_start", flat=True)
    )
    requested = request.GET.get("week")
    if requested is None:
        week = services.pick_week(weeks, timezone.localdate())
    else:
        try:
            week = datetime.date.fromisoformat(requested)
        except ValueError:
            return _public_not_found()
        if week not in weeks or week.isoformat() != requested:
            return _public_not_found()
    context = {
        "school_name": school.name,
        "class_name": klass.name,
        "title": None,
        "days": [],
        "rows": [],
        "previous_week": None,
        "next_week": None,
    }
    if week is not None:
        index = weeks.index(week)
        context["previous_week"] = weeks[index - 1] if index else None
        context["next_week"] = weeks[index + 1] if index + 1 < len(weeks) else None
        context["title"] = week_title(week)
        dates = [week + datetime.timedelta(days=i) for i in range(5)]
        lessons = {}
        for day, hour, subject in (
            Slot.objects.filter(
                timetable__school=school,
                timetable__week_start=week,
                timetable__state=TimetableState.APPROVATO,
            )
            .filter(Q(school_class_id=klass.pk) | Q(paired_class_id=klass.pk))
            .values_list("day", "start_hour", "subject__name")
        ):
            lessons[day, hour] = subject
        entries = list(CalendarEntry.objects.filter(school=school, date__in=dates))
        stage = [_is_stage(entries, date, klass) for date in dates]
        context["days"] = [DAY_LABELS[d] for d in Day.values]
        context["rows"] = [
            {
                "label": f"{hour}-{hour + 1}",
                "cells": [
                    {"kind": "lesson", "text": lessons[day, hour]}
                    if (day, hour) in lessons
                    else {"kind": "stage", "text": "STAGE"}
                    if stage[i]
                    else {"kind": "empty", "text": ""}
                    for i, day in enumerate(Day.values)
                ],
            }
            for hour in HOURS
        ]
    return _no_store(render(request, "feeds/public_week.html", context))

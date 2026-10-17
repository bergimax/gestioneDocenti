import datetime
import json
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import (
    Http404,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotAllowed,
)
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import add_never_cache_headers, patch_cache_control
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from projects.permissions import (
    can_edit_availability,
    can_manage_availability_links,
    permission_required,
)
from school.models import Teacher
from school.selectors import current_school, today

from . import grid as grid_logic
from . import links, services
from .models import AvailabilityLink

TPL = "availability/"
DAY_LABELS = {
    "lunedi": "Lun",
    "martedi": "Mar",
    "mercoledi": "Mer",
    "giovedi": "Gio",
    "venerdi": "Ven",
}


class BadRequest(Exception):
    pass


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _parse_week(school, raw):
    """The listed week whose Monday is `raw` (ISO date), else BadRequest."""
    try:
        start = datetime.date.fromisoformat(raw)
    except (TypeError, ValueError):
        raise BadRequest("Settimana non valida.") from None
    for week in services.school_weeks(school):
        if week["start"] == start:
            return week
    raise BadRequest("Settimana non presente nell'anno formativo.")


def _default_week(weeks):
    day = today()
    for week in weeks:
        if week["start"] <= day < week["start"] + datetime.timedelta(days=7):
            return week
    return weeks[0] if weeks else None


def _source_label(resolution):
    if resolution.errors:
        return "Griglia memorizzata non valida: mostrata quella predefinita"
    if resolution.source == grid_logic.STORED:
        return "Salvata per questa settimana"
    if resolution.source == grid_logic.INHERITED:
        return f"Copiata dalla settimana del {resolution.from_week:%d/%m/%Y}"
    return "Predefinita (tutto disponibile)"


def _display_grid(resolution):
    return grid_logic.default_grid() if resolution.errors else resolution.grid


def _card(row, week):
    resolution = row["resolution"]
    grid = _display_grid(resolution)
    days = [
        {
            "key": day,
            "label": DAY_LABELS[day],
            "slots": [
                {
                    "index": i,
                    "available": slot["disponibile"],
                    "start": slot["ora_inizio"],
                }
                for i, slot in enumerate(grid[day])
            ],
        }
        for day in grid_logic.DAYS
    ]
    teacher = row["teacher"]
    return {
        "teacher": teacher,
        "days": days,
        "hours": [s["start"] for s in days[0]["slots"]],
        "source": _source_label(resolution),
        "stored": resolution.source == grid_logic.STORED and not resolution.errors,
        "assunto": teacher.type == "ASSUNTO",
        "week": week,
    }


def _status_context(school, week):
    return {"status": services.week_status(school, week["start"]), "week": week}


def _page_context(school, week, notice=None):
    weeks = services.school_weeks(school)
    rows = services.school_rows(school, week["start"])
    previous = grid_logic.previous_week(weeks, week["start"])
    context = {
        "school": school,
        "weeks": weeks,
        "week": week,
        "cards": [_card(row, week) for row in rows],
        "has_previous": previous is not None,
        "notice": notice,
    }
    context.update(_status_context(school, week))
    return context


def _no_school(request):
    return render(request, TPL + "page.html", {"school": None})


def _bad_request(message):
    return HttpResponseBadRequest(message)


@permission_required(can_edit_availability)
@require_GET
def availability_page(request):
    school = current_school()
    if school is None:
        return _no_school(request)
    weeks = services.school_weeks(school)
    raw = request.GET.get("settimana")
    if raw is not None:
        try:
            week = _parse_week(school, raw)
        except BadRequest as e:
            return _bad_request(str(e))
    else:
        week = _default_week(weeks)
    if week is None:
        return render(request, TPL + "page.html", {"school": school, "weeks": []})
    context = _page_context(school, week)
    if (
        _is_htmx(request)
        and request.headers.get("HX-History-Restore-Request") != "true"
    ):
        return render(request, TPL + "_section.html", context)
    return render(request, TPL + "page.html", context)


def _teacher_of(school, teacher_id):
    try:
        return Teacher.objects.get(pk=teacher_id, school=school, active=True)
    except Teacher.DoesNotExist:
        raise Http404 from None


def _row_for(school, week, teacher):
    for row in services.school_rows(school, week["start"]):
        if row["teacher"].pk == teacher.pk:
            return row
    raise Http404


def _card_response(request, school, week, teacher):
    context = {"card": _card(_row_for(school, week, teacher), week)}
    context.update(_status_context(school, week))
    context["oob"] = True
    return render(request, TPL + "_card_update.html", context)


def _context_for_write(school, week_raw, teacher_id):
    week = _parse_week(school, week_raw)
    return week, _teacher_of(school, teacher_id)


def _toggled(resolution, day, slot):
    grid = _display_grid(resolution)
    if day not in grid_logic.DAYS:
        raise BadRequest("Giorno non valido.")
    try:
        index = int(slot)
    except (TypeError, ValueError):
        raise BadRequest("Fascia non valida.") from None
    if not 0 <= index < len(grid[day]):
        raise BadRequest("Fascia non valida.")
    grid[day][index]["disponibile"] = not grid[day][index]["disponibile"]
    return grid


def _write(school, week_raw, teacher_id, build_grid):
    """Resolve week/teacher, build the grid, save it; return (week, teacher)."""
    week, teacher = _context_for_write(school, week_raw, teacher_id)
    grid = build_grid(services.resolve_teacher_grid(teacher, week["start"]))
    try:
        services.save_availability(teacher, week["start"], grid)
    except (ValidationError, ValueError):
        raise BadRequest("Griglia non valida.") from None
    return week, teacher


def _do_write(request, week_raw, teacher_id, build_grid):
    school = current_school()
    if school is None:
        raise Http404
    try:
        week, teacher = _write(school, week_raw, teacher_id, build_grid)
    except BadRequest as e:
        return _bad_request(str(e))
    return _card_response(request, school, week, teacher)


@permission_required(can_edit_availability)
@require_POST
def availability_toggle(request, week, teacher_id):
    return _do_write(
        request,
        week,
        teacher_id,
        lambda resolution: _toggled(
            resolution, request.POST.get("day"), request.POST.get("slot")
        ),
    )


def _posted_grid(request):
    try:
        grid = json.loads(request.POST.get("grid", ""))
    except ValueError:
        raise BadRequest("Griglia non valida.") from None
    if grid_logic.grid_errors(grid):
        raise BadRequest("Griglia non valida.")
    return grid


@permission_required(can_edit_availability)
@require_POST
def availability_save(request, week, teacher_id):
    def build(_resolution):
        return _posted_grid(request)

    return _do_write(request, week, teacher_id, build)


def _copy_notice(result):
    if result["previous_week"] is None:
        return "Nessuna settimana precedente: non e stato copiato nulla."
    if result["previous_records"] == 0:
        return (
            "La settimana precedente non ha disponibilita registrate: "
            "non e stato copiato nulla."
        )
    return (
        f"Copiati {result['copied']} docenti, "
        f"saltati {result['skipped']} (gia compilati)."
    )


@permission_required(can_edit_availability)
@require_POST
def availability_copy(request, week):
    school = current_school()
    if school is None:
        raise Http404
    try:
        week_info = _parse_week(school, week)
    except BadRequest as e:
        return _bad_request(str(e))
    result = services.copy_previous_week(school, week_info["start"])
    context = _page_context(school, week_info, notice=_copy_notice(result))
    return render(request, TPL + "_section.html", context)


# magic links (issue 19)


def _link_teacher(request, week, teacher_id):
    school = current_school()
    return Teacher.objects.filter(pk=teacher_id, school=school).first()


def _no_store(response):
    patch_cache_control(response, no_store=True, private=True)
    add_never_cache_headers(response)
    return response


@permission_required(can_manage_availability_links, _link_teacher)
@require_POST
def link_generate(request, week, teacher_id):
    teacher = _link_teacher(request, week, teacher_id)
    context = {"week_raw": week, "teacher": teacher}
    try:
        try:
            week_start = datetime.date.fromisoformat(week)
        except ValueError:
            raise links.LinkError("Settimana non valida.") from None
        link, token = links.create_link(teacher, week_start, request.user)
    except links.LinkError as e:
        context["error"] = str(e)
        response = render(request, TPL + "link_created.html", context, status=400)
        return _no_store(response)
    context.update(
        link=link,
        url=request.build_absolute_uri(reverse("availability_link_open", args=[token])),
        expires_at=timezone.localtime(link.expires_at),
    )
    return _no_store(render(request, TPL + "link_created.html", context))


def _link_rows(school):
    at = links.now()
    rows = []
    qs = (
        AvailabilityLink.objects.filter(teacher__school=school)
        .select_related("teacher")
        .order_by("-created_at", "-pk")[:200]
    )
    for link in qs:
        status = links.link_status(link, at)
        rows.append({"link": link, "status": status, "can_revoke": status == "attivo"})
    return rows


@permission_required(can_manage_availability_links)
@require_GET
def link_list(request):
    school = current_school()
    rows = _link_rows(school) if school else []
    return _no_store(render(request, TPL + "link_list.html", {"rows": rows}))


@permission_required(can_manage_availability_links)
@require_POST
def link_revoke(request, link_id):
    school = current_school()
    link = AvailabilityLink.objects.filter(pk=link_id, teacher__school=school).first()
    if link is not None and links.revoke_link(link.pk):
        messages.success(request, "Link revocato.")
    return redirect("availability_links")


def _client_address(request):
    return request.META.get("REMOTE_ADDR", "")


NEUTRAL_MESSAGE = "Link non valido o scaduto, chiedi un nuovo link alla segreteria."


def _public(response):
    """Headers of every response of the public teacher page."""
    _no_store(response)
    response["Referrer-Policy"] = "no-referrer"
    return response


def _neutral(request, status=404, limited=False, fragment=False):
    template = "link_neutral_fragment.html" if fragment else "link_neutral.html"
    response = render(request, TPL + template, {"limited": limited}, status=status)
    if limited:
        response["Retry-After"] = str(settings.LINK_LOOKUP_WINDOW_SECONDS)
    return _public(response)


def _usable_link(request, token):
    """(link, None) when usable, else (None, neutral/429 response)."""
    address = _client_address(request)
    fragment = request.method == "POST"
    if links.is_rate_limited(address):
        return None, _neutral(request, 429, limited=True, fragment=fragment)
    link = links.lookup_link(token)
    if link is None or not link.teacher.active:
        links.record_failure(address)
        return None, _neutral(request, 404, fragment=fragment)
    return link, None


APPROVED_NOTICE = (
    "Orario gia approvato: la disponibilita di questa settimana non si puo "
    "piu modificare. Per cambiarla rivolgiti alla segreteria."
)
APPROVED_REFUSAL = (
    "La settimana e gia approvata: la disponibilita non e stata modificata."
)


def _link_page_context(link, token=None, grid=None, result=None, read_only=False):
    resolution = services.resolve_teacher_grid(link.teacher, link.week_start)
    if grid is None:
        grid = _display_grid(resolution)
    days = [
        {
            "key": day,
            "label": DAY_LABELS[day],
            "date": link.week_start + datetime.timedelta(days=n),
            "slots": [
                {
                    "index": i,
                    "available": slot["disponibile"],
                    "start": slot["ora_inizio"],
                }
                for i, slot in enumerate(grid[day])
            ],
        }
        for n, day in enumerate(grid_logic.DAYS)
    ]
    return {
        "link": link,
        "teacher": link.teacher,
        "week_end": link.week_start + datetime.timedelta(days=4),
        "days": days,
        "hours": [s["start"] for s in days[0]["slots"]],
        "grid": grid,
        "result": result,
        "read_only": read_only,
        "approved_notice": APPROVED_NOTICE,
        "save_url": reverse("availability_link_save", args=[token]) if token else "",
    }


def link_open(request, token):
    """Teacher page: the token in the URL is the only credential."""
    if request.method not in ("GET", "HEAD"):
        return _public(HttpResponseNotAllowed(["GET"]))
    link, refusal = _usable_link(request, token)
    if refusal:
        return refusal
    links.mark_used(link)
    read_only = services.week_is_approved(link.teacher.school, link.week_start)
    context = _link_page_context(link, token, read_only=read_only)
    return _public(render(request, TPL + "link_open.html", context))


def _same_origin(request):
    """Cross-site defence for the cookieless POST (no CSRF cookie exists)."""
    if request.headers.get("HX-Request") != "true":
        return False
    if request.headers.get("Sec-Fetch-Site", "same-origin") not in (
        "same-origin",
        "none",
    ):
        return False
    origin = request.headers.get("Origin")
    return origin is None or urlsplit(origin).netloc == request.get_host()


@csrf_exempt
def link_save(request, token):
    """Save the grid; teacher and week come from the link, never the request."""
    if request.method != "POST":
        # Same accounting as the page: /l/<anything>/salva/ is just a token.
        return _usable_link(request, token)[1] or _neutral(request)
    if not _same_origin(request):
        return _public(HttpResponseForbidden("Richiesta non consentita."))
    link, refusal = _usable_link(request, token)
    if refusal:
        return refusal
    with transaction.atomic():
        # Same lock as approve_timetable: a save never interleaves with it.
        if services.week_is_approved(link.teacher.school, link.week_start, lock=True):
            result = {"ok": False, "message": APPROVED_REFUSAL}
            context = _link_page_context(link, result=result)
            return _public(
                render(request, TPL + "link_result.html", context, status=409)
            )
        return _save_posted(request, link)


def _save_posted(request, link):
    try:
        grid = _posted_grid(request)
    except BadRequest:
        result = {"ok": False, "message": "Griglia non valida: nulla e stato salvato."}
        context = _link_page_context(link, result=result)
        return _public(render(request, TPL + "link_result.html", context, status=400))
    try:
        services.save_availability(link.teacher, link.week_start, grid)
    except (ValidationError, ValueError):
        result = {"ok": False, "message": "Griglia non valida: nulla e stato salvato."}
        context = _link_page_context(link, result=result)
        return _public(render(request, TPL + "link_result.html", context, status=400))
    result = {"ok": True, "message": "Disponibilita salvata. Grazie!"}
    context = _link_page_context(link, grid=grid, result=result)
    return _public(render(request, TPL + "link_result.html", context))

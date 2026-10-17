import logging

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from projects.permissions import can_run_setup, permission_required

from .. import setup_service as service
from ..forms import SetupSaveForm
from ..importers.parsers import FILE_TYPES
from ..models import School

logger = logging.getLogger(__name__)

TPL = "school/setup/"


def _ai_enabled():
    return bool(settings.ANTHROPIC_API_KEY)


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _context(request, notices=(), form=None):
    uploads = service.parked_uploads()
    issues = service.collect_issues(uploads)
    blockers = service.save_blockers(uploads, issues)
    low, high = service.calendar_bounds(uploads)
    stage_classes = []
    if "classes" in uploads:
        stage_classes = [r["nome"] for r in uploads["classes"].rows]
    return {
        "sections": service.build_sections(uploads, issues),
        "notices": list(notices),
        "blockers": blockers,
        "can_save": not blockers,
        "form": form or SetupSaveForm(existing_school=School.objects.first()),
        "replace_counts": service.replace_counts(),
        "calendar_present": "calendar" in uploads,
        "stage_classes": stage_classes,
        "calendar_low": low,
        "calendar_high": high,
        "school_exists": School.objects.exists(),
        "max_upload_bytes": settings.SETUP_MAX_UPLOAD_BYTES,
        "ai_enabled": _ai_enabled(),
    }


def _respond(request, notices=(), form=None, status=200):
    """The whole wizard: a partial for HTMX, the full page otherwise."""
    context = _context(request, notices, form)
    template = "_wizard.html" if _is_htmx(request) else "page.html"
    return render(request, TPL + template, context, status=status)


def _error_notices(exc):
    return [("error", exc.message, exc.details)]


@permission_required(can_run_setup)
@require_GET
def setup_page(request):
    return _respond(request)


@permission_required(can_run_setup)
@require_POST
def setup_upload(request, file_type):
    if file_type not in FILE_TYPES:
        raise Http404("Tipo di file sconosciuto.")
    use_ai = request.POST.get("ai") == "1"
    if use_ai and (file_type != "calendar" or not _ai_enabled()):
        notices = [("error", "La lettura con AI non è disponibile.", [])]
        return _respond(request, notices, status=422)
    uploaded = request.FILES.get("file")
    if uploaded is None:
        notices = [("error", "Seleziona un file da caricare.", [])]
        return _respond(request, notices, status=422)
    limit = settings.SETUP_MAX_UPLOAD_BYTES
    content = uploaded.read(limit + 1)
    if use_ai:
        return _ai_upload(request, uploaded.name or "", content, limit)
    try:
        source, result = service.read_upload(
            file_type, uploaded.name or "", content, limit
        )
    except service.SetupError as exc:
        if exc.no_dates and _ai_enabled():
            exc.message += " Puoi riprovare con «Leggi con AI»."
        return _respond(request, _error_notices(exc), status=422)
    service.park_upload(file_type, source, uploaded.name, result, request.user)
    label = service.LABELS[file_type]
    return _respond(
        request, [("success", f"{label}: caricate {len(result.rows)} righe.", [])]
    )


def _ai_upload(request, filename, content, limit):
    """Read the calendar PDF with the AI; the bytes are dropped on return."""
    try:
        rows = service.read_ai_calendar(filename, content, limit)
    except service.SetupError as exc:
        return _respond(request, _error_notices(exc), status=422)
    service.park_ai_calendar(filename, rows, request.user)
    text = f"Calendario: letta dall'AI una bozza di {len(rows)} righe, da controllare."
    return _respond(request, [("success", text, [])])


@permission_required(can_run_setup)
def setup_validate(request):
    return _respond(request)


@permission_required(can_run_setup)
@require_POST
def setup_correction(request):
    try:
        row = int(request.POST.get("row", ""))
    except ValueError:
        return _respond(request, [("error", "Riga non valida.", [])], status=422)
    try:
        service.apply_correction(
            request.POST.get("file_type", ""),
            row,
            request.POST.get("field", ""),
            request.POST.get("value", ""),
        )
    except service.SetupError as exc:
        return _respond(request, _error_notices(exc), status=422)
    return _respond(request, [("success", "Valore corretto.", [])])


@permission_required(can_run_setup)
@require_POST
def setup_add_date(request):
    try:
        date = service.add_manual_date(
            request.POST.get("date", ""),
            request.POST.get("max_hours", ""),
            request.POST.get("is_closure") == "on",
            request.POST.get("stage_class", ""),
        )
    except service.SetupError as exc:
        return _respond(request, _error_notices(exc), status=422)
    return _respond(request, [("success", f"Data {date:%d/%m/%Y} aggiunta.", [])])


@permission_required(can_run_setup)
@require_POST
def setup_save(request):
    form = SetupSaveForm(request.POST, existing_school=School.objects.first())
    if not form.is_valid():
        notices = [("error", "Controlla i dati della scuola.", [])]
        return _respond(request, notices, form=form, status=422)
    data = form.cleaned_data
    try:
        counts = service.save_setup(
            data["school_name"],
            data["school_year"],
            data["year_start"],
            data["year_end"],
            data.get("confirm_name"),
        )
    except service.SetupError as exc:
        return _respond(request, _error_notices(exc), form=form, status=422)
    except Exception:
        logger.exception("Setup save failed")
        notices = [
            (
                "error",
                "Salvataggio non riuscito: nessun dato è stato scritto.",
                [],
            )
        ]
        return _respond(request, notices, form=form, status=500)
    request.session["setup_replaced"] = counts.pop("sostituiti", None)
    request.session["setup_counts"] = counts
    messages.success(request, "Setup completato.")
    if _is_htmx(request):
        response = HttpResponse("")
        response["HX-Redirect"] = "/setup/completato/"
        return response
    return redirect("setup_done")


@permission_required(can_run_setup)
@require_GET
def setup_done(request):
    counts = request.session.get("setup_counts")
    if counts is None and not School.objects.exists():
        return redirect("setup")
    context = {
        "counts": counts or {},
        "replaced": request.session.get("setup_replaced"),
    }
    return render(request, TPL + "done.html", context)

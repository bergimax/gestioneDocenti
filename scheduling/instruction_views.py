"""The three htmx endpoints of the natural-language instruction box (#42)."""

from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_POST

from ai.calendar_pdf import AiError
from projects.permissions import can_use_ai_instructions, permission_required

from . import instruction
from .views import TPL, _job_context, _parse_week, _school_or_404

MSG_APPLIED = "Deroga «{label}» applicata a: {scope}. Rigenerazione avviata."
MSG_DISCARDED = "Istruzione scartata: l'orario non è stato modificato."


def _response(request, school, week, status=200, **kw):
    """The panel plus the job area (to start its polling) in one answer."""
    context = instruction.panel_context(school, week, request.user, **kw)
    context.update(_job_context(school, week, request.user))
    context["job_oob"] = True
    return render(request, TPL + "_instruction_response.html", context, status=status)


def _panel(request, school, week, status=200, **kw):
    context = instruction.panel_context(school, week, request.user, **kw)
    return render(request, TPL + "_instruction.html", context, status=status)


def _setup(week):
    if not instruction.feature_on():
        raise Http404("Funzione non attiva.")
    school = _school_or_404()
    return school, _parse_week(school, week)


@permission_required(can_use_ai_instructions)
@require_POST
def instruction_create(request, week):
    school, week = _setup(week)
    text = request.POST.get("testo", "")
    try:
        result = instruction.create_instruction(
            school, week["start"], request.user, text
        )
    except instruction.InstructionRefused as exc:
        return _panel(request, school, week, status=409, error=str(exc), text=text)
    except AiError as exc:
        return _panel(request, school, week, error=str(exc.args[0]), text=text)
    if isinstance(result, str):
        return _panel(request, school, week, question=result, text=text)
    return _panel(request, school, week, draft=result, text=text)


@permission_required(can_use_ai_instructions)
@require_POST
def instruction_confirm(request, week, draft_id):
    school, week = _setup(week)
    try:
        label, scope = instruction.confirm_instruction(
            school, week["start"], request.user, draft_id
        )
    except instruction.InstructionRefused as exc:
        return _panel(request, school, week, status=409, error=str(exc))
    message = MSG_APPLIED.format(label=label, scope=scope)
    return _response(request, school, week, message=message)


@permission_required(can_use_ai_instructions)
@require_POST
def instruction_discard(request, week, draft_id):
    school, week = _setup(week)
    try:
        instruction.discard_instruction(school, week["start"], request.user, draft_id)
    except instruction.InstructionRefused as exc:
        return _panel(request, school, week, status=409, error=str(exc))
    return _panel(request, school, week, message=MSG_DISCARDED)

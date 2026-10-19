"""The setup wizard's logic (specs section 5): parked uploads, live
corrections, the manual calendar date and the single-transaction save.

Parsed files live in the `ParkedUpload` table, never in process memory, so a
restart in the middle of a setup loses nothing. No module-level state.
"""

import csv
import datetime
import io
import logging
import re

import pdfplumber
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import connection, transaction
from django.utils import timezone

from ai.calendar_pdf import AiError, read_calendar_pdf
from availability.models import Availability, AvailabilityLink
from feeds.models import FeedToken
from ledger.models import LedgerEntry
from scheduling.audit import SETUP_SAVED, record_audit
from scheduling.models import SolveJob, Timetable, TimetableState

from .importers import (
    CalendarPdfError,
    CalendarPdfNoDatesError,
    normalize_name,
    parse_assignments,
    parse_calendar,
    parse_calendar_pdf,
    parse_classes,
    parse_pairings,
    parse_subjects,
    parse_teachers,
    validate_all,
)
from .importers.parsers import (
    FILE_TYPES,
    GROUPS,
    STAGE_ORE_GIORNATA,
    TRUE_VALUES,
    calendar_hours_error,
    hours_message,
)
from .models import (
    Assignment,
    CalendarEntry,
    Class,
    ClassPairing,
    Draft,
    ParkedUpload,
    School,
    Subject,
    Teacher,
)

logger = logging.getLogger(__name__)

SETUP_LOCK_ID = 7_160_016

LABELS = {
    "calendar": "Calendario",
    "teachers": "Docenti",
    "classes": "Classi",
    "subjects": "Materie",
    "assignments": "Assegnazioni",
    "pairings": "Accoppiamenti",
}

# Row keys equal the CSV column names, so a cell edit is a CSV round trip.
COLUMNS = {
    "calendar": (
        "data",
        "gruppo",
        "ore_max_giornata",
        "flag_chiusura",
        "stage_classe_id",
    ),
    "teachers": ("nome", "email", "tipo"),
    "classes": ("nome", "n_studenti", "gruppo"),
    "subjects": ("nome", "tipo", "peso_cognitivo"),
    "assignments": ("docente_nome", "classe_nome", "materia_nome", "ore_anno"),
    "pairings": ("classe_a", "classe_b", "materia_nome", "note"),
}
COLUMN_LABELS = {
    "data": "Data",
    "gruppo": "Gruppo",
    "ore_max_giornata": "Ore massime",
    "flag_chiusura": "Chiusura",
    "stage_classe_id": "Classe in stage",
    "nome": "Nome",
    "email": "Email",
    "tipo": "Tipo",
    "n_studenti": "Studenti",
    "peso_cognitivo": "Peso cognitivo",
    "docente_nome": "Docente",
    "classe_nome": "Classe",
    "materia_nome": "Materia",
    "ore_anno": "Ore annue",
    "classe_a": "Classe A",
    "classe_b": "Classe B",
    "note": "Note",
}
PARSERS = {
    "calendar": parse_calendar,
    "teachers": parse_teachers,
    "classes": parse_classes,
    "subjects": parse_subjects,
    "assignments": parse_assignments,
    "pairings": parse_pairings,
}
# Rows read from a PDF or by the AI may carry 0 hours on a lesson row, which the
# CSV parser refuses; when the shared hours rule accepts such a value it is
# replaced by this placeholder only to re-parse the row's other cells.
_PLACEHOLDER_HOURS = "6"
_INT_MAX = 2_147_483_647
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class SetupError(Exception):
    """A rejected setup action; `details` lists the specific problems."""

    def __init__(self, message, details=(), no_dates=False):
        super().__init__(message)
        self.message = message
        self.details = list(details)
        # The deterministic PDF parser found no date row (an AI read may help).
        self.no_dates = no_dates


# --- rows <-> JSON ------------------------------------------------------------


def _dump_rows(file_type, rows):
    out = []
    for row in rows:
        row = dict(row)
        if isinstance(row.get("data"), datetime.date):
            row["data"] = row["data"].isoformat()
        out.append(row)
    return out


def load_rows(upload):
    rows = []
    for row in upload.rows:
        row = dict(row)
        if upload.file_type == "calendar" and isinstance(row.get("data"), str):
            # An AI row may hold a date that does not exist; it stays a string
            # and the validator lists it on its row.
            try:
                row["data"] = datetime.date.fromisoformat(row["data"])
            except ValueError:
                pass
        rows.append(row)
    return rows


def _message_dicts(file_type, messages):
    return [
        {"severity": m.severity, "row": m.row, "text": m.text}
        for m in messages
        if m.severity in ("error", "warning")
    ]


def parked_uploads():
    return {u.file_type: u for u in ParkedUpload.objects.all()}


# --- upload -------------------------------------------------------------------


def accepted_extensions(file_type):
    return (".csv", ".pdf") if file_type == "calendar" else (".csv",)


def _rejected(message, details=()):
    return SetupError(message, details)


def read_upload(file_type, filename, content, max_bytes):
    """Parse an uploaded file into (source, ParseResult); raise SetupError.

    Nothing is written: the caller parks the result only on success.
    """
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed = accepted_extensions(file_type)
    if suffix not in allowed:
        raise _rejected(
            f"Estensione non valida per «{filename}»: sono accettati i file "
            + ", ".join(allowed)
            + "."
        )
    if len(content) > max_bytes:
        raise _rejected(
            f"Il file «{filename}» supera la dimensione massima consentita "
            f"({max_bytes} byte)."
        )
    if not content.strip():
        raise _rejected(f"Il file «{filename}» è vuoto.")
    if suffix == ".pdf":
        try:
            result = parse_calendar_pdf(content)
        except CalendarPdfError as exc:
            error = _rejected(f"PDF del calendario non utilizzabile: {exc}")
            error.no_dates = isinstance(exc, CalendarPdfNoDatesError)
            raise error from exc
        for number, row in enumerate(result.rows, start=1):
            row["row"] = number
        return "pdf", result
    result = PARSERS[file_type](content)
    errors = [m for m in result.messages if m.severity == "error"]
    if errors and (not result.rows or any(m.row is None for m in errors)):
        raise _rejected(
            f"Il file «{filename}» non è valido e non è stato caricato.",
            [m.text for m in errors[:10]],
        )
    return "csv", result


def park_upload(file_type, source, filename, result, user=None):
    """Store a parsed file, replacing the previous one of the same type."""
    rows = _dump_rows(file_type, result.rows)
    messages = _message_dicts(file_type, result.messages)
    with transaction.atomic():
        if file_type == "calendar":
            _discard_calendar_drafts()
        upload, _ = ParkedUpload.objects.update_or_create(
            file_type=file_type,
            defaults={
                "source": source,
                "filename": filename[:255],
                "rows": rows,
                "messages": messages,
                "uploaded_by": user if getattr(user, "pk", None) else None,
            },
        )
    return upload


# --- AI calendar read ---------------------------------------------------------


def _discard_calendar_drafts():
    Draft.objects.filter(
        kind=Draft.Kind.CALENDAR_ROWS, status=Draft.Status.DRAFT
    ).update(status=Draft.Status.DISCARDED)


def read_ai_calendar(filename, content, max_bytes):
    """Read the calendar PDF with the AI; return the proposed rows.

    Raise SetupError for an unusable upload, an AI failure or an empty
    result. Nothing is written and the bytes are not kept anywhere.
    """
    if not filename.lower().endswith(".pdf"):
        raise _rejected(f"Estensione non valida per «{filename}»: serve un file .pdf.")
    if len(content) > max_bytes:
        raise _rejected(
            f"Il file «{filename}» supera la dimensione massima consentita "
            f"({max_bytes} byte)."
        )
    if not content.strip():
        raise _rejected(f"Il file «{filename}» è vuoto.")
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = len(pdf.pages)
    except Exception:
        raise _rejected(f"Il file «{filename}» non è un PDF leggibile.") from None
    if pages == 0:
        raise _rejected(f"Il PDF «{filename}» non contiene pagine.")
    try:
        rows = read_calendar_pdf(content)
    except AiError as exc:
        raise _rejected(str(exc)) from None
    except Exception as exc:
        # Never log the message: it could echo the request.
        logger.error("AI calendar read failed: %s", type(exc).__name__)
        raise _rejected(
            "Lettura con AI non riuscita: riprova o carica il calendario in CSV."
        ) from None
    if not rows:
        raise _rejected(f"Lettura con AI di «{filename}»: nessuna riga letta.")
    return rows


def park_ai_calendar(filename, rows, user=None):
    """Park the AI rows as the calendar upload and record them as one DRAFT.

    The previous calendar DRAFT, if any, is DISCARDED in the same transaction.
    """
    owner = user if getattr(user, "pk", None) else None
    parked = []
    for number, row in enumerate(rows, start=1):
        row = dict(row)
        if isinstance(row.get("gruppo"), str):
            row["gruppo"] = row["gruppo"].strip().upper() or None
        row["row"] = number
        parked.append(row)
    with transaction.atomic():
        _discard_calendar_drafts()
        Draft.objects.create(
            kind=Draft.Kind.CALENDAR_ROWS,
            payload=_dump_rows("calendar", rows),
            source=filename[:255],
            created_by=owner,
        )
        upload, _ = ParkedUpload.objects.update_or_create(
            file_type="calendar",
            defaults={
                "source": "ai",
                "filename": filename[:255],
                "rows": _dump_rows("calendar", parked),
                "messages": [],
                "uploaded_by": owner,
            },
        )
    return upload


def _structured_hours_error(row):
    """The shared hours rule applied to a PDF or AI row."""
    return calendar_hours_error(
        row.get("ore_max_giornata"),
        gruppo=row.get("gruppo"),
        chiusura=bool(row.get("flag_chiusura")),
        stage=bool(row.get("flag_stage_gruppo")),
        structured=True,
        stage_class=bool(row.get("stage_classe_id")),
    )


def _csv_hours_error(row):
    """The hours rule as the CSV parser applies it to this row."""
    return calendar_hours_error(
        row.get("ore_max_giornata"),
        gruppo=row.get("gruppo"),
        chiusura=bool(row.get("flag_chiusura")),
        stage=False,
        structured=False,
        stage_class=bool(row.get("stage_classe_id")),
    )


def _is_valid_ai_row(row):
    return (
        isinstance(row.get("data"), datetime.date)
        and row.get("gruppo") in (*GROUPS, None)
        and _structured_hours_error(row) is None
    )


def _pdf_hours_messages(rows):
    return [
        {"severity": "error", "row": r["row"], "text": text}
        for r in rows
        if (text := _structured_hours_error(r))
    ]


def _ai_calendar_messages(rows):
    """Errors for AI rows: the CSV validator (#14) on the rows rendered as CSV
    (hours the shared rule accepts are masked there), plus the hours rule."""
    messages = []
    text, lines = _csv_text("calendar", rows, None, None, None)
    row_of = dict(zip(lines, (r["row"] for r in rows), strict=True))
    for m in parse_calendar(text).messages:
        if m.severity != "error":
            continue
        body = re.sub(
            r"duplicate of row (\d+)",
            lambda g: f"duplicate of row {row_of.get(int(g.group(1)), g.group(1))}",
            m.text,
        )
        messages.append({"severity": "error", "row": row_of.get(m.row), "text": body})
    seen = {(m["row"], m["text"]) for m in messages}
    for r in rows:
        text = _structured_hours_error(r)
        if text and (r["row"], text) not in seen:
            messages.append({"severity": "error", "row": r["row"], "text": text})
        elif not text and r.get("flag_stage_gruppo") and not r.get("gruppo"):
            messages.append(
                {
                    "severity": "error",
                    "row": r["row"],
                    "text": "lo stage di gruppo richiede un gruppo",
                }
            )
    return messages


def _ai_cell_value(field, value):
    """The stored value for a corrected cell of an AI row; SetupError if bad."""
    if field == "data":
        if _DATE.fullmatch(value):
            try:
                return datetime.date.fromisoformat(value).isoformat()
            except ValueError:
                pass
        raise SetupError("Data non valida: usa il formato AAAA-MM-GG.")
    if field == "gruppo":
        if value and value.upper() not in GROUPS:
            raise SetupError("Gruppo non valido: " + ", ".join(GROUPS) + ".")
        return value.upper() or None
    if field == "ore_max_giornata":
        # Not a number: kept as text, refused by the hours rule with the raw value.
        return int(value) if value.isascii() and value.isdigit() else value
    if field == "flag_chiusura":
        return value.lower() in TRUE_VALUES
    return value or None


# --- validation ---------------------------------------------------------------


def _model_checks(file_type, rows):
    """Errors for values the CSV parsers accept but the database cannot hold."""
    errors = []

    def err(row, text):
        errors.append({"severity": "error", "row": row["row"], "text": text})

    for row in rows:
        if file_type == "teachers":
            if len(row["nome"]) > 255:
                err(row, "nome is longer than 255 characters")
            if row["email"]:
                try:
                    validate_email(row["email"])
                except ValidationError:
                    err(row, f"email '{row['email']}' is not a valid address")
        elif file_type == "classes":
            if len(row["nome"]) > 50:
                err(row, "nome is longer than 50 characters")
            if (row["n_studenti"] or 0) > _INT_MAX:
                err(row, "n_studenti is too large")
        elif file_type == "subjects" and len(row["nome"]) > 255:
            err(row, "nome is longer than 255 characters")
        elif file_type == "assignments" and row["ore_anno"] > _INT_MAX:
            err(row, "ore_anno is too large")
    return errors


def collect_issues(uploads):
    """Errors and warnings per file type: parser messages, model checks and
    the cross-file validation of every file present."""
    issues = {t: {"errors": [], "warnings": []} for t in FILE_TYPES}
    parsed = {}
    for file_type, upload in uploads.items():
        rows = load_rows(upload)
        own = upload.messages
        if file_type == "calendar" and upload.source == "ai":
            own = _ai_calendar_messages(rows)
            rows = [r for r in rows if _is_valid_ai_row(r)]
        elif file_type == "calendar" and upload.source == "pdf":
            own = [*own, *_pdf_hours_messages(rows)]
        parsed[file_type] = rows
        for m in [*own, *_model_checks(file_type, rows)]:
            key = "errors" if m["severity"] == "error" else "warnings"
            issues[file_type][key].append({"row": m["row"], "text": m["text"]})
    if len(parsed) > 1:
        result = validate_all(parsed)
        for key, messages in (("errors", result.errors), ("warnings", result.warnings)):
            for m in messages:
                issues[m.file_type][key].append({"row": m.row, "text": m.text})
    for item in issues.values():
        for key in ("errors", "warnings"):
            item[key].sort(key=lambda m: (m["row"] is None, m["row"] or 0, m["text"]))
    return issues


def save_blockers(uploads, issues):
    """Reasons the setup cannot be saved yet (empty list = ready)."""
    reasons = []
    missing = [LABELS[t] for t in FILE_TYPES if t not in uploads]
    if missing:
        reasons.append("Mancano ancora i file: " + ", ".join(missing) + ".")
    count = sum(len(i["errors"]) for i in issues.values())
    if count:
        reasons.append(f"Ci sono {count} errori da correggere prima di salvare.")
    reasons.extend(replace_blockers())
    return reasons


def replace_blockers():
    """Why an existing school cannot be replaced (empty list = allowed)."""
    if not School.objects.exists():
        return []
    parts = []
    approved = Timetable.objects.filter(state=TimetableState.APPROVATO).count()
    if approved:
        parts.append(
            f"{approved} orario approvato"
            if approved == 1
            else f"{approved} orari approvati"
        )
    movements = LedgerEntry.objects.count()
    if movements:
        parts.append(
            f"{movements} movimento nel registro ore"
            if movements == 1
            else f"{movements} movimenti nel registro ore"
        )
    if not parts:
        return []
    return [
        "I dati della scuola non possono essere sostituiti: esistono "
        + ", ".join(parts)
        + "."
    ]


def replace_counts():
    """What a replace would delete, for the confirmation panel."""
    if not School.objects.exists():
        return {}
    return {
        "Classi": Class.objects.count(),
        "Docenti": Teacher.objects.count(),
        "Materie": Subject.objects.count(),
        "Righe di calendario": CalendarEntry.objects.count(),
        "Assegnazioni": Assignment.objects.count(),
        "Accoppiamenti": ClassPairing.objects.count(),
        "Orari": Timetable.objects.count(),
        "Disponibilità": Availability.objects.count(),
    }


def _delete_school_data():
    """Delete everything that hangs off the school, never the School row."""
    SolveJob.objects.all().delete()  # alternative picks go with their job
    Timetable.objects.all().delete()  # slots, decisions; audit/feedback -> NULL
    Availability.objects.all().delete()
    AvailabilityLink.objects.all().delete()
    FeedToken.objects.all().delete()
    ClassPairing.objects.all().delete()
    Assignment.objects.all().delete()
    CalendarEntry.objects.all().delete()
    Class.objects.all().delete()
    Teacher.objects.all().delete()
    Subject.objects.all().delete()


# --- sections for the template ------------------------------------------------


def _cell(value):
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)


def build_sections(uploads, issues):
    sections = []
    for file_type in FILE_TYPES:
        upload = uploads.get(file_type)
        section = {
            "type": file_type,
            "label": LABELS[file_type],
            "accept": ",".join(accepted_extensions(file_type)),
            "columns": [(c, COLUMN_LABELS[c]) for c in COLUMNS[file_type]],
            "errors": issues[file_type]["errors"],
            "warnings": issues[file_type]["warnings"],
            "upload": upload,
            "is_ai": bool(upload and upload.source == "ai"),
            "rows": [],
        }
        if upload:
            rows = load_rows(upload)
            section["row_count"] = len(rows)
            for row in rows:
                section["rows"].append(
                    {
                        "row": row["row"],
                        "stage": bool(row.get("flag_stage_gruppo")),
                        "cells": [
                            {"field": c, "value": _cell(row.get(c))}
                            for c in COLUMNS[file_type]
                        ],
                    }
                )
        sections.append(section)
    return sections


# --- live corrections ---------------------------------------------------------


def _csv_text(file_type, rows, edited_index, field, value):
    """CSV of all rows with one cell replaced; also each row's first line."""
    columns = COLUMNS[file_type]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    lines = []
    for index, row in enumerate(rows):
        lines.append(out.getvalue().count("\n") + 1)
        fields = [_cell(row.get(c)) for c in columns]
        if (
            file_type == "calendar"
            and _csv_hours_error(row)
            and _structured_hours_error(row) is None
        ):
            fields[columns.index("ore_max_giornata")] = _PLACEHOLDER_HOURS
        if index == edited_index:
            fields[columns.index(field)] = value
        writer.writerow(fields)
    return out.getvalue(), lines


def apply_correction(file_type, row_number, field, value):
    """Replace one cell of a parked file after re-parsing it; SetupError if the
    new value is invalid (nothing is stored then)."""
    if file_type not in COLUMNS or field not in COLUMNS[file_type]:
        raise SetupError("Campo non modificabile.")
    value = value.strip()
    with transaction.atomic():
        upload = (
            ParkedUpload.objects.select_for_update().filter(file_type=file_type).first()
        )
        if upload is None:
            raise SetupError("Il file non è stato caricato.")
        rows = load_rows(upload)
        index = next((i for i, r in enumerate(rows) if r["row"] == row_number), None)
        if index is None:
            raise SetupError("Riga non trovata.")
        old = rows[index]
        if file_type == "calendar" and old.get("flag_stage_gruppo"):
            if field == "gruppo" and not value:
                raise SetupError("Lo stage di gruppo richiede un gruppo.")
            if field == "stage_classe_id" and value:
                raise SetupError(
                    "Una riga di stage di gruppo non può avere una classe in stage."
                )
        structured_hours = (
            file_type == "calendar"
            and field == "ore_max_giornata"
            and upload.source in ("ai", "pdf")
        )
        if structured_hours or (file_type == "calendar" and upload.source == "ai"):
            # A row may hold several bad cells; only the edited one is checked.
            new = {**old, field: _ai_cell_value(field, value)}
            if field == "ore_max_giornata":
                new["flag_stage_gruppo"] = bool(
                    new[field] == STAGE_ORE_GIORNATA and new.get("gruppo")
                )
                if text := _structured_hours_error(new):
                    raise SetupError(
                        hours_message(value) if "ammessi" in text else text
                    )
            rows[index] = new
            upload.rows = _dump_rows(file_type, rows)
            upload.save(update_fields=["rows", "updated_at"])
            return upload
        text, lines = _csv_text(file_type, rows, index, field, value)
        line = lines[index]
        result = PARSERS[file_type](text)
        problems = [
            m.text
            for m in result.messages
            if m.severity == "error"
            and (m.row == line or m.text == f"duplicate of row {line}")
        ]
        parsed = next((r for r in result.rows if r["row"] == line), None)
        if problems or parsed is None:
            raise SetupError(
                f"Valore non valido per «{COLUMN_LABELS[field]}»: non salvato.",
                problems,
            )
        rows[index] = {**old, field: parsed[field]}
        if file_type == "calendar":
            rows[index]["flag_stage_gruppo"] = parsed["flag_stage_gruppo"]
        upload.rows = _dump_rows(file_type, rows)
        upload.save(update_fields=["rows", "updated_at"])
    return upload


# --- manual calendar date -----------------------------------------------------


def add_manual_date(date_text, hours_text, is_closure, stage_class_text):
    """Add a whole-school calendar row to the parked calendar."""
    date = None
    if _DATE.fullmatch(date_text.strip()):
        try:
            date = datetime.date.fromisoformat(date_text.strip())
        except ValueError:
            pass
    if date is None:
        raise SetupError("Data non valida: usa il formato AAAA-MM-GG.")
    hours_text = hours_text.strip()
    if not hours_text.isascii() or not hours_text.isdigit():
        hours = None
    else:
        hours = int(hours_text)
    if text := calendar_hours_error(
        hours if hours is not None else hours_text,
        gruppo=None,
        chiusura=bool(is_closure),
        stage=False,
        structured=False,
        stage_class=bool(stage_class_text.strip()),
        shown=hours_text,
    ):
        raise SetupError(text)
    with transaction.atomic():
        calendar = (
            ParkedUpload.objects.select_for_update()
            .filter(file_type="calendar")
            .first()
        )
        if calendar is None:
            raise SetupError("Carica prima il calendario.")
        stage = None
        stage_class_text = stage_class_text.strip()
        if stage_class_text:
            classes = ParkedUpload.objects.filter(file_type="classes").first()
            known = {
                normalize_name(r["nome"]): r["nome"]
                for r in (classes.rows if classes else [])
            }
            stage = known.get(normalize_name(stage_class_text))
            if stage is None:
                raise SetupError(
                    "La classe in stage non è presente nel file delle classi caricato."
                )
        rows = load_rows(calendar)
        if any(r["data"] == date for r in rows):
            raise SetupError(f"Il {date.isoformat()} è già presente nel calendario.")
        rows.append(
            {
                "data": date,
                "gruppo": None,
                "ore_max_giornata": hours,
                "flag_chiusura": bool(is_closure),
                "stage_classe_id": stage,
                "flag_stage_gruppo": False,
                "row": max((r["row"] for r in rows), default=0) + 1,
            }
        )
        rows.sort(key=lambda r: (_cell(r["data"]), r["row"]))
        calendar.rows = _dump_rows("calendar", rows)
        calendar.save(update_fields=["rows", "updated_at"])
    return date


# --- purge of abandoned uploads ------------------------------------------------


def purge_parked_uploads(max_age_hours, dry_run=False):
    """Delete parked uploads whose `updated_at` is older than `max_age_hours`.

    Takes the setup advisory lock, so it never races a save or a replace.
    Touches only ParkedUpload. Returns the file types (sorted) of the stale
    rows; with `dry_run` nothing is deleted.
    """
    cutoff = timezone.now() - datetime.timedelta(hours=max_age_hours)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [SETUP_LOCK_ID])
        stale = ParkedUpload.objects.filter(updated_at__lt=cutoff)
        file_types = sorted(stale.values_list("file_type", flat=True))
        if file_types and not dry_run:
            stale.delete()
    return file_types


# --- save ---------------------------------------------------------------------


def calendar_bounds(uploads):
    upload = uploads.get("calendar")
    dates = [
        r["data"]
        for r in (load_rows(upload) if upload else [])
        if isinstance(r["data"], datetime.date)
    ]
    return (min(dates), max(dates)) if dates else (None, None)


def _full_clean(instance, label, row):
    try:
        instance.full_clean(validate_unique=False, validate_constraints=False)
    except ValidationError as exc:
        raise SetupError(f"{label} riga {row}: {' '.join(exc.messages)}") from exc


def _create_school(name, year, start, end):
    school = School.objects.first() or School()
    school.name, school.formative_year = name, year
    school.year_start, school.year_end = start, end
    school.full_clean()
    school.save()
    return school


def _create_classes(school, rows):
    created = {}
    for r in rows:
        obj = Class(
            school=school,
            name=r["nome"],
            student_count=r["n_studenti"] or 0,
            group=r["gruppo"] or "",
        )
        _full_clean(obj, "Classi", r["row"])
        created[normalize_name(r["nome"])] = obj
    Class.objects.bulk_create(created.values())
    return created


def _create_teachers(school, rows):
    created = {}
    for r in rows:
        obj = Teacher(
            school=school, name=r["nome"], email=r["email"] or "", type=r["tipo"]
        )
        _full_clean(obj, "Docenti", r["row"])
        created[normalize_name(r["nome"])] = obj
    Teacher.objects.bulk_create(created.values())
    return created


def _create_subjects(school, rows):
    created = {}
    for r in rows:
        obj = Subject(
            school=school,
            name=r["nome"],
            kind=r["tipo"],
            cognitive_weight=r["peso_cognitivo"],
        )
        _full_clean(obj, "Materie", r["row"])
        created[normalize_name(r["nome"])] = obj
    Subject.objects.bulk_create(created.values())
    return created


def _create_calendar(school, rows, classes):
    entries = []
    for r in rows:
        stage = r.get("stage_classe_id")
        entry = CalendarEntry(
            school=school,
            date=r["data"],
            group=r["gruppo"],
            max_hours=r["ore_max_giornata"],
            is_closure=r["flag_chiusura"],
            stage_class=classes[normalize_name(stage)] if stage else None,
            is_group_stage=bool(r.get("flag_stage_gruppo")),
        )
        _full_clean(entry, "Calendario", r["row"])
        entries.append(entry)
    CalendarEntry.objects.bulk_create(entries)
    return entries


def _create_assignments(school, rows, teachers, classes, subjects):
    objs = []
    for r in rows:
        obj = Assignment(
            school=school,
            teacher=teachers[normalize_name(r["docente_nome"])],
            school_class=classes[normalize_name(r["classe_nome"])],
            subject=subjects[normalize_name(r["materia_nome"])],
            ore_totali=r["ore_anno"],
        )
        _full_clean(obj, "Assegnazioni", r["row"])
        objs.append(obj)
    Assignment.objects.bulk_create(objs)
    return objs


def _pairing_teacher(assignments, a, b, subject):
    """The one teacher who teaches `subject` in both classes, or None."""

    def teachers(klass):
        return {
            x.teacher_id
            for x in assignments
            if x.school_class_id == klass.pk and x.subject_id == subject.pk
        }

    common = teachers(a) & teachers(b)
    return common.pop() if len(common) == 1 else None


def _create_pairings(school, rows, classes, subjects, assignments, teachers):
    objs = []
    teachers_by_pk = {t.pk: t for t in teachers.values()}
    for r in rows:
        a = classes[normalize_name(r["classe_a"])]
        b = classes[normalize_name(r["classe_b"])]
        if a.pk > b.pk:
            a, b = b, a
        subject = subjects[normalize_name(r["materia_nome"])]
        teacher_id = _pairing_teacher(assignments, a, b, subject)
        if teacher_id is None:
            raise SetupError(
                f"Accoppiamenti riga {r['row']}: serve un solo docente che "
                f"insegni {subject} sia in {a} sia in {b}."
            )
        obj = ClassPairing(
            school=school,
            class_a=a,
            class_b=b,
            subject=subject,
            teacher=teachers_by_pk[teacher_id],
            note=r["note"] or "",
        )
        _full_clean(obj, "Accoppiamenti", r["row"])
        objs.append(obj)
    ClassPairing.objects.bulk_create(objs)
    return objs


def save_setup(name, year, start, end, confirm_name=None, user=None):
    """Create everything from the parked uploads in one transaction.

    With a school present this is the replace: `confirm_name` must equal the
    current school name, and all school data is deleted first (the School row
    is kept and updated).

    Returns the created counts. On any error nothing is written and the
    parked uploads stay where they are.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [SETUP_LOCK_ID])
        uploads = {
            u.file_type: u for u in ParkedUpload.objects.select_for_update().all()
        }
        existing = School.objects.first()
        if existing and (confirm_name or "").strip() != existing.name:
            raise SetupError(
                "Conferma non valida: scrivi il nome attuale della scuola."
            )
        reasons = save_blockers(uploads, collect_issues(uploads))
        if reasons:
            raise SetupError("Il setup non può essere salvato.", reasons)
        low, high = calendar_bounds(uploads)
        start, end = start or low, end or high
        if start is None or end is None:
            raise SetupError("Il calendario non contiene date.")
        if end <= start:
            raise SetupError("La fine dell'anno deve essere dopo l'inizio.")
        rows = {t: load_rows(u) for t, u in uploads.items()}
        replaced = replace_counts()
        if existing:
            _delete_school_data()
        school = _create_school(name, year, start, end)
        classes = _create_classes(school, rows["classes"])
        teachers = _create_teachers(school, rows["teachers"])
        subjects = _create_subjects(school, rows["subjects"])
        calendar = _create_calendar(school, rows["calendar"], classes)
        assignments = _create_assignments(
            school, rows["assignments"], teachers, classes, subjects
        )
        pairings = _create_pairings(
            school, rows["pairings"], classes, subjects, assignments, teachers
        )
        if uploads["calendar"].source == "ai":
            Draft.objects.filter(
                kind=Draft.Kind.CALENDAR_ROWS, status=Draft.Status.DRAFT
            ).update(status=Draft.Status.CONFIRMED)
        ParkedUpload.objects.all().delete()
        record_audit(
            SETUP_SAVED,
            user,
            None,
            {
                "replaced": bool(existing),
                "created": {
                    "classes": len(classes),
                    "teachers": len(teachers),
                    "subjects": len(subjects),
                    "calendar": len(calendar),
                    "assignments": len(assignments),
                    "pairings": len(pairings),
                },
                "deleted": replaced,
            },
        )
    result = {
        "Classi": len(classes),
        "Docenti": len(teachers),
        "Materie": len(subjects),
        "Righe di calendario": len(calendar),
        "Assegnazioni": len(assignments),
        "Accoppiamenti": len(pairings),
    }
    if replaced:
        result["sostituiti"] = replaced
    return result

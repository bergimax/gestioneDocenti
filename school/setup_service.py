"""The setup wizard's logic (specs section 5): parked uploads, live
corrections, the manual calendar date and the single-transaction save.

Parsed files live in the `ParkedUpload` table, never in process memory, so a
restart in the middle of a setup loses nothing. No module-level state.
"""

import csv
import datetime
import io
import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import connection, transaction

from .importers import (
    CalendarPdfError,
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
from .importers.parsers import FILE_TYPES, MAX_HOURS_VALUES
from .models import (
    Assignment,
    CalendarEntry,
    Class,
    ClassPairing,
    ParkedUpload,
    School,
    Subject,
    Teacher,
)

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
# Calendar rows from the PDF carry an hours value of 0 for empty cells, which
# the CSV parser would refuse; they are replaced only to re-parse a row.
_PLACEHOLDER_HOURS = "6"
_INT_MAX = 2_147_483_647
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class SetupError(Exception):
    """A rejected setup action; `details` lists the specific problems."""

    def __init__(self, message, details=()):
        super().__init__(message)
        self.message = message
        self.details = list(details)


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
            row["data"] = datetime.date.fromisoformat(row["data"])
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
            raise _rejected(f"PDF del calendario non utilizzabile: {exc}") from exc
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
        parsed[file_type] = rows
        for m in [*upload.messages, *_model_checks(file_type, rows)]:
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
    if School.objects.exists():
        reasons.append("Esiste già una scuola: il setup non può essere rieseguito.")
    return reasons


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
        if file_type == "calendar" and row.get("ore_max_giornata") not in (
            MAX_HOURS_VALUES
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
    if hours not in MAX_HOURS_VALUES:
        raise SetupError("Le ore massime devono essere 4, 5 o 6.")
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
        rows.sort(key=lambda r: (r["data"], r["row"]))
        calendar.rows = _dump_rows("calendar", rows)
        calendar.save(update_fields=["rows", "updated_at"])
    return date


# --- save ---------------------------------------------------------------------


def calendar_bounds(uploads):
    upload = uploads.get("calendar")
    dates = [r["data"] for r in load_rows(upload)] if upload else []
    return (min(dates), max(dates)) if dates else (None, None)


def _full_clean(instance, label, row):
    try:
        instance.full_clean(validate_unique=False, validate_constraints=False)
    except ValidationError as exc:
        raise SetupError(f"{label} riga {row}: {' '.join(exc.messages)}") from exc


def _create_school(name, year, start, end):
    school = School(name=name, formative_year=year, year_start=start, year_end=end)
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


def _create_pairings(school, rows, classes, subjects):
    objs = []
    for r in rows:
        a = classes[normalize_name(r["classe_a"])]
        b = classes[normalize_name(r["classe_b"])]
        if a.pk > b.pk:
            a, b = b, a
        obj = ClassPairing(
            school=school,
            class_a=a,
            class_b=b,
            subject=subjects[normalize_name(r["materia_nome"])],
            teacher=None,
            note=r["note"] or "",
        )
        _full_clean(obj, "Accoppiamenti", r["row"])
        objs.append(obj)
    ClassPairing.objects.bulk_create(objs)
    return objs


def save_setup(name, year, start, end):
    """Create everything from the parked uploads in one transaction.

    Returns the created counts. On any error nothing is written and the
    parked uploads stay where they are.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [SETUP_LOCK_ID])
        uploads = {
            u.file_type: u for u in ParkedUpload.objects.select_for_update().all()
        }
        if School.objects.exists():
            raise SetupError(
                "Esiste già una scuola: il setup non può essere rieseguito."
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
        school = _create_school(name, year, start, end)
        classes = _create_classes(school, rows["classes"])
        teachers = _create_teachers(school, rows["teachers"])
        subjects = _create_subjects(school, rows["subjects"])
        calendar = _create_calendar(school, rows["calendar"], classes)
        assignments = _create_assignments(
            school, rows["assignments"], teachers, classes, subjects
        )
        pairings = _create_pairings(school, rows["pairings"], classes, subjects)
        ParkedUpload.objects.all().delete()
    return {
        "Classi": len(classes),
        "Docenti": len(teachers),
        "Materie": len(subjects),
        "Righe di calendario": len(calendar),
        "Assegnazioni": len(assignments),
        "Accoppiamenti": len(pairings),
    }

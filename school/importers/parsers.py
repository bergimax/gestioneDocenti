"""CSV parsers for the six setup files (specs section 5.1). Pure: stdlib only."""

import csv
import datetime
import io
import re
from dataclasses import dataclass, field

GROUPS = ("PRIME", "SECONDE", "TERZE", "QUARTE", "PRIMA4+2")
TEACHER_TYPES = ("ASSUNTO", "CONTRATTO")
SUBJECT_TYPES = ("TEORIA", "PRATICA")
COGNITIVE_WEIGHTS = ("ALTO", "MEDIO", "BASSO")
MAX_HOURS_VALUES = (4, 5, 6)
TRUE_VALUES = ("true", "1", "yes")

FILE_TYPES = (
    "calendar",
    "teachers",
    "classes",
    "subjects",
    "assignments",
    "pairings",
)

_DIGITS = re.compile(r"[0-9]+")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class Message:
    severity: str
    file_type: str
    row: int | None
    text: str


@dataclass
class ParseResult:
    rows: list = field(default_factory=list)
    messages: list = field(default_factory=list)


def normalize_name(value):
    """Strip, collapse whitespace runs, casefold. Used for matching names."""
    return " ".join(str(value).split()).casefold()


def _error(file_type, row, text):
    return Message("error", file_type, row, text)


def _decode(content):
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return content.lstrip("﻿")


def _read_records(text):
    """Yield (line_number, fields) for non-blank records; may raise csv.Error."""
    reader = csv.reader(io.StringIO(text, newline=""))
    while True:
        start = reader.line_num + 1
        try:
            fields = next(reader)
        except StopIteration:
            return
        if not any(cell.strip() for cell in fields) and len(fields) <= 1:
            continue
        yield start, fields


def _parse(content, file_type, required, optional, build, dup_key):
    result = ParseResult()
    text = _decode(content)
    if text is None:
        result.messages.append(_error(file_type, None, "file is not valid UTF-8"))
        return result
    try:
        records = _read_records(text)
        header_rec = next(records, None)
        if header_rec is None:
            result.messages.append(_error(file_type, None, "file is empty: no header"))
            return result
        header_line, header = header_rec
        names = [h.strip().lower() for h in header]
        known = set(required) | set(optional)
        ok = True
        for col in required:
            if col not in names:
                ok = False
                result.messages.append(
                    _error(file_type, header_line, f"missing required column '{col}'")
                )
        for col in sorted(known):
            if names.count(col) > 1:
                ok = False
                result.messages.append(
                    _error(file_type, header_line, f"column '{col}' is repeated")
                )
        if not ok:
            return result
        seen = {}
        for line, fields in records:
            if len(fields) != len(names):
                result.messages.append(
                    _error(
                        file_type,
                        line,
                        f"expected {len(names)} fields, found {len(fields)}",
                    )
                )
                continue
            raw = {}
            for name, value in zip(names, fields, strict=True):
                if name in known:
                    raw[name] = value.strip()
            row, errors = build(raw)
            for text_ in errors:
                result.messages.append(_error(file_type, line, text_))
            if errors:
                continue
            key = dup_key(row)
            if key in seen:
                result.messages.append(
                    _error(
                        file_type,
                        line,
                        f"duplicate of row {seen[key]}",
                    )
                )
                continue
            seen[key] = line
            row["row"] = line
            result.rows.append(row)
    except csv.Error as exc:
        result.messages.append(_error(file_type, None, f"malformed CSV: {exc}"))
    return result


def _choice(raw, col, allowed, errors, required=True):
    value = raw.get(col, "").upper()
    if not value and not required:
        return None
    if value not in allowed:
        errors.append(f"{col} '{raw.get(col, '')}' must be one of {', '.join(allowed)}")
        return None
    return value


def _name(raw, col, errors):
    value = raw.get(col, "")
    if not value:
        errors.append(f"{col} is empty")
    return value


# --- calendar ---------------------------------------------------------------


def _build_calendar(raw):
    errors = []
    date = None
    if _DATE.fullmatch(raw["data"]):
        try:
            date = datetime.date.fromisoformat(raw["data"])
        except ValueError:
            pass
    if date is None:
        errors.append(f"data '{raw['data']}' is not a valid YYYY-MM-DD date")
    hours = None
    if _DIGITS.fullmatch(raw["ore_max_giornata"]) and (
        int(raw["ore_max_giornata"]) in MAX_HOURS_VALUES
    ):
        hours = int(raw["ore_max_giornata"])
    else:
        errors.append(f"ore_max_giornata '{raw['ore_max_giornata']}' must be 4, 5 or 6")
    group = _choice(raw, "gruppo", GROUPS, errors, required=False)
    row = {
        "data": date,
        "ore_max_giornata": hours,
        "flag_chiusura": raw.get("flag_chiusura", "").lower() in TRUE_VALUES,
        "stage_classe_id": raw.get("stage_classe_id") or None,
        "gruppo": group,
    }
    return row, errors


def _calendar_key(row):
    if row["stage_classe_id"]:
        return ("stage", row["data"], normalize_name(row["stage_classe_id"]))
    return ("group", row["data"], row["gruppo"])


def parse_calendar(content):
    return _parse(
        content,
        "calendar",
        ("data", "ore_max_giornata"),
        ("flag_chiusura", "stage_classe_id", "gruppo"),
        _build_calendar,
        _calendar_key,
    )


# --- teachers ---------------------------------------------------------------


def _build_teacher(raw):
    errors = []
    nome = _name(raw, "nome", errors)
    tipo = _choice(raw, "tipo", TEACHER_TYPES, errors)
    return {"nome": nome, "tipo": tipo, "email": raw.get("email") or None}, errors


def parse_teachers(content):
    return _parse(
        content,
        "teachers",
        ("nome", "tipo"),
        ("email",),
        _build_teacher,
        lambda r: normalize_name(r["nome"]),
    )


# --- classes ----------------------------------------------------------------


def _build_class(raw):
    errors = []
    nome = _name(raw, "nome", errors)
    students = raw.get("n_studenti", "")
    if students == "":
        students = None
    elif _DIGITS.fullmatch(students):
        students = int(students)
    else:
        errors.append(f"n_studenti '{students}' must be an integer >= 0")
    group = _choice(raw, "gruppo", GROUPS, errors, required=False)
    return {"nome": nome, "n_studenti": students, "gruppo": group}, errors


def parse_classes(content):
    return _parse(
        content,
        "classes",
        ("nome",),
        ("n_studenti", "gruppo"),
        _build_class,
        lambda r: normalize_name(r["nome"]),
    )


# --- subjects ---------------------------------------------------------------


def _build_subject(raw):
    errors = []
    nome = _name(raw, "nome", errors)
    tipo = _choice(raw, "tipo", SUBJECT_TYPES, errors)
    peso = _choice(raw, "peso_cognitivo", COGNITIVE_WEIGHTS, errors)
    return {"nome": nome, "tipo": tipo, "peso_cognitivo": peso}, errors


def parse_subjects(content):
    return _parse(
        content,
        "subjects",
        ("nome", "tipo", "peso_cognitivo"),
        (),
        _build_subject,
        lambda r: normalize_name(r["nome"]),
    )


# --- assignments ------------------------------------------------------------


def _build_assignment(raw):
    errors = []
    row = {
        col: _name(raw, col, errors)
        for col in ("docente_nome", "classe_nome", "materia_nome")
    }
    hours = raw["ore_anno"]
    if _DIGITS.fullmatch(hours) and int(hours) >= 1:
        row["ore_anno"] = int(hours)
    else:
        errors.append(f"ore_anno '{hours}' must be an integer >= 1")
        row["ore_anno"] = None
    return row, errors


def parse_assignments(content):
    return _parse(
        content,
        "assignments",
        ("docente_nome", "classe_nome", "materia_nome", "ore_anno"),
        (),
        _build_assignment,
        lambda r: (
            normalize_name(r["docente_nome"]),
            normalize_name(r["classe_nome"]),
            normalize_name(r["materia_nome"]),
        ),
    )


# --- pairings ---------------------------------------------------------------


def _build_pairing(raw):
    errors = []
    row = {
        col: _name(raw, col, errors) for col in ("classe_a", "classe_b", "materia_nome")
    }
    if row["classe_a"] and normalize_name(row["classe_a"]) == normalize_name(
        row["classe_b"]
    ):
        errors.append("classe_a and classe_b must differ")
    row["note"] = raw.get("note") or None
    return row, errors


def parse_pairings(content):
    return _parse(
        content,
        "pairings",
        ("classe_a", "classe_b", "materia_nome"),
        ("note",),
        _build_pairing,
        lambda r: (
            frozenset((normalize_name(r["classe_a"]), normalize_name(r["classe_b"]))),
            normalize_name(r["materia_nome"]),
        ),
    )

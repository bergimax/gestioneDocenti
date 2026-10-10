"""Deterministic parser for the agency's annual calendar PDF (specs section 5.2).

Pure: reads a PDF (path or bytes) with pdfplumber, no database, no Django models.
"""

import datetime
import io
import re
from pathlib import Path

import pdfplumber

from .parsers import GROUPS, Message, ParseResult

# x0 ranges (PDF points) of the five group columns of the "CALENDARIO AF" template.
CALENDARIO_PDF_GRUPPI_COLONNE = {
    "PRIME": (140, 195),
    "SECONDE": (195, 250),
    "TERZE": (250, 305),
    "QUARTE": (305, 360),
    "PRIMA4+2": (360, 410),
}
CALENDARIO_PDF_COLONNA_DATA_MAX_X0 = 140
CALENDARIO_PDF_AREA_MAX_X = 410
# A cell with at least this many hours marks a group stage day.
STAGE_ORE_GIORNATA = 8

MESI = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
}
_DATE_ROW = re.compile(r"^(?:\S+\s+)?(\d{1,2})\s+([^\W\d_]+)\s+(\d{4})$", re.IGNORECASE)
_LINE_TOLERANCE = 3
_FILE_TYPE = "calendar_pdf"


class CalendarPdfError(Exception):
    """The input is not a readable PDF."""


class CalendarPdfNoDatesError(CalendarPdfError):
    """No date row was recognised: the PDF uses a different template."""


def _parse_date(text):
    match = _DATE_ROW.match(" ".join(text.split()))
    if not match:
        return None
    month = MESI.get(match.group(2).casefold())
    if month is None:
        return None
    try:
        return datetime.date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def _cell_value(words):
    text = "".join(w["text"] for w in words).strip()
    if not text:
        return 0
    try:
        return int(text) if re.fullmatch(r"\d+", text) else 0
    except ValueError:
        return 0


def _lines(page):
    """Words of the main table, grouped by visual line, top to bottom."""
    page = page.crop((0, 0, min(CALENDARIO_PDF_AREA_MAX_X, page.width), page.height))
    words = sorted(page.extract_words(), key=lambda w: (w["top"], w["x0"]))
    lines = []
    for word in words:
        if lines and abs(word["top"] - lines[-1][0]) <= _LINE_TOLERANCE:
            lines[-1][1].append(word)
        else:
            lines.append((word["top"], [word]))
    return [ws for _, ws in lines]


def _open(source):
    try:
        if isinstance(source, bytes | bytearray):
            return pdfplumber.open(io.BytesIO(bytes(source)))
        if isinstance(source, str | Path):
            return pdfplumber.open(source)
        return pdfplumber.open(source)
    except Exception as exc:
        raise CalendarPdfError(
            "Il file non è un PDF leggibile; impossibile leggere il calendario."
        ) from exc


def _read_dates(source):
    """Return (messages, {date: {group: hours}}) in reading order."""
    messages = []
    by_date = {}
    try:
        with _open(source) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                for words in _lines(page):
                    label = " ".join(
                        w["text"]
                        for w in words
                        if w["x0"] < CALENDARIO_PDF_COLONNA_DATA_MAX_X0
                    )
                    if not label:
                        continue
                    day = _parse_date(label)
                    if day is None:
                        continue
                    if day in by_date:
                        messages.append(
                            Message(
                                "warning",
                                _FILE_TYPE,
                                number,
                                f"Data {day.isoformat()} ripetuta nel PDF "
                                f"(pagina {number}): la prima riga è mantenuta.",
                            )
                        )
                        continue
                    by_date[day] = {
                        group: _cell_value([w for w in words if lo <= w["x0"] < hi])
                        for group, (lo, hi) in CALENDARIO_PDF_GRUPPI_COLONNE.items()
                    }
    except CalendarPdfError:
        raise
    except Exception as exc:
        raise CalendarPdfError(
            "Impossibile leggere il PDF del calendario: file danneggiato."
        ) from exc
    return messages, by_date


def parse_calendar_pdf(source):
    """Parse the calendar PDF (path or bytes) into calendar rows.

    Returns a ParseResult whose rows have the same shape as `parse_calendar`
    plus `flag_stage_gruppo`. Raises CalendarPdfError for unreadable input and
    CalendarPdfNoDatesError when no date row is recognised.
    """
    messages, by_date = _read_dates(source)
    if not by_date:
        raise CalendarPdfNoDatesError(
            "Nessuna riga data riconosciuta nel PDF: serve un parser diverso "
            "per un altro template."
        )
    rows = []
    for day, cells in by_date.items():
        for group in GROUPS:
            hours = cells[group]
            rows.append(
                {
                    "data": day,
                    "gruppo": group,
                    "ore_max_giornata": hours,
                    "flag_chiusura": False,
                    "stage_classe_id": None,
                    "flag_stage_gruppo": hours >= STAGE_ORE_GIORNATA,
                }
            )
    first, last = min(by_date), max(by_date)
    for offset in range((last - first).days + 1):
        day = first + datetime.timedelta(days=offset)
        if day.weekday() < 5 and day not in by_date:
            rows.append(
                {
                    "data": day,
                    "gruppo": None,
                    "ore_max_giornata": 0,
                    "flag_chiusura": True,
                    "stage_classe_id": None,
                    "flag_stage_gruppo": False,
                }
            )
    rows.sort(
        key=lambda r: (r["data"], GROUPS.index(r["gruppo"]) if r["gruppo"] else -1)
    )
    return ParseResult(rows=rows, messages=messages)

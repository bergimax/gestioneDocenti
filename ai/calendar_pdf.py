"""Vision reader for the annual calendar PDF (specs section 5.2).

`read_calendar_pdf(pdf_bytes)` sends the PDF to the Anthropic model and
returns plain calendar rows in the shape `parse_calendar_pdf` produces:
dicts with `data` (ISO `YYYY-MM-DD` string), `gruppo` (a group name or None
for a closure), `ore_max_giornata` (int), `flag_chiusura` (bool),
`stage_classe_id` (always None) and `flag_stage_gruppo` (bool).

The values are what the model said: an invalid date, an unknown group or an
hours value out of range is NOT rejected here, the setup validator lists it
on its row. Only a response that is not the expected JSON shape raises
`AiError`. No Django models are imported, the bytes are never stored or
logged, and neither is the API key.
"""

import base64
import json
import logging
import re

import anthropic
from django.conf import settings

logger = logging.getLogger(__name__)

STAGE_HOURS = 8
MAX_TOKENS = 16000
PROMPT = """\
Questo PDF è il calendario scolastico annuale di una scuola italiana: ogni riga \
è un giorno, con una colonna per ciascun gruppo (PRIME, SECONDE, TERZE, \
QUARTE, PRIMA4+2) che riporta le ore di lezione di quel giorno per quel gruppo. \
Le celle vuote valgono 0 ore. Un giorno di chiusura della scuola (festivo, \
vacanza) è un giorno da segnare come chiusura.

Rispondi SOLO con un oggetto JSON, senza testo prima o dopo, in questa forma:
{"giorni": [{"data": "AAAA-MM-GG", "chiusura": false, \
"ore": {"PRIME": 6, "SECONDE": 6, "TERZE": 5, "QUARTE": 5, "PRIMA4+2": 0}}]}
Per un giorno di chiusura usa "chiusura": true e "ore": {}. Includi ogni giorno \
presente nel PDF, in ordine di data. Non inventare giorni che non vedi.
"""


class AiError(Exception):
    """The AI call failed or answered with something unusable.

    `args[0]` is a message safe to show to the admin (Italian).
    """


def build_client():
    """The Anthropic client; the one place a client is created (mock this)."""
    return anthropic.Anthropic(
        api_key=settings.ANTHROPIC_API_KEY,
        timeout=float(settings.ANTHROPIC_TIMEOUT_SECONDS),
        max_retries=0,
    )


def _invalid():
    return AiError("La risposta dell'AI non è valida: nessuna riga è stata letta.")


def _json_object(text):
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except ValueError:
        raise _invalid() from None
    if not isinstance(data, dict):
        raise _invalid()
    return data


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def rows_from_response(text):
    """Validate the model's JSON shape and expand it to calendar rows."""
    days = _json_object(text).get("giorni")
    if not isinstance(days, list):
        raise _invalid()
    rows = []
    for day in days:
        if not isinstance(day, dict) or not isinstance(day.get("data"), str):
            raise _invalid()
        closure = day.get("chiusura", False)
        hours = day.get("ore", {})
        if not isinstance(closure, bool) or not isinstance(hours, dict):
            raise _invalid()
        if closure:
            groups = [(None, 0)]
        else:
            if not all(isinstance(k, str) and _is_int(v) for k, v in hours.items()):
                raise _invalid()
            groups = list(hours.items())
        for group, value in groups:
            rows.append(
                {
                    "data": day["data"].strip(),
                    "gruppo": group,
                    "ore_max_giornata": value,
                    "flag_chiusura": closure,
                    "stage_classe_id": None,
                    "flag_stage_gruppo": group is not None and value >= STAGE_HOURS,
                }
            )
    return rows


def read_calendar_pdf(pdf_bytes):
    """Read the calendar PDF with the vision model; return a list of row dicts.

    Raises AiError on an API error, a timeout or an unusable answer.
    """
    document = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
        },
    }
    try:
        response = build_client().messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": [document, {"type": "text", "text": PROMPT}],
                }
            ],
        )
    except anthropic.APITimeoutError:
        raise AiError("L'AI non ha risposto in tempo: riprova più tardi.") from None
    except anthropic.APIError as exc:
        # Only the class name: the message could echo request details.
        logger.warning("Anthropic call failed: %s", type(exc).__name__)
        raise AiError(
            "Il servizio AI non è raggiungibile: riprova più tardi."
        ) from None
    text = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )
    return rows_from_response(text)

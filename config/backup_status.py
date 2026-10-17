"""Age of the last successful backup, read from the marker deploy/backup.sh writes.

The marker holds one line: ``<UTC ISO-8601 timestamp>Z <dump file name>``.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

_LINE = re.compile(
    r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (gestione-docenti-[^ /]+\.dump)$"
)


def read_backup_status(path, threshold_hours, now=None):
    """Return a dict: ``ok`` False if unverifiable, else ``hours``, ``stale``, ``dump``.

    Never raises: a missing, empty, unreadable, malformed or future-dated
    marker gives ``{"ok": False}``. Staleness compares whole hours, like
    deploy/backup-status.sh.
    """
    unknown = {"ok": False}
    if not path:
        return unknown
    now = now or datetime.now(UTC)
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return unknown
    match = _LINE.match(lines[0]) if len(lines) == 1 else None
    if not match:
        return unknown
    try:
        written = datetime.strptime(match[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return unknown
    if written > now:
        return unknown
    hours = int((now - written).total_seconds() // 3600)
    return {
        "ok": True,
        "hours": hours,
        "stale": hours > threshold_hours,
        "dump": match[2],
    }

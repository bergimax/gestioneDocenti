"""Environment parsing. Every function takes a mapping so tests can pass a dict."""

from collections.abc import Mapping
from urllib.parse import unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured

DEFAULT_SOLVER_TIMEOUT_SECONDS = 60
DEFAULT_SETUP_MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def get_required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"{name} is required but not set.")
    return value


def get_bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return False
    raise ImproperlyConfigured(
        f"{name} must be one of true, false, 1, 0 (got {raw!r})."
    )


def get_hosts(env: Mapping[str, str], name: str, required: bool) -> list[str]:
    hosts = [h.strip() for h in env.get(name, "").split(",") if h.strip()]
    if required and not hosts:
        raise ImproperlyConfigured(
            f"{name} is required when DEBUG is false (comma-separated hosts)."
        )
    return hosts


def get_positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ImproperlyConfigured(
            f"{name} must be a positive integer (got {raw!r})."
        ) from None
    if value <= 0:
        raise ImproperlyConfigured(f"{name} must be a positive integer (got {raw!r}).")
    return value


def parse_database_url(url: str, name: str = "DATABASE_URL") -> dict:
    parts = urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql"):
        raise ImproperlyConfigured(
            f"{name} must be a PostgreSQL URL (postgres://user:pass@host:5432/db)."
        )
    db_name = unquote(parts.path.lstrip("/"))
    if not db_name:
        raise ImproperlyConfigured(f"{name} must include a database name.")
    try:
        port = parts.port
    except ValueError:
        raise ImproperlyConfigured(f"{name} has an invalid port.") from None
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": db_name,
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or "",
        "PORT": str(port) if port else "",
    }

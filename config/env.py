"""Environment parsing. Every function takes a mapping so tests can pass a dict."""

from collections.abc import Mapping
from urllib.parse import unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured

DEFAULT_SOLVER_TIMEOUT_SECONDS = 60
DEFAULT_SOLVER_EXPLAIN_BUDGET_SECONDS = 10
DEFAULT_AVAILABILITY_LINK_TTL_HOURS = 72
DEFAULT_LINK_LOOKUP_MAX_FAILURES = 10
DEFAULT_LINK_LOOKUP_WINDOW_SECONDS = 300
DEFAULT_LOGIN_MAX_FAILURES = 5
DEFAULT_LOGIN_WINDOW_SECONDS = 900
DEFAULT_LOGIN_ADDRESS_MAX_FAILURES = 20
DEFAULT_SETUP_MAX_UPLOAD_BYTES = 5 * 1024 * 1024
DEFAULT_SETUP_UPLOAD_MAX_AGE_HOURS = 72
DEFAULT_SOLVER_STALE_MARGIN_SECONDS = 120
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"
DEFAULT_ANTHROPIC_TIMEOUT_SECONDS = 60


def get_required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"{name} is required but not set.")
    return value


def get_optional_str(env: Mapping[str, str], name: str, default: str = "") -> str:
    """A stripped string; unset or blank gives the default."""
    value = env.get(name, "").strip()
    return value or default


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


def get_domain(env: Mapping[str, str], required: bool) -> str:
    """DOMAIN: a bare host name, no scheme or path."""
    value = env.get("DOMAIN", "").strip().lower()
    if not value:
        if required:
            raise ImproperlyConfigured("DOMAIN is required when DEBUG is false.")
        return ""
    if "://" in value or "/" in value or " " in value:
        raise ImproperlyConfigured(
            f"DOMAIN must be a bare host name without scheme or path (got {value!r})."
        )
    return value


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


def get_explain_budget(env: Mapping[str, str], timeout: int) -> int:
    """SOLVER_EXPLAIN_BUDGET_SECONDS: a positive integer below ``timeout``."""
    name = "SOLVER_EXPLAIN_BUDGET_SECONDS"
    value = get_positive_int(env, name, DEFAULT_SOLVER_EXPLAIN_BUDGET_SECONDS)
    if value >= timeout:
        raise ImproperlyConfigured(
            f"{name} ({value}) must be below SOLVER_TIMEOUT_SECONDS ({timeout})."
        )
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

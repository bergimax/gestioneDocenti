import os
from pathlib import Path

from config.env import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_ANTHROPIC_TIMEOUT_SECONDS,
    DEFAULT_AVAILABILITY_LINK_PURGE_DAYS,
    DEFAULT_AVAILABILITY_LINK_TTL_HOURS,
    DEFAULT_LINK_LOOKUP_MAX_FAILURES,
    DEFAULT_LINK_LOOKUP_WINDOW_SECONDS,
    DEFAULT_LOGIN_ADDRESS_MAX_FAILURES,
    DEFAULT_LOGIN_MAX_FAILURES,
    DEFAULT_LOGIN_WINDOW_SECONDS,
    DEFAULT_SETUP_MAX_UPLOAD_BYTES,
    DEFAULT_SETUP_UPLOAD_MAX_AGE_HOURS,
    DEFAULT_SOLVER_STALE_MARGIN_SECONDS,
    DEFAULT_SOLVER_TIMEOUT_SECONDS,
    get_bool,
    get_domain,
    get_explain_budget,
    get_hosts,
    get_optional_str,
    get_positive_int,
    get_required,
    parse_database_url,
)

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = get_required(os.environ, "SECRET_KEY")
DEBUG = get_bool(os.environ, "DEBUG", default=False)
ALLOWED_HOSTS = get_hosts(os.environ, "ALLOWED_HOSTS", required=not DEBUG)
# Public host name; TLS ends at the reverse proxy. Required in production.
DOMAIN = get_domain(os.environ, required=not DEBUG)
SOLVER_TIMEOUT_SECONDS = get_positive_int(
    os.environ, "SOLVER_TIMEOUT_SECONDS", DEFAULT_SOLVER_TIMEOUT_SECONDS
)
SOLVER_EXPLAIN_BUDGET_SECONDS = get_explain_budget(os.environ, SOLVER_TIMEOUT_SECONDS)
SOLVER_STALE_MARGIN_SECONDS = get_positive_int(
    os.environ, "SOLVER_STALE_MARGIN_SECONDS", DEFAULT_SOLVER_STALE_MARGIN_SECONDS
)
SETUP_MAX_UPLOAD_BYTES = get_positive_int(
    os.environ, "SETUP_MAX_UPLOAD_BYTES", DEFAULT_SETUP_MAX_UPLOAD_BYTES
)
SETUP_UPLOAD_MAX_AGE_HOURS = get_positive_int(
    os.environ, "SETUP_UPLOAD_MAX_AGE_HOURS", DEFAULT_SETUP_UPLOAD_MAX_AGE_HOURS
)
# Empty key = the AI features are off and absent from the UI.
ANTHROPIC_API_KEY = get_optional_str(os.environ, "ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = get_optional_str(
    os.environ, "ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL
)
ANTHROPIC_TIMEOUT_SECONDS = get_positive_int(
    os.environ, "ANTHROPIC_TIMEOUT_SECONDS", DEFAULT_ANTHROPIC_TIMEOUT_SECONDS
)
AVAILABILITY_LINK_TTL_HOURS = get_positive_int(
    os.environ, "AVAILABILITY_LINK_TTL_HOURS", DEFAULT_AVAILABILITY_LINK_TTL_HOURS
)
AVAILABILITY_LINK_PURGE_DAYS = get_positive_int(
    os.environ, "AVAILABILITY_LINK_PURGE_DAYS", DEFAULT_AVAILABILITY_LINK_PURGE_DAYS
)
LINK_LOOKUP_MAX_FAILURES = get_positive_int(
    os.environ, "LINK_LOOKUP_MAX_FAILURES", DEFAULT_LINK_LOOKUP_MAX_FAILURES
)
LINK_LOOKUP_WINDOW_SECONDS = get_positive_int(
    os.environ, "LINK_LOOKUP_WINDOW_SECONDS", DEFAULT_LINK_LOOKUP_WINDOW_SECONDS
)
LOGIN_MAX_FAILURES = get_positive_int(
    os.environ, "LOGIN_MAX_FAILURES", DEFAULT_LOGIN_MAX_FAILURES
)
LOGIN_WINDOW_SECONDS = get_positive_int(
    os.environ, "LOGIN_WINDOW_SECONDS", DEFAULT_LOGIN_WINDOW_SECONDS
)
LOGIN_ADDRESS_MAX_FAILURES = get_positive_int(
    os.environ, "LOGIN_ADDRESS_MAX_FAILURES", DEFAULT_LOGIN_ADDRESS_MAX_FAILURES
)

# Marker written by deploy/backup.sh, as the web process sees it (empty: unknown).
BACKUP_MARKER_FILE = get_optional_str(os.environ, "BACKUP_MARKER_FILE")
BACKUP_STALE_HOURS = get_positive_int(os.environ, "BACKUP_STALE_HOURS", 30)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "school",
    "availability",
    "scheduling",
    "feeds",
    "ledger",
    "django_tasks",
    "django_tasks_db",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "projects.context_processors.navigation",
            ]
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {"default": parse_database_url(get_required(os.environ, "DATABASE_URL"))}

LANGUAGE_CODE = "it"
TIME_ZONE = "Europe/Rome"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# collectstatic target; the proxy serves it, Django does not.
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "accounts.validators.MinimumLengthItalian"},
]
SESSION_COOKIE_AGE = 7 * 24 * 60 * 60  # 7 days, specs §4

# Behind the TLS-terminating proxy Django sees plain HTTP: trust its
# forwarded-proto header, accept the public origin, mark cookies secure.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
CSRF_TRUSTED_ORIGINS = [f"https://{DOMAIN}"] if DOMAIN else []

# Magic-link tokens travel in URL paths: redact them from request logs.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "redact_link_tokens": {"()": "availability.logging.RedactLinkTokens"},
    },
    "loggers": {
        "django.request": {"filters": ["redact_link_tokens"]},
        "django.server": {"filters": ["redact_link_tokens"]},
    },
}

# Background jobs (architecture 7.1): PostgreSQL is the queue, no broker.
# django_tasks is the compat package that provides django.tasks on Django 5.2.
TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend"}}

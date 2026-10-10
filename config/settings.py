import os
from pathlib import Path

from config.env import (
    DEFAULT_SETUP_MAX_UPLOAD_BYTES,
    DEFAULT_SOLVER_TIMEOUT_SECONDS,
    get_bool,
    get_hosts,
    get_positive_int,
    get_required,
    parse_database_url,
)

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = get_required(os.environ, "SECRET_KEY")
DEBUG = get_bool(os.environ, "DEBUG", default=False)
ALLOWED_HOSTS = get_hosts(os.environ, "ALLOWED_HOSTS", required=not DEBUG)
SOLVER_TIMEOUT_SECONDS = get_positive_int(
    os.environ, "SOLVER_TIMEOUT_SECONDS", DEFAULT_SOLVER_TIMEOUT_SECONDS
)
SETUP_MAX_UPLOAD_BYTES = get_positive_int(
    os.environ, "SETUP_MAX_UPLOAD_BYTES", DEFAULT_SETUP_MAX_UPLOAD_BYTES
)

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

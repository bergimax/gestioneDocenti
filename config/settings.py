import os
from pathlib import Path

from config.env import (
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

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {"default": parse_database_url(get_required(os.environ, "DATABASE_URL"))}

LANGUAGE_CODE = "it"
TIME_ZONE = "Europe/Rome"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

"""Login throttle: failed attempts per account and per client address.

Mirrors the magic-link limiter in availability/links.py. Stored in Postgres.
"""

import datetime

from django.conf import settings
from django.utils import timezone

from .models import LoginFailure


def normalize_email(email):
    return (email or "").strip().lower()[:254]


def _window_start(current):
    return current - datetime.timedelta(seconds=settings.LOGIN_WINDOW_SECONDS)


def is_locked(email, client_address):
    since = _window_start(timezone.now())
    recent = LoginFailure.objects.filter(attempted_at__gt=since)
    email = normalize_email(email)
    if email and recent.filter(email=email).count() >= settings.LOGIN_MAX_FAILURES:
        return True
    return (
        recent.filter(client_address=client_address[:64]).count()
        >= settings.LOGIN_ADDRESS_MAX_FAILURES
    )


def record_failure(email, client_address):
    current = timezone.now()
    LoginFailure.objects.filter(attempted_at__lte=_window_start(current)).delete()
    LoginFailure.objects.create(
        email=normalize_email(email),
        client_address=client_address[:64],
        attempted_at=current,
    )


def clear_account(email):
    LoginFailure.objects.filter(email=normalize_email(email)).delete()

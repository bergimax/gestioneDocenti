"""Rate limit for failed feed token lookups, stored in Postgres.

Same shape as availability/links.py. Takes only a client address: never a
token (nothing token-like is stored or logged).
"""

import datetime

from django.conf import settings
from django.utils import timezone

from .models import FeedLookupFailure


def _window_start(current):
    return current - datetime.timedelta(seconds=settings.FEED_LOOKUP_WINDOW_SECONDS)


def is_limited(client_address):
    count = FeedLookupFailure.objects.filter(
        client_address=client_address[:64],
        attempted_at__gt=_window_start(timezone.now()),
    ).count()
    return count >= settings.FEED_LOOKUP_MAX_FAILURES


def record_failure(client_address):
    current = timezone.now()
    FeedLookupFailure.objects.filter(attempted_at__lte=_window_start(current)).delete()
    FeedLookupFailure.objects.create(
        client_address=client_address[:64], attempted_at=current
    )

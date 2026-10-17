"""Magic-link tokens: creation, lookup, revocation and the lookup rate limit.

Never log a token. The full token exists only in the return value of
create_link and in the URL the admin copies.
"""

import datetime
import hashlib
import re
import secrets

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from . import grid as grid_logic
from .models import AvailabilityLink, LinkLookupFailure

TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")  # token_urlsafe(32)


class LinkError(Exception):
    """A generation request that cannot be honoured (message is user-facing)."""


def now():
    return timezone.now()


def hash_token(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def validate_request(teacher, week_start):
    if teacher is None:
        raise LinkError("Docente non trovato.")
    if not teacher.active:
        raise LinkError("Il docente non e attivo.")
    try:
        grid_logic.check_monday(week_start)
    except (ValueError, AttributeError, TypeError):
        raise LinkError("La settimana deve iniziare di lunedi.") from None
    school = teacher.school
    starts = {
        w["start"] for w in grid_logic.list_weeks(school.year_start, school.year_end)
    }
    if week_start not in starts:
        raise LinkError("Settimana fuori dall'anno formativo.")


@transaction.atomic
def create_link(teacher, week_start, user):
    """Revoke the active link of teacher+week, create a new one.

    Returns (link, token). Raises LinkError (nothing is written).
    """
    validate_request(teacher, week_start)
    current = now()
    AvailabilityLink.objects.filter(
        teacher=teacher, week_start=week_start, revoked_at__isnull=True
    ).update(revoked_at=current)
    token = secrets.token_urlsafe(32)
    link = AvailabilityLink.objects.create(
        teacher=teacher,
        week_start=week_start,
        token_hash=hash_token(token),
        expires_at=current
        + datetime.timedelta(hours=settings.AVAILABILITY_LINK_TTL_HOURS),
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    return link, token


def lookup_link(token):
    """The usable link for `token`, else None (no distinction why)."""
    if not isinstance(token, str) or not TOKEN_FORMAT.fullmatch(token):
        return None
    link = (
        AvailabilityLink.objects.select_related("teacher")
        .filter(token_hash=hash_token(token), revoked_at__isnull=True)
        .first()
    )
    if link is None or link.expires_at <= now():
        return None
    return link


def mark_used(link):
    AvailabilityLink.objects.filter(pk=link.pk, used_at__isnull=True).update(
        used_at=now()
    )


def link_status(link, at=None):
    at = at or now()
    if link.revoked_at is not None:
        return "revocato"
    if link.expires_at <= at:
        return "scaduto"
    return "attivo"


@transaction.atomic
def revoke_link(link_id):
    """Set revoked_at on an active link; anything else is a no-op."""
    return AvailabilityLink.objects.filter(
        pk=link_id, revoked_at__isnull=True, expires_at__gt=now()
    ).update(revoked_at=now())


def is_rate_limited(client_address):
    since = now() - datetime.timedelta(seconds=settings.LINK_LOOKUP_WINDOW_SECONDS)
    count = LinkLookupFailure.objects.filter(
        client_address=client_address, attempted_at__gt=since
    ).count()
    return count >= settings.LINK_LOOKUP_MAX_FAILURES


def record_failure(client_address):
    current = now()
    cutoff = current - datetime.timedelta(seconds=settings.LINK_LOOKUP_WINDOW_SECONDS)
    LinkLookupFailure.objects.filter(attempted_at__lte=cutoff).delete()
    LinkLookupFailure.objects.create(
        client_address=client_address[:64], attempted_at=current
    )


def purge_dead_links(days, dry_run=False):
    """Delete links dead for more than `days` days and stale lookup failures.

    A link is dead since the earlier of expires_at and revoked_at. Takes the
    setup advisory lock so it never races a setup replace. Returns
    (links, failures) counts; with `dry_run` nothing is deleted.
    """
    from school.setup_service import SETUP_LOCK_ID

    current = now()
    cutoff = current - datetime.timedelta(days=days)
    window = current - datetime.timedelta(seconds=settings.LINK_LOOKUP_WINDOW_SECONDS)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [SETUP_LOCK_ID])
        dead = AvailabilityLink.objects.filter(
            Q(expires_at__lt=cutoff) | Q(revoked_at__lt=cutoff)
        )
        stale = LinkLookupFailure.objects.filter(attempted_at__lte=window)
        counts = (dead.count(), stale.count())
        if not dry_run:
            dead.delete()
            stale.delete()
    return counts
